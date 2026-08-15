"""Read/write helpers on top of `db.py`.

Keeps SQL out of the pipeline and the web layer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import now_iso
from .dedup import MergedListing
from .models import Commute, Image, Listing, ScoreBreakdown, VisionResult


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------


def upsert_listing(conn: sqlite3.Connection, merged: MergedListing) -> tuple[str, bool]:
    """Insert or refresh a merged listing. Returns (listing_id, is_new)."""
    listing = merged.merged()
    listing_id = _resolve_id(conn, merged)
    stamp = now_iso()

    existing = conn.execute(
        "SELECT id, first_seen FROM listing WHERE id = ?", (listing_id,)
    ).fetchone()
    is_new = existing is None

    conn.execute(
        """
        INSERT INTO listing (
            id, dedup_key, title, description, price_chf, price_net_chf, charges_chf,
            rooms, living_space_m2, floor, street, zipcode, city, lat, lon,
            available_from, category, is_furnished, is_temporary, year_built,
            year_renovated, amenities, images, published, first_seen, last_seen, active
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            price_chf = excluded.price_chf,
            price_net_chf = excluded.price_net_chf,
            charges_chf = excluded.charges_chf,
            rooms = excluded.rooms,
            living_space_m2 = excluded.living_space_m2,
            floor = excluded.floor,
            street = excluded.street,
            zipcode = excluded.zipcode,
            city = excluded.city,
            lat = COALESCE(excluded.lat, listing.lat),
            lon = COALESCE(excluded.lon, listing.lon),
            available_from = excluded.available_from,
            amenities = excluded.amenities,
            images = CASE WHEN json_array_length(excluded.images) >
                               json_array_length(listing.images)
                          THEN excluded.images ELSE listing.images END,
            last_seen = excluded.last_seen,
            active = 1
        """,
        (
            listing_id,
            _dedup_key_of(listing),
            listing.title,
            listing.description,
            listing.price_chf,
            listing.price_net_chf,
            listing.charges_chf,
            listing.rooms,
            listing.living_space_m2,
            listing.floor,
            listing.street,
            listing.zipcode,
            listing.city,
            listing.lat,
            listing.lon,
            listing.available_from.isoformat() if listing.available_from else None,
            listing.category,
            int(listing.is_furnished),
            int(listing.is_temporary),
            listing.year_built,
            listing.year_renovated,
            json.dumps(listing.amenities),
            json.dumps([i.model_dump() for i in listing.images]),
            listing.published.isoformat() if listing.published else None,
            stamp,
            stamp,
        ),
    )

    for source_listing in merged.sources:
        conn.execute(
            "INSERT INTO listing_source (listing_id, source, source_id, url, last_seen) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(source, source_id) DO UPDATE SET "
            "listing_id = excluded.listing_id, url = excluded.url, "
            "last_seen = excluded.last_seen",
            (
                listing_id,
                source_listing.source,
                source_listing.source_id,
                source_listing.url,
                stamp,
            ),
        )
    return listing_id, is_new


def _dedup_key_of(listing: Listing) -> str:
    from .normalize import dedup_key

    return dedup_key(listing)


def _resolve_id(conn: sqlite3.Connection, merged: MergedListing) -> str:
    """Reuse the id we already assigned this flat, if any.

    A listing whose price was reduced would otherwise hash to a new id and
    reappear as "new". Matching on the portal's own id first keeps it stable.
    """
    for source_listing in merged.sources:
        row = conn.execute(
            "SELECT listing_id FROM listing_source WHERE source = ? AND source_id = ?",
            (source_listing.source, source_listing.source_id),
        ).fetchone()
        if row:
            return row["listing_id"]
    return merged.id


def row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source="merged",
        source_id=row["id"],
        url="",
        title=row["title"] or "",
        description=row["description"] or "",
        price_chf=row["price_chf"],
        price_net_chf=row["price_net_chf"],
        charges_chf=row["charges_chf"],
        rooms=row["rooms"],
        living_space_m2=row["living_space_m2"],
        floor=row["floor"],
        street=row["street"] or "",
        zipcode=row["zipcode"],
        city=row["city"] or "",
        lat=row["lat"],
        lon=row["lon"],
        available_from=_as_date(row["available_from"]),
        category=row["category"] or "APARTMENT",
        is_furnished=bool(row["is_furnished"]),
        is_temporary=bool(row["is_temporary"]),
        year_built=row["year_built"],
        year_renovated=row["year_renovated"],
        amenities=json.loads(row["amenities"] or "[]"),
        images=[Image(**i) for i in json.loads(row["images"] or "[]")],
        published=_as_dt(row["published"]),
    )


def _as_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _as_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def active_listings(conn: sqlite3.Connection, max_age_days: int = 45) -> list[tuple[str, Listing]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM listing WHERE active = 1 AND COALESCE(published, first_seen) >= ? "
        "ORDER BY COALESCE(published, first_seen) DESC",
        (cutoff,),
    ).fetchall()
    return [(r["id"], row_to_listing(r)) for r in rows]


def sources_for(conn: sqlite3.Connection, listing_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT source, url FROM listing_source WHERE listing_id = ? ORDER BY source",
        (listing_id,),
    ).fetchall()
    return [{"source": r["source"], "url": r["url"]} for r in rows]


def deactivate_stale(conn: sqlite3.Connection, before: str) -> int:
    cur = conn.execute(
        "UPDATE listing SET active = 0 WHERE active = 1 AND last_seen < ?", (before,)
    )
    return cur.rowcount


# --------------------------------------------------------------------------
# commute / score / vision
# --------------------------------------------------------------------------


def save_commutes(
    conn: sqlite3.Connection, listing_id: str, legs: dict[str, Commute | None]
) -> None:
    for leg, commute in legs.items():
        conn.execute(
            "INSERT INTO commute (listing_id, leg, minutes, transfers, origin, computed_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(listing_id, leg) DO UPDATE SET minutes = excluded.minutes, "
            "transfers = excluded.transfers, origin = excluded.origin, "
            "computed_at = excluded.computed_at",
            (
                listing_id,
                leg,
                commute.minutes if commute else None,
                commute.transfers if commute else None,
                commute.origin_station if commute else None,
                now_iso(),
            ),
        )


def load_commutes(conn: sqlite3.Connection, listing_id: str) -> dict[str, Commute | None]:
    rows = conn.execute(
        "SELECT leg, minutes, transfers, origin FROM commute WHERE listing_id = ?",
        (listing_id,),
    ).fetchall()
    legs: dict[str, Commute | None] = {}
    for row in rows:
        legs[row["leg"]] = (
            Commute(
                minutes=row["minutes"],
                transfers=row["transfers"] or 0,
                origin_station=row["origin"] or "",
            )
            if row["minutes"] is not None
            else None
        )
    return legs


def drop_score(conn: sqlite3.Connection, listing_id: str) -> None:
    """Remove a stale score.

    A listing scored on an earlier run (before its commute was known) can fail
    the filters once the timetable data arrives. The score row has to go with
    it, or the ranking keeps serving a flat that no longer qualifies.
    """
    conn.execute("DELETE FROM score WHERE listing_id = ?", (listing_id,))


def save_score(
    conn: sqlite3.Connection, listing_id: str, breakdown: ScoreBreakdown, criteria_v: int
) -> None:
    conn.execute(
        "INSERT INTO score (listing_id, total, parts, reasons, criteria_v, computed_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(listing_id) DO UPDATE SET total = excluded.total, "
        "parts = excluded.parts, reasons = excluded.reasons, "
        "criteria_v = excluded.criteria_v, computed_at = excluded.computed_at",
        (
            listing_id,
            breakdown.total,
            json.dumps(breakdown.parts),
            json.dumps(breakdown.reasons),
            criteria_v,
            now_iso(),
        ),
    )


def save_vision(
    conn: sqlite3.Connection,
    listing_id: str,
    model: str,
    result: VisionResult,
    n_photos: int,
) -> None:
    conn.execute(
        "INSERT INTO vision (listing_id, model, score, result, n_photos, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(listing_id) DO UPDATE SET model = excluded.model, "
        "score = excluded.score, result = excluded.result, "
        "n_photos = excluded.n_photos, created_at = excluded.created_at",
        (listing_id, model, result.score, result.model_dump_json(), n_photos, now_iso()),
    )


def load_vision(conn: sqlite3.Connection, listing_id: str) -> VisionResult | None:
    row = conn.execute(
        "SELECT result FROM vision WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if not row:
        return None
    try:
        return VisionResult(**json.loads(row["result"]))
    except (ValueError, TypeError):
        return None


def vision_scored_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["listing_id"] for r in conn.execute("SELECT listing_id FROM vision")}


# --------------------------------------------------------------------------
# ranking / notification bookkeeping
# --------------------------------------------------------------------------


def ranked(
    conn: sqlite3.Connection,
    limit: int = 100,
    include_hidden: bool = False,
    min_score: float = 0.0,
    listing_id: str | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT l.*, s.total AS score, s.parts, s.reasons,
               f.verdict AS verdict,
               ca.minutes AS commute_a, cb.minutes AS commute_b,
               ca.origin AS commute_a_from, cb.origin AS commute_b_from,
               ca.transfers AS commute_a_transfers, cb.transfers AS commute_b_transfers,
               v.result AS vision, v.model AS vision_model, v.n_photos AS vision_photos
        FROM listing l
        JOIN score s ON s.listing_id = l.id
        LEFT JOIN feedback f ON f.listing_id = l.id
        LEFT JOIN commute ca ON ca.listing_id = l.id AND ca.leg = 'a'
        LEFT JOIN commute cb ON cb.listing_id = l.id AND cb.leg = 'b'
        LEFT JOIN vision v ON v.listing_id = l.id
        WHERE l.active = 1 AND s.total >= ?
    """
    params: list[Any] = [min_score]
    # The detail page asks for one listing by id; everything else lists.
    if listing_id is not None:
        sql += " AND l.id = ?"
        params.append(listing_id)
    if not include_hidden:
        sql += " AND (f.verdict IS NULL OR f.verdict NOT IN ('hidden', 'down'))"
    sql += " ORDER BY s.total DESC LIMIT ?"
    params.append(limit)

    out = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["listing"] = row_to_listing(row)
        item["parts"] = json.loads(row["parts"] or "{}")
        item["reasons"] = json.loads(row["reasons"] or "[]")
        item["vision"] = json.loads(row["vision"]) if row["vision"] else None
        item["sources"] = sources_for(conn, row["id"])
        out.append(item)
    return out


def get_ranked(conn: sqlite3.Connection, listing_id: str) -> dict[str, Any] | None:
    """One listing in the same shape `ranked` returns, for the detail page."""
    rows = ranked(conn, limit=1, include_hidden=True, listing_id=listing_id)
    return rows[0] if rows else None


def vision_candidates(
    conn: sqlite3.Connection, limit: int, min_score: float
) -> list[dict[str, Any]]:
    """Listings worth spending an OpenAI call on, best first.

    Three gates, all of them about not paying to photograph a flat that was
    never going to make the digest:

    * it scores at least `min_score` on the metrics that cost nothing;
    * both commutes are resolved — until then the score is price and size only,
      and cheap roomy places an hour outside the corridor sit at the top;
    * it has photos, and has not been photographed before (results are cached
      forever, so this is once per listing, not once per run).
    """
    already = vision_scored_ids(conn)
    return [
        item
        for item in ranked(conn, limit=limit * 10, min_score=min_score)
        if item["id"] not in already
        and item["listing"].images
        and item.get("commute_a") is not None
        and item.get("commute_b") is not None
    ][:limit]


def unnotified(conn: sqlite3.Connection, kind: str, limit: int, min_score: float = 0.0):
    """Top-ranked listings we have not emailed under this notification kind.

    Only listings whose commute is actually resolved are eligible. Until that
    lookup happens a flat is scored on price and size alone, which floats cheap,
    roomy places well outside the corridor to the top - emailing those and then
    silently dropping them the next day is worse than waiting a few hours.
    """
    rows = ranked(conn, limit=500, min_score=min_score)
    sent = {
        r["listing_id"]
        for r in conn.execute("SELECT listing_id FROM notified WHERE kind = ?", (kind,))
    }
    return [
        r
        for r in rows
        if r["id"] not in sent
        and r.get("commute_a") is not None
        and r.get("commute_b") is not None
    ][:limit]


def mark_notified(conn: sqlite3.Connection, listing_id: str, kind: str, score: float) -> None:
    conn.execute(
        "INSERT INTO notified (listing_id, kind, score, sent_at) VALUES (?,?,?,?) "
        "ON CONFLICT(listing_id, kind) DO UPDATE SET score = excluded.score, "
        "sent_at = excluded.sent_at",
        (listing_id, kind, score, now_iso()),
    )


def set_feedback(conn: sqlite3.Connection, listing_id: str, verdict: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO feedback (listing_id, verdict, note, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(listing_id) DO UPDATE SET verdict = excluded.verdict, "
        "note = excluded.note, created_at = excluded.created_at",
        (listing_id, verdict, note, now_iso()),
    )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO run (started_at) VALUES (?)", (now_iso(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, ok: bool, stats: dict, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE run SET finished_at = ?, ok = ?, stats = ?, error = ? WHERE id = ?",
        (now_iso(), int(ok), json.dumps(stats, default=str), error, run_id),
    )


def record_source_run(
    conn: sqlite3.Connection,
    run_id: int,
    source: str,
    n_fetched: int = 0,
    n_kept: int = 0,
    n_new: int = 0,
    duration_ms: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO run_source (run_id, source, n_fetched, n_kept, n_new, duration_ms, error) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id, source) DO UPDATE SET n_fetched = excluded.n_fetched, "
        "n_kept = excluded.n_kept, n_new = excluded.n_new, "
        "duration_ms = excluded.duration_ms, error = excluded.error",
        (run_id, source, n_fetched, n_kept, n_new, duration_ms, error),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    runs = []
    for row in conn.execute(
        "SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall():
        run = dict(row)
        run["stats"] = json.loads(row["stats"] or "{}")
        run["sources"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM run_source WHERE run_id = ? ORDER BY source", (row["id"],)
            ).fetchall()
        ]
        runs.append(run)
    return runs
