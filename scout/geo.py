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
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx

from .models import Commute, Criteria, Listing

log = logging.getLogger(__name__)

API = "https://transport.opendata.ch/v1"
ROUTE_TTL = timedelta(days=30)
STATION_TTL = timedelta(days=365)  # stations do not move
# A *failed* lookup is not a fact about the world - it is usually a rate limit
# or a transient error. Caching those for a year would make one bad afternoon
# permanent, so misses expire quickly and get retried.
STATION_MISS_TTL = timedelta(hours=6)

WALK_METRES_PER_MIN = 80.0
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
        self._consecutive_failures = 0
        self.throttled = False

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
            log.warning("route %s -> %s failed: %s", origin, destination, exc)
            return None

        durations: list[tuple[int, int]] = []
        for conn_ in data.get("connections") or []:
            minutes = parse_duration(conn_.get("duration") or "")
            if minutes:
                durations.append((minutes, int(conn_.get("transfers") or 0)))
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
