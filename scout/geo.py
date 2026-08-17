"""Public-transport commute times via transport.opendata.ch.

This is what makes the corridor search actually work: "between Zurich and Basel"
is meaningless as a radius, because in Switzerland the map that matters is the
rail map. Aarau is 30 minutes from Zurich HB; a village 8 km away with no station
is 70.

The API is free and needs no key, but is soft-limited to roughly 1000 requests a
day, so everything is cached in SQLite:

* `station_cache` maps a ~100 m coordinate grid cell (or a text address) to its
  nearest station. Every listing in the same neighbourhood shares one lookup.
* `route_cache` maps (station, destination, arrival time) to a duration, with a
  30-day TTL. Every listing in the same town shares one lookup.

After the first run almost every listing resolves entirely from cache.

When opendata.ch does throttle anyway - which the corridor map makes far more
likely, because it prices hundreds of stations rather than dozens of towns -
routing falls back to search.ch's timetable API. It answers the same question
from the same Swiss timetable, on a separate quota, so one exhausted service no
longer stops the map filling in.

SBB's own site is not usable as a third source: www.sbb.ch answers 403 to
anything that is not a real browser (Akamai), and its timetable is a client-side
app with no stable deep link to a result set. What SBB *do* publish openly is
the station register itself, and that is where `stations.py` gets the map's
geometry from.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx

from .models import Commute, Criteria, Listing

log = logging.getLogger(__name__)

API = "https://transport.opendata.ch/v1"
#: Second opinion on the same timetable, used only once opendata.ch throttles.
SEARCH_CH_API = "https://timetable.search.ch/api/route.json"
ROUTE_TTL = timedelta(days=30)
STATION_TTL = timedelta(days=365)  # stations do not move
# A *failed* lookup is not a fact about the world - it is usually a rate limit
# or a transient error. Caching those for a year would make one bad afternoon
# permanent, so misses expire quickly and get retried.
STATION_MISS_TTL = timedelta(hours=6)

WALK_METRES_PER_MIN = 80.0
#: Furthest a listing may be from a registered train station before we stop
#: answering from the local table and ask the timetable API instead - which also
#: knows bus and tram stops, and may well find something closer.
MAX_LOCAL_WALK_M = 1600.0
#: give up on the timetable API for this run after this many 429s in a row
MAX_CONSECUTIVE_429 = 5
_DURATION_RE = re.compile(r"(?:(\d+)d)?(\d{2}):(\d{2}):(\d{2})")


def parse_duration(text: str) -> int | None:
    """opendata.ch returns durations as '00d00:27:00'."""
    match = _DURATION_RE.search(text or "")
    if not match:
        return None
    days, hours, minutes, _seconds = match.groups()
    return int(days or 0) * 1440 + int(hours) * 60 + int(minutes)


def _grid_key(lat: float, lon: float) -> str:
    # 3 decimal places is ~110 m in latitude - fine-grained enough that the
    # nearest station is the same for everything in the cell.
    return f"{lat:.3f},{lon:.3f}"


class CommuteService:
    """Resolves listings to commute minutes for both workplaces."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: httpx.AsyncClient,
        criteria: Criteria,
        rate_limit: float = 1.5,
    ) -> None:
        self.conn = conn
        self.client = client
        self.criteria = criteria
        # Measured the hard way: this service starts returning 429 well before
        # its documented daily quota if you request faster than about 1/second.
        self.rate_limit = rate_limit
        self._lock = asyncio.Lock()
        self.api_calls = 0
        self.searchch_calls = 0
        self._consecutive_failures = 0
        self._searchch_failures = 0
        self.throttled = False
        #: set once the fallback has failed as often as the primary did; at that
        #: point there is no timetable left to ask.
        self.searchch_throttled = False

    @property
    def calls(self) -> int:
        """Timetable requests made this run, across both services."""
        return self.api_calls + self.searchch_calls

    @property
    def exhausted(self) -> bool:
        """True only when *neither* timetable service is answering any more."""
        return self.throttled and self.searchch_throttled

    # -- HTTP -------------------------------------------------------------

    async def _get(self, path: str, params: dict) -> dict:
        """One request at a time, spaced out, backing off on 429.

        This is a free service run by volunteers, so being throttled is a signal
        to slow down rather than an error to retry through. After a few 429s in
        a row the service gives up for the rest of the run and lets the next run
        (by then mostly cache hits) finish the job.
        """
        if self.throttled:
            raise RuntimeError("timetable API is throttling us; skipping for this run")

        delay = self.rate_limit
        for attempt in range(3):
            async with self._lock:
                await asyncio.sleep(delay)
                self.api_calls += 1
                resp = await self.client.get(f"{API}/{path}", params=params, timeout=25.0)

            if resp.status_code == 429:
                delay = self.rate_limit * (3 ** (attempt + 1))
                self._consecutive_failures += 1
                if self._consecutive_failures >= MAX_CONSECUTIVE_429:
                    self.throttled = True
                    log.warning(
                        "timetable API returned 429 %d times in a row; "
                        "pausing commute lookups until the next run",
                        self._consecutive_failures,
                    )
                    raise RuntimeError("timetable API is throttling us")
                log.debug("timetable API 429, backing off %.1fs", delay)
                continue

            resp.raise_for_status()
            self._consecutive_failures = 0
            return resp.json()

        raise RuntimeError("timetable API kept returning 429")

    # -- station resolution ------------------------------------------------

    async def nearest_station(self, listing: Listing) -> tuple[str | None, int]:
        """Return (station name, walking minutes from the flat)."""
        if listing.lat is not None and listing.lon is not None:
            key = _grid_key(listing.lat, listing.lon)
            params = {"x": listing.lat, "y": listing.lon, "type": "station"}
        elif listing.city or listing.zipcode:
            # Query the town name alone. Including the postcode makes the API
            # return street-address matches, which come back with a null id and
            # are useless for routing; the bare town name returns real stations.
            town = (listing.city or "").strip()
            if not town:
                return None, 0
            key = f"q:{listing.zipcode or ''} {town}".strip().lower()
            params = {"query": town, "type": "station"}
        else:
            return None, 0

        cached = self._cached_station(key)
        if cached is not None:
            name, walk = cached
            return name, walk

        # The SBB station register (see `stations.py`) is a local table with
        # every corridor station's coordinates in it, so for a listing that has
        # coordinates the nearest station is arithmetic, not an API call. This
        # is what stops a cold database burning its whole timetable budget on
        # geocoding before it prices a single trip.
        if listing.lat is not None and listing.lon is not None:
            local = self._nearest_local(listing.lat, listing.lon)
            if local is not None:
                name, walk = local
                self._store_station(key, name, walk)
                return name, walk

        try:
            data = await self._get("locations", params)
        except Exception as exc:
            log.warning("station lookup failed for %s: %s", key, exc)
            return None, 0

        for station in data.get("stations") or []:
            # Entries without an id are street addresses, not stations.
            if not station.get("id") or not station.get("name"):
                continue
            distance = station.get("distance")
            walk = (
                int(round(float(distance) / WALK_METRES_PER_MIN))
                if isinstance(distance, (int, float))
                else 0
            )
            self._store_station(key, station["name"], walk)
            return station["name"], walk

        self._store_station(key, None, 0)
        return None, 0

    def _nearest_local(self, lat: float, lon: float) -> tuple[str, int] | None:
        """Nearest registered train station, or None if the table cannot answer.

        Deliberately gives up rather than guessing: beyond `MAX_LOCAL_WALK_M`
        the answer would be a station nobody would walk to, and the timetable
        API - which also knows about bus and tram stops - has a better one.
        """
        # A degree of latitude is ~111 km; this box is a cheap index-friendly
        # prefilter around the real distance test below.
        span = MAX_LOCAL_WALK_M / 111_000.0
        rows = self.conn.execute(
            "SELECT name, lat, lon FROM corridor_station "
            "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - span, lat + span, lon - span * 1.5, lon + span * 1.5),
        ).fetchall()
        best: tuple[float, str] | None = None
        for row in rows:
            metres = _metres_between(lat, lon, row["lat"], row["lon"])
            if metres <= MAX_LOCAL_WALK_M and (best is None or metres < best[0]):
                best = (metres, row["name"])
        if best is None:
            return None
        return best[1], int(round(best[0] / WALK_METRES_PER_MIN))

    def _cached_station(self, key: str) -> tuple[str | None, int] | None:
        row = self.conn.execute(
            "SELECT station, fetched_at FROM station_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        station = row["station"]
        ttl = STATION_TTL if station else STATION_MISS_TTL
        if _expired(row["fetched_at"], ttl):
            return None
        if not station:
            return None, 0
        name, _, walk = station.rpartition("|")
        return (name or station), int(walk or 0)

    def _store_station(self, key: str, name: str | None, walk: int) -> None:
        value = f"{name}|{walk}" if name else None
        self.conn.execute(
            "INSERT INTO station_cache (key, station, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET station = excluded.station, "
            "fetched_at = excluded.fetched_at",
            (key, value, _now()),
        )
        self.conn.commit()

    # -- routing -----------------------------------------------------------

    async def route(self, origin: str, destination: str) -> Commute | None:
        arrive_by = self.criteria.arrive_by
        cached = self._cached_route(origin, destination, arrive_by)
        if cached is not None:
            return cached

        # Living at the workplace's own station is a real answer, and it is the
        # one the timetable API refuses to give: asked to route Basel SBB to
        # Basel SBB it returns nothing. Recording it keeps the corridor map from
        # leaving a permanent hole exactly where the two anchors sit - and from
        # re-queueing them, at distance zero from the line, in every batch.
        if origin.strip().casefold() == destination.strip().casefold():
            self._store_route(origin, destination, arrive_by, 0, 0)
            return Commute(minutes=0, transfers=0, origin_station=origin)

        durations = await self._opendata_durations(origin, destination, arrive_by)
        if durations is None:
            durations = await self._searchch_durations(origin, destination, arrive_by)
        if durations is None:
            # Neither service answered at all. That is a fact about them, not
            # about this route, so nothing is cached and a later run retries.
            return None
        if not durations:
            self._store_route(origin, destination, arrive_by, None, 0)
            return None

        # A commuter picks a good connection, not the average one - but the
        # single fastest departure of the morning is not representative either.
        # Median of the three best is a fair stand-in for "what you'd actually
        # ride most mornings".
        durations.sort(key=lambda d: d[0])
        best = durations[:3]
        minutes = int(statistics.median(d[0] for d in best))
        transfers = int(statistics.median(d[1] for d in best))

        self._store_route(origin, destination, arrive_by, minutes, transfers)
        return Commute(minutes=minutes, transfers=transfers, origin_station=origin)

    async def _opendata_durations(
        self, origin: str, destination: str, arrive_by: str
    ) -> list[tuple[int, int]] | None:
        """(minutes, transfers) per connection, or None if the service failed."""
        try:
            data = await self._get(
                "connections",
                {
                    "from": origin,
                    "to": destination,
                    "date": _next_weekday().isoformat(),
                    "time": arrive_by,
                    "isArrivalTime": 1,
                    "limit": 5,
                },
            )
        except Exception as exc:
            log.debug("opendata.ch route %s -> %s failed: %s", origin, destination, exc)
            return None

        out: list[tuple[int, int]] = []
        for connection in data.get("connections") or []:
            minutes = parse_duration(connection.get("duration") or "")
            if minutes:
                out.append((minutes, int(connection.get("transfers") or 0)))
        return out

    async def _searchch_durations(
        self, origin: str, destination: str, arrive_by: str
    ) -> list[tuple[int, int]] | None:
        """Same question, asked of search.ch.

        Durations arrive in seconds, and `legs` includes a final arrival-only
        entry, so a direct train has two legs and zero changes.
        """
        if self.searchch_throttled:
            return None
        try:
            async with self._lock:
                await asyncio.sleep(self.rate_limit)
                self.searchch_calls += 1
                resp = await self.client.get(
                    SEARCH_CH_API,
                    params={
                        "from": origin,
                        "to": destination,
                        "date": _next_weekday().isoformat(),
                        "time": arrive_by,
                        "time_type": "arrival",
                        "num": 4,
                        "show_delays": 0,
                    },
                    timeout=25.0,
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._searchch_failures += 1
            if self._searchch_failures >= MAX_CONSECUTIVE_429:
                self.searchch_throttled = True
                log.warning(
                    "search.ch failed %d times in a row; no timetable service left "
                    "for this run", self._searchch_failures
                )
            log.warning("search.ch route %s -> %s failed: %s", origin, destination, exc)
            return None

        self._searchch_failures = 0
        out: list[tuple[int, int]] = []
        for connection in data.get("connections") or []:
            seconds = connection.get("duration")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                continue
            transfers = max(0, len(connection.get("legs") or []) - 2)
            out.append((int(round(seconds / 60.0)), transfers))
        return out

    def _cached_route(self, origin: str, destination: str, arrive_by: str) -> Commute | None:
        row = self.conn.execute(
            "SELECT minutes, transfers, fetched_at FROM route_cache "
            "WHERE origin = ? AND destination = ? AND arrive_by = ?",
            (origin, destination, arrive_by),
        ).fetchone()
        if not row:
            return None
        ttl = ROUTE_TTL if row["minutes"] is not None else STATION_MISS_TTL
        if _expired(row["fetched_at"], ttl):
            return None
        if row["minutes"] is None:
            return None
        return Commute(
            minutes=row["minutes"], transfers=row["transfers"] or 0, origin_station=origin
        )

    def _store_route(
        self, origin: str, destination: str, arrive_by: str, minutes: int | None, transfers: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO route_cache (origin, destination, arrive_by, minutes, transfers, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(origin, destination, arrive_by) DO UPDATE SET "
            "minutes = excluded.minutes, transfers = excluded.transfers, "
            "fetched_at = excluded.fetched_at",
            (origin, destination, arrive_by, minutes, transfers, _now()),
        )
        self.conn.commit()

    # -- public API --------------------------------------------------------

    async def commutes(self, listing: Listing) -> dict[str, Commute | None]:
        """Door-to-door minutes to both workplaces, walk to station included."""
        station, walk = await self.nearest_station(listing)
        if not station:
            return {"a": None, "b": None}

        legs: dict[str, Commute | None] = {}
        for leg, destination in (
            ("a", self.criteria.workplace_a),
            ("b", self.criteria.workplace_b),
        ):
            route = await self.route(station, destination)
            if route is None:
                legs[leg] = None
                continue
            legs[leg] = Commute(
                minutes=route.minutes + walk,
                transfers=route.transfers,
                origin_station=f"{station} (+{walk}' walk)" if walk else station,
            )
        return legs


def retry_due(stamp: str | None) -> bool:
    """Whether a route that came back empty is worth asking about again.

    An empty answer is usually a throttled or flaky request rather than a place
    with no trains, so it is retried - but not immediately, or the corridor map
    would spend every batch re-asking the same handful of failures instead of
    working through the stations it has not tried yet.
    """
    return stamp is None or _expired(stamp, STATION_MISS_TTL)


def _metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation - exact enough over a few kilometres."""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    x = math.radians(lon2 - lon1) * math.cos(mean_lat)
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6_371_000.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expired(stamp: str, ttl: timedelta) -> bool:
    try:
        fetched = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > ttl


def _next_weekday() -> date:
    """Timetables differ at weekends; always price the commute on a workday."""
    now = datetime.now()
    day = now.date()
    # After the evening rush there is no useful morning left today.
    if now.hour >= 20:
        day += timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun
        day += timedelta(days=1)
    return day
