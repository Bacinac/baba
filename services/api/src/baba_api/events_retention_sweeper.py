"""Periodic retention sweeper for the events table.

Every zone enter/exit/dwell, track_finalized, object_parked/unparked,
scene_state_change and doorbell_press appends a row. Nothing pruned events
before this sweeper — for ~8 cameras that is millions of rows/year accumulating
forever on the latency-sensitive Postgres state tier, and (because the
events.track_id FK was intentionally dropped in migration 035) a track_finalized
event OUTLIVES its track when the tracks retention sweeper deletes the track +
its thumbnail at 30/60/90 days — leaving the Activity/Events feed rendering a
row with a now-missing thumbnail indefinitely.

Two passes each tick:
  1. Age: drop events older than BABA_EVENTS_RETENTION_DAYS (default 90 — the
     longest track retention tier, so events don't long outlive their media).
  2. Tombstones: drop events whose track_id no longer resolves. Gated to rows
     older than a day so an in-flight event (zone events carry a pre-allocated
     db_track_id before the track row exists — migration 035) is never touched;
     a real tombstone only appears once a track is retention-deleted (30d+).

Lives in api alongside the other sweepers (deliveries, tracks) so retention is
owned in one process. Disable with BABA_EVENTS_RETENTION_DAYS=0.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import asyncpg

log = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_INTERVAL_S = 3600  # hourly — deadlines are day-level


def _deleted_count(result: str) -> int:
    if isinstance(result, str) and result.startswith("DELETE "):
        with contextlib.suppress(ValueError):
            return int(result.split(" ", 1)[1])
    return 0


async def run_sweeper(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    retention_days = int(
        os.environ.get("BABA_EVENTS_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS),
    )
    interval_s = int(
        os.environ.get("BABA_EVENTS_SWEEP_INTERVAL_S", _DEFAULT_INTERVAL_S),
    )
    if retention_days <= 0:
        log.info("events sweeper disabled (BABA_EVENTS_RETENTION_DAYS=0)")
        return

    # Small initial delay so we don't fight migrations / startup bursts.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=min(60, interval_s))

    while not stop.is_set():
        try:
            aged = _deleted_count(
                await pool.execute(
                    "DELETE FROM events WHERE at < now() - make_interval(days => $1)",
                    retention_days,
                )
            )
            # Tombstones: track_id set but the track row is gone (retention
            # already pruned it). Older-than-a-day guard keeps in-flight events
            # whose track row hasn't been created yet safe.
            tombstoned = _deleted_count(
                await pool.execute(
                    "DELETE FROM events e "
                    "WHERE e.track_id IS NOT NULL "
                    "  AND e.at < now() - interval '1 day' "
                    "  AND NOT EXISTS (SELECT 1 FROM tracks_all t WHERE t.id = e.track_id)"
                )
            )
            if aged or tombstoned:
                log.info(
                    "events sweeper: deleted %d aged (>%dd) + %d tombstoned event(s)",
                    aged,
                    retention_days,
                    tombstoned,
                )
        except Exception:
            log.exception("events sweeper: DELETE failed, will retry next tick")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
