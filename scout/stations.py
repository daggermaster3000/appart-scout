"""The corridor's train stations, straight from SBB's own open-data registry.

The timetable API (`geo.py`) can tell us how long a trip takes, but it cannot
tell us *which* stations exist: it only ever answers "what is nearest to this
point?", one rate-limited request at a time. Enumerating a whole corridor that
way would cost hundreds of calls before a single commute was priced.

SBB publish the authoritative list instead — the DIDOK service-point register,
mirrored on data.sbb.ch as an Opendatasoft dataset. It is a plain GET, needs no
key, is not metered like the timetable API, and carries exactly the four things
the map needs: official name, WGS84 position, municipality and canton. One
paged request per ~100 stations, cached in SQLite afterwards, and the station
geometry never has to be fetched again.

The names it returns are the official ones, which is what the timetable API
expects as `from`/`to` — so a station discovered here routes without any
translation step, and shares `route_cache` rows with listing lookups.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

import httpx
from pydantic import BaseModel

from .db import now_iso
from .models import Criteria

log = logging.getLogger(__name__)

DATASET = (
    "https://data.sbb.ch/api/explore/v2.1/catalog/datasets/"
    "dienststellen-gemass-opentransportdataswiss/records"
)
#: Opendatasoft caps `limit` at 100.
PAGE_SIZE = 100
#: Enough for a whole canton group; a guard against paging forever if the
#: filter is ever loosened by accident.
MAX_PAGES = 40

FIELDS = "number,designationofficial,geopos,municipalityname,cantonabbreviation"


class Station(BaseModel):
    id: int
    name: str
    lat: float
    lon: float
    municipality: str = ""
    canton: str = ""


def _where(criteria: Criteria) -> str:
    """ODQL filter: passenger train stops, still valid, inside the corridor.

    `stoppoint` excludes junctions and depots that have a DIDOK number but no
    platform. `meansoftransport LIKE "TRAIN"` keeps bus and boat stops out — the
    field is a list, so `LIKE` is the membership test here, not a wildcard. The
    bounding box is the same one that gates listings, so the map covers exactly
    the area the search does.
    """
    clauses = [
        'stoppoint="true"',
        'meansoftransport LIKE "TRAIN"',
        f'validto>="{date.today().isoformat()}"',
        (
            f"in_bbox(geopos, {criteria.lat_min}, {criteria.lon_min}, "
            f"{criteria.lat_max}, {criteria.lon_max})"
        ),
    ]
    if criteria.cantons:
        codes = ", ".join(f'"{c.strip().upper()}"' for c in criteria.cantons if c.strip())
        if codes:
            clauses.append(f"cantonabbreviation in ({codes})")
    return " and ".join(clauses)


def _to_station(record: dict) -> Station | None:
    pos = record.get("geopos") or {}
    lat, lon = pos.get("lat"), pos.get("lon")
    name = (record.get("designationofficial") or "").strip()
    if not name or lat is None or lon is None or record.get("number") is None:
        return None
    return Station(
        id=int(record["number"]),
        name=name,
        lat=float(lat),
        lon=float(lon),
        municipality=(record.get("municipalityname") or "").strip(),
        canton=(record.get("cantonabbreviation") or "").strip(),
    )


async def fetch_corridor(client: httpx.AsyncClient, criteria: Criteria) -> list[Station]:
    """Every train station inside the corridor, deduplicated by DIDOK number."""
    where = _where(criteria)
    found: dict[int, Station] = {}

    for page in range(MAX_PAGES):
        resp = await client.get(
            DATASET,
            params={
                "select": FIELDS,
                "where": where,
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "order_by": "number",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        records = resp.json().get("results") or []
        for record in records:
            station = _to_station(record)
            if station is not None:
                found[station.id] = station
        if len(records) < PAGE_SIZE:
            break

    log.info("SBB registry: %d train stations in the corridor", len(found))
    return sorted(found.values(), key=lambda s: s.name)


async def lookup(client: httpx.AsyncClient, name: str) -> Station | None:
    """Locate one station by its official name — used for the two workplaces.

    The workplaces are free text the user typed, so an exact match is tried
    first and a prefix match second; anything looser starts matching the wrong
    town entirely ("Basel" would happily return "Basel Dreispitz").
    """
    clean = (name or "").strip().replace('"', "")
    if not clean:
        return None
    for clause in (
        f'designationofficial="{clean}"',
        f'startswith(designationofficial, "{clean}")',
    ):
        resp = await client.get(
            DATASET,
            params={
                "select": FIELDS,
                "where": f'{clause} and stoppoint="true" and meansoftransport LIKE "TRAIN"',
                "limit": 1,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        for record in resp.json().get("results") or []:
            station = _to_station(record)
            if station is not None:
                return station
    return None


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def save(conn: sqlite3.Connection, stations: list[Station]) -> int:
    stamp = now_iso()
    for station in stations:
        conn.execute(
            "INSERT INTO corridor_station (id, name, lat, lon, municipality, canton, "
            "fetched_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, lat = excluded.lat, "
            "lon = excluded.lon, municipality = excluded.municipality, "
            "canton = excluded.canton, fetched_at = excluded.fetched_at",
            (
                station.id,
                station.name,
                station.lat,
                station.lon,
                station.municipality,
                station.canton,
                stamp,
            ),
        )
    conn.commit()
    return len(stations)


def load(conn: sqlite3.Connection) -> list[Station]:
    rows = conn.execute(
        "SELECT id, name, lat, lon, municipality, canton FROM corridor_station ORDER BY name"
    ).fetchall()
    return [Station(**dict(row)) for row in rows]


def prune_outside(conn: sqlite3.Connection, criteria: Criteria) -> int:
    """Drop stations the corridor no longer covers after a criteria edit."""
    cur = conn.execute(
        "DELETE FROM corridor_station WHERE lat < ? OR lat > ? OR lon < ? OR lon > ?",
        (criteria.lat_min, criteria.lat_max, criteria.lon_min, criteria.lon_max),
    )
    conn.commit()
    return cur.rowcount
