"""Background scheduling.

Runs inside the same process as the web UI - on a Pi there is no reason to
operate a second service or a cron entry just to call one coroutine.

Scraping cadence and digest cadence are deliberately separate. Scraping every
few hours catches a good flat while it is still available (and feeds instant
alerts); the digest lands every few days so the inbox stays readable.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .db import connect, load_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_run_lock = asyncio.Lock()

JOB_ID = "scout-run"


async def scheduled_run() -> None:
    """The scheduled job. Never lets two runs overlap."""
    if _run_lock.locked():
        log.info("skipping scheduled run: one is already in progress")
        return
    async with _run_lock:
        from .pipeline import run_once

        try:
            stats = await run_once()
            log.info("scheduled run finished: %s", stats)
        except Exception:
            log.exception("scheduled run failed")


def is_running() -> bool:
    return _run_lock.locked()


async def trigger_now() -> None:
    """Fire a run in the background, e.g. from the 'Run now' button."""
    asyncio.create_task(scheduled_run())


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    with connect() as conn:
        settings = load_settings(conn)

    _scheduler = AsyncIOScheduler(timezone="Europe/Zurich")
    _scheduler.add_job(
        scheduled_run,
        IntervalTrigger(hours=max(1, settings.run_every_hours)),
        id=JOB_ID,
        # A Pi that was asleep should do one run, not queue up the ones it missed.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    log.info("scheduler started: every %d hours", settings.run_every_hours)
    return _scheduler


def reschedule(hours: int) -> None:
    """Apply a cadence change made in the UI without a restart."""
    if _scheduler is None:
        return
    _scheduler.reschedule_job(JOB_ID, trigger=IntervalTrigger(hours=max(1, hours)))
    log.info("scheduler rescheduled: every %d hours", hours)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None
