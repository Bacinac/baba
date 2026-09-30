"""Periodic retention sweeper for notification_deliveries.

The deliveries audit table is append-only — every fire across every
channel inserts a row. At default rates (8 cameras × ~100 events/day
× a couple of channels per rule) that's a few hundred rows per day,
trivial. Operators with chatty zones or wide-fanout rules can easily
hit 10k+ rows per day. A simple "drop everything older than N days"
sweep keeps the table bounded without operator intervention.

Lives in api (not event-manager) because the dispatcher does too —
keeps the notification subsystem self-contained in one process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import asyncpg

log = logging.getLogger(__name__)


# Defaults pick a comfortable window for forensic queries without
# being so generous the table can't be VACUUMed cheaply.
_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_INTERVAL_S = 3600  # hourly


async def run_sweeper(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    retention_days = int(
        os.environ.get("BABA_DELIVERIES_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS),
    )
    interval_s = int(
        os.environ.get("BABA_DELIVERIES_SWEEP_INTERVAL_S", _DEFAULT_INTERVAL_S),
    )
    if retention_days <= 0:
        log.info("deliveries sweeper disabled (BABA_DELIVERIES_RETENTION_DAYS=0)")
        return

    # Small initial delay so the sweeper doesn't fight migrations or
    # the rules dispatcher's first burst of work right after startup.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=min(60, interval_s))

    while not stop.is_set():
        try:
            result = await pool.execute(
                "DELETE FROM notification_deliveries WHERE at < now() - make_interval(days => $1)",
                retention_days,
            )
            # asyncpg.execute returns "DELETE <n>" on success; parse the
            # count cheaply so the log line is informative when there's
            # something to report.
            n = 0
            if isinstance(result, str) and result.startswith("DELETE "):
                try:
                    n = int(result.split(" ", 1)[1])
                except ValueError:
                    n = 0
            if n > 0:
                log.info(
                    "deliveries sweeper: deleted %d rows older than %d days",
                    n,
                    retention_days,
                )
        except Exception:
            log.exception("deliveries sweeper: DELETE failed, will retry next tick")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
