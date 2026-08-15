"""One end-to-end run: fetch -> merge -> filter -> commute -> score -> photos -> notify."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

from . import store
from .browser import BrowserUnavailable, browser_session
from .config import get_config
from .db import (
    connect,
    criteria_version,
    get_cursor,
    load_criteria,
    load_settings,
    now_iso,
    set_cursor,
)
from .dedup import merge
from .geo import CommuteService
from .models import Criteria, Listing, Settings
from .notify.email import EmailSender, render_digest, render_text
from .scoring import passes_filters, score
from .sources.base import make_client
from .sources.registry import load_sources
from .vision import VisionScorer

log = logging.getLogger(__name__)

#: A source that hangs must not hold up the run.
SOURCE_TIMEOUT = 600.0


async def run_once(
    *,
    send_email: bool = True,
    use_vision: bool = True,
    only_sources: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a full scouting run. Returns a stats dict."""
    with connect() as conn:
        criteria = load_criteria(conn)
        settings = load_settings(conn)
        version = criteria_version(conn)
        run_id = store.start_run(conn)

    names = only_sources or settings.enabled_sources
    sources = load_sources(names)
    stats: dict = {"sources": {}, "fetched": 0, "merged": 0, "kept": 0, "new": 0, "vision": 0}
    started = now_iso()

    try:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(make_client())

            # Only pay the cost of a browser if something actually needs one.
            if any(s.needs_browser for s in sources):
                try:
                    session = await stack.enter_async_context(browser_session())
                    for source in sources:
                        if source.needs_browser:
                            source.session = session
                except BrowserUnavailable as exc:
                    log.warning("browser unavailable: %s", exc)
                    with connect() as conn:
                        for source in sources:
                            if source.needs_browser:
                                store.record_source_run(
                                    conn, run_id, source.name, error=f"browser unavailable: {exc}"
                                )
                    sources = [s for s in sources if not s.needs_browser]

            collected = await _fetch_all(conn_run_id=run_id, sources=sources,
                                         client=client, criteria=criteria,
                                         settings=settings, stats=stats)

            merged = merge(collected)
            stats["merged"] = len(merged)

            with connect() as conn:
                commute = CommuteService(conn, client, criteria)
                new_ids = _persist(conn, merged, stats)
                kept_ids = await _rank(
                    conn, criteria, settings, commute, version, stats
                )

                if use_vision and settings.vision_enabled:
                    stats["vision"] = await _run_vision(
                        conn, client, criteria, settings, version, commute
                    )

                cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                stats["deactivated"] = store.deactivate_stale(conn, cutoff)
                stats["commute_api_calls"] = commute.api_calls
                stats["kept"] = len(kept_ids)
                stats["new"] = len(new_ids)

            if send_email and not dry_run:
                stats["emailed"] = _notify(settings, criteria)

        with connect() as conn:
            store.finish_run(conn, run_id, True, stats)
    except Exception as exc:
        log.exception("run failed")
        with connect() as conn:
            store.finish_run(conn, run_id, False, stats, error=str(exc))
        raise

    stats["run_id"] = run_id
    stats["started"] = started
    return stats


# --------------------------------------------------------------------------


async def _fetch_all(*, conn_run_id: int, sources, client, criteria, settings, stats) -> list[Listing]:
    """Fetch every source, isolating failures so one dead portal is not fatal."""
    collected: list[Listing] = []

    for source in sources:
        began = time.monotonic()
        with connect() as conn:
            raw_state = get_cursor(conn, source.name, "state")
        state = json.loads(raw_state) if raw_state else {}

        try:
            listings, new_state = await asyncio.wait_for(
                source.fetch(client, criteria, settings, state), timeout=SOURCE_TIMEOUT
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - began) * 1000)
            message = f"{type(exc).__name__}: {exc}"[:400]
            log.warning("source %s failed: %s", source.name, message)
            with connect() as conn:
                store.record_source_run(
                    conn, conn_run_id, source.name, duration_ms=elapsed, error=message
                )
            stats["sources"][source.name] = {"error": message}
            continue

        elapsed = int((time.monotonic() - began) * 1000)
        collected.extend(listings)
        stats["fetched"] += len(listings)
        stats["sources"][source.name] = {"fetched": len(listings), "ms": elapsed}
        with connect() as conn:
            store.record_source_run(
                conn, conn_run_id, source.name, n_fetched=len(listings), duration_ms=elapsed
            )
            set_cursor(conn, source.name, "state", json.dumps(new_state))

    return collected


def _persist(conn, merged, stats) -> list[str]:
    new_ids = []
    for item in merged:
        listing_id, is_new = store.upsert_listing(conn, item)
        if is_new:
            new_ids.append(listing_id)
    return new_ids


async def _rank(conn, criteria: Criteria, settings: Settings, commute, version, stats) -> list[str]:
    """Filter, resolve commutes for survivors, then score on metadata."""
    kept: list[str] = []
    dropped = 0

    for listing_id, listing in store.active_listings(conn, settings.max_listing_age_days):
        ok, _reason = passes_filters(listing, criteria)
        if not ok:
            store.drop_score(conn, listing_id)
            dropped += 1
            continue

        legs = store.load_commutes(conn, listing_id)
        if (
            not legs
            and not commute.throttled
            and commute.api_calls < settings.max_commute_calls_per_run
        ):
            legs = await commute.commutes(listing)
            store.save_commutes(conn, listing_id, legs)

        # A listing whose commute is not resolved yet is scored on metadata
        # alone rather than dropped; the next run fills it in.
        ok, _reason = passes_filters(listing, criteria, legs)
        if not ok:
            store.drop_score(conn, listing_id)
            dropped += 1
            continue

        vision = store.load_vision(conn, listing_id)
        store.save_score(conn, listing_id, score(listing, criteria, legs, vision), version)
        kept.append(listing_id)

    conn.commit()
    stats["dropped"] = dropped
    return kept


async def _run_vision(conn, client, criteria, settings, version, commute) -> int:
    """Photo-score the top N candidates that have never been photographed."""
    scorer = VisionScorer(settings=settings)
    if not scorer.available:
        log.info("vision skipped: no OPENAI_API_KEY configured")
        return 0

    candidates = store.vision_candidates(
        conn, limit=settings.vision_top_n, min_score=settings.vision_min_score
    )
    if not candidates:
        log.info(
            "vision skipped: nothing scoring %.0f+ with both commutes resolved and "
            "photos it has not already seen",
            settings.vision_min_score,
        )
        return 0

    scored = 0
    for item in candidates:
        listing = item["listing"]
        result, n_photos = await scorer.score_listing(
            client, listing, criteria, settings.vision_max_photos
        )
        if result is None:
            continue
        store.save_vision(conn, item["id"], scorer.model, result, n_photos)
        legs = store.load_commutes(conn, item["id"])
        store.save_score(conn, item["id"], score(listing, criteria, legs, result), version)
        scored += 1

    conn.commit()
    return scored


def _notify(settings: Settings, criteria: Criteria) -> int:
    """Send instant alerts and, when due, the periodic digest."""
    sender = EmailSender(get_config().resolve(settings))
    if not sender.available:
        log.info("email skipped: SMTP not configured")
        return 0
    if not settings.recipients:
        log.info("email skipped: no recipients configured")
        return 0

    sent = 0
    with connect() as conn:
        if settings.instant_alert_enabled:
            urgent = store.unnotified(
                conn, "instant", limit=5, min_score=settings.instant_alert_min_score
            )
            if urgent:
                subject, html = render_digest(urgent, criteria, subject_prefix="Strong match")
                sender.send(settings.recipients, subject, html, render_text(urgent, criteria))
                for item in urgent:
                    store.mark_notified(conn, item["id"], "instant", item["score"])
                    store.mark_notified(conn, item["id"], "digest", item["score"])
                sent += len(urgent)

        if _digest_due(conn, settings):
            items = store.unnotified(conn, "digest", limit=settings.digest_size)
            if items or settings.send_when_empty:
                subject, html = render_digest(items, criteria)
                sender.send(settings.recipients, subject, html, render_text(items, criteria))
                for item in items:
                    store.mark_notified(conn, item["id"], "digest", item["score"])
                _mark_digest_sent(conn)
                sent += len(items)

    return sent


def _digest_due(conn, settings: Settings) -> bool:
    from .db import get_kv

    last = get_kv(conn, "last_digest_at")
    if not last:
        return True
    try:
        sent_at = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - sent_at >= timedelta(days=settings.digest_every_days)


def _mark_digest_sent(conn) -> None:
    from .db import set_kv

    set_kv(conn, "last_digest_at", now_iso())
