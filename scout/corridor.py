"""The corridor map: every station between the two workplaces, priced.

The dashboard answers "is this flat any good?". This answers the question that
comes before it — *where should we even be looking?* — and it is a different
shape of question, because the answer is a map rather than a list.

Three things had to be true for it to work at all:

* **Station geometry must be free.** The timetable API only answers "nearest
  station to this point", so enumerating 350 stations that way would cost 350
  metered calls before pricing a single trip. SBB publish the register instead
  (`stations.py`), which is one unmetered paged GET.
* **Routing must be resumable.** Two legs per station is ~700 timetable calls,
  well past what opendata.ch tolerates in one sitting. So filling the map is a
  budgeted batch that can be run again: every answer lands in `route_cache`,
  shared with listing lookups, and a second click continues where the first
  stopped rather than starting over.
* **The order must be useful.** Stations are resolved nearest-the-line-first,
  so the map becomes readable along the Zurich <-> Basel axis after one batch
  instead of after all of them.

What the map then shows, per station, is both partners' door-to-station commute
and the gap between them — which is the number a couple actually argues about,
and the one no listing site will ever show you.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from typing import Any

import httpx

from . import scheduler, stations
from .db import connect, get_kv, load_criteria, load_settings, set_kv
from .geo import CommuteService, retry_due
from .models import Criteria
from .sources.base import make_client

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
#: Progress of the batch currently running, read by the page while it polls.
_progress: dict[str, Any] = {"active": False, "done": 0, "total": 0, "note": ""}


# --------------------------------------------------------------------------
# workplace anchors
# --------------------------------------------------------------------------


def _anchor_key(criteria: Criteria) -> str:
    return f"{criteria.workplace_a}|{criteria.workplace_b}"


def load_anchors(conn: sqlite3.Connection, criteria: Criteria) -> dict[str, dict]:
    """Coordinates of the two workplaces, if we have looked them up already.

    Stored against the workplace names so that renaming a workplace in the
    criteria discards the old position instead of quietly reusing it.
    """
    stored = get_kv(conn, "workplace_geo") or {}
    if stored.get("key") != _anchor_key(criteria):
        return {}
    return {leg: stored[leg] for leg in ("a", "b") if stored.get(leg)}


async def _resolve_anchors(
    conn: sqlite3.Connection, client: httpx.AsyncClient, criteria: Criteria
) -> dict[str, dict]:
    found: dict[str, Any] = {"key": _anchor_key(criteria)}
    for leg, name in (("a", criteria.workplace_a), ("b", criteria.workplace_b)):
        station = await stations.lookup(client, name)
        if station is not None:
            found[leg] = {"name": station.name, "lat": station.lat, "lon": station.lon}
    set_kv(conn, "workplace_geo", found)
    conn.commit()
    return {leg: found[leg] for leg in ("a", "b") if found.get(leg)}


# --------------------------------------------------------------------------
# reading the map
# --------------------------------------------------------------------------


def load_map(conn: sqlite3.Connection, criteria: Criteria) -> dict[str, Any]:
    """Everything the map pane renders, in one dict.

    Commute minutes come straight out of `route_cache` rather than a table of
    their own: it is already keyed by (station, destination, arrival time),
    which is exactly this query, and it means a station priced for a listing is
    a station already priced for the map.
    """
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.lat, s.lon, s.municipality, s.canton,
               ra.minutes AS minutes_a, rb.minutes AS minutes_b,
               ra.transfers AS transfers_a, rb.transfers AS transfers_b,
               ra.fetched_at AS tried_a, rb.fetched_at AS tried_b
        FROM corridor_station s
        LEFT JOIN route_cache ra
               ON ra.origin = s.name AND ra.destination = ? AND ra.arrive_by = ?
        LEFT JOIN route_cache rb
               ON rb.origin = s.name AND rb.destination = ? AND rb.arrive_by = ?
        ORDER BY s.name
        """,
        (
            criteria.workplace_a,
            criteria.arrive_by,
            criteria.workplace_b,
            criteria.arrive_by,
        ),
    ).fetchall()

    points = []
    for row in rows:
        a, b = row["minutes_a"], row["minutes_b"]
        points.append(
            {
                "id": row["id"],
                "name": row["name"],
                "lat": row["lat"],
                "lon": row["lon"],
                "municipality": row["municipality"] or "",
                "canton": row["canton"] or "",
                "a": a,
                "b": b,
                "transfers_a": row["transfers_a"],
                "transfers_b": row["transfers_b"],
                # Scheduling detail, stripped by `public_map` before it is sent
                # to the browser: is this leg worth asking about in this batch?
                "_due_a": a is None and retry_due(row["tried_a"]),
                "_due_b": b is None and retry_due(row["tried_b"]),
                "gap": abs(a - b) if a is not None and b is not None else None,
                "total": a + b if a is not None and b is not None else None,
                # The partner who has it worst — the honest summary of a place,
                # because a couple is only as settled as its longer commute.
                "worst": max(a, b) if a is not None and b is not None else None,
                "ok": (
                    a is not None
                    and b is not None
                    and a <= criteria.commute_a_max_min
                    and b <= criteria.commute_b_max_min
                    and (a + b) <= criteria.commute_total_max_min
                ),
            }
        )

    resolved = sum(1 for p in points if p["a"] is not None and p["b"] is not None)
    return {
        "points": points,
        "anchors": load_anchors(conn, criteria),
        "resolved": resolved,
        "total": len(points),
        "progress": dict(_progress),
        "criteria": {
            "label_a": criteria.label_a,
            "label_b": criteria.label_b,
            "workplace_a": criteria.workplace_a,
            "workplace_b": criteria.workplace_b,
            "arrive_by": criteria.arrive_by,
            "max_a": criteria.commute_a_max_min,
            "max_b": criteria.commute_b_max_min,
            "max_total": criteria.commute_total_max_min,
        },
    }


def public_map(conn: sqlite3.Connection, criteria: Criteria) -> dict[str, Any]:
    """`load_map` without the internal scheduling flags, for the browser."""
    data = load_map(conn, criteria)
    data["points"] = [
        {k: v for k, v in point.items() if not k.startswith("_")}
        for point in data["points"]
    ]
    return data


# --------------------------------------------------------------------------
# filling it in
# --------------------------------------------------------------------------


def _priority(points: list[dict], anchors: dict[str, dict]) -> list[dict]:
    """Nearest the workplace-to-workplace line first.

    With a budget that covers a fraction of the corridor, resolution order *is*
    the feature: pricing the axis first gives a map that reads as a corridor
    after one batch. Without both anchors there is no line to measure against,
    so alphabetical order at least stays predictable between runs.
    """
    a, b = anchors.get("a"), anchors.get("b")
    if not a or not b:
        return sorted(points, key=lambda p: p["name"])

    # Flat projection: at Swiss latitudes and this span the distortion is far
    # below anything that would reorder two stations.
    scale = math.cos(math.radians((a["lat"] + b["lat"]) / 2.0))
    ax, ay = a["lon"] * scale, a["lat"]
    bx, by = b["lon"] * scale, b["lat"]
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy

    def distance(point: dict) -> float:
        px, py = point["lon"] * scale, point["lat"]
        if length_sq == 0:
            return math.hypot(px - ax, py - ay)
        # Position along the segment, clamped so the ends behave like points.
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    return sorted(points, key=distance)


async def resolve_batch(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    criteria: Criteria,
    limit: int,
) -> dict[str, int]:
    """Price up to `limit` more stations, best-first. Safe to call again."""
    data = load_map(conn, criteria)
    anchors = data["anchors"] or await _resolve_anchors(conn, client, criteria)
    pending = [p for p in data["points"] if p["_due_a"] or p["_due_b"]]
    queue = _priority(pending, anchors)[:limit]

    _progress.update(active=True, done=0, total=len(queue), note="")
    commute = CommuteService(conn, client, criteria)
    resolved = 0

    try:
        for point in queue:
            if commute.exhausted:
                _progress["note"] = "both timetable services stopped answering"
                break
            for leg, destination in (
                ("a", criteria.workplace_a),
                ("b", criteria.workplace_b),
            ):
                if point[f"_due_{leg}"]:
                    await commute.route(point["name"], destination)
            _progress["done"] += 1
            resolved += 1
    finally:
        _progress["active"] = False

    log.info(
        "corridor: priced %d stations (%d opendata.ch, %d search.ch calls)",
        resolved,
        commute.api_calls,
        commute.searchch_calls,
    )
    return {
        "stations": resolved,
        "opendata_calls": commute.api_calls,
        "searchch_calls": commute.searchch_calls,
    }


async def refresh_stations(
    conn: sqlite3.Connection, client: httpx.AsyncClient, criteria: Criteria
) -> int:
    """(Re)load the station register for the current corridor bounds."""
    stations.prune_outside(conn, criteria)
    found = await stations.fetch_corridor(client, criteria)
    stations.save(conn, found)
    await _resolve_anchors(conn, client, criteria)
    return len(found)


# --------------------------------------------------------------------------
# background job, so the browser is not held open for several minutes
# --------------------------------------------------------------------------


def is_busy() -> bool:
    return _lock.locked()


def progress() -> dict[str, Any]:
    return dict(_progress)


async def _job(refresh: bool, batch: int) -> None:
    if _lock.locked():
        log.info("corridor: a batch is already running")
        return
    # A scouting run is pricing listings against the same rate-limited service.
    # Two rate limiters that do not know about each other are how you get
    # throttled, so the map waits rather than competing with the listings.
    if scheduler.is_running():
        _progress.update(active=False, note="waiting for the current scouting run")
        log.info("corridor: a scouting run is in progress; not starting a batch")
        return
    async with _lock:
        _progress.update(active=True, done=0, total=0, note="starting")
        try:
            async with make_client() as client:
                with connect() as conn:
                    criteria = load_criteria(conn)
                    if refresh:
                        _progress["note"] = "loading stations from SBB"
                        count = await refresh_stations(conn, client, criteria)
                        _progress["note"] = f"{count} stations"
                    await resolve_batch(conn, client, criteria, batch)
        except Exception as exc:
            log.exception("corridor batch failed")
            _progress["note"] = f"failed: {exc}"
        finally:
            _progress["active"] = False


async def trigger(refresh: bool = False, batch: int | None = None) -> None:
    """Kick off a batch in the background and return immediately."""
    if batch is None:
        with connect() as conn:
            batch = load_settings(conn).corridor_batch_size
    asyncio.create_task(_job(refresh, batch))
