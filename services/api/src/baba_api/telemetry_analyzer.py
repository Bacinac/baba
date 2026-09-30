"""Auto-analyzer for telemetry incidents.

When the operator flips the "Automatska AI analiza" switch (app_settings
key `telemetry_auto_analyze`), this loop picks up incidents the watchers
opened and runs the same AI verdict the manual "AI analiza" button does —
evidence + 30-min telemetry summary + live snapshot → verdict/suggestion,
incident moves open → analyzed. Apply stays manual: the AI never changes
camera settings on its own.

Each analysis is one paid vision call, so the loop is deliberately tame:
one pass a minute, oldest-first, capped per pass. Watcher cooldowns bound
the incident rate (one open per camera+kind, 24 h reopen cooldown), which
bounds spend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import asyncpg
from fastapi import HTTPException

from baba_api.routes_telemetry import (
    _INCIDENT_COLS,
    auto_analyze_enabled,
    run_incident_analysis,
)

log = logging.getLogger(__name__)

_INTERVAL_S = 60
# Cost guard, not a queue limit — anything left over is picked up next pass.
_MAX_PER_PASS = 3


async def run_auto_analyzer(
    pool: asyncpg.Pool,
    *,
    secret_key: str,
    go2rtc_url: str,
    go2rtc_auth: tuple[str, str],
    stop: asyncio.Event,
) -> None:
    misconfigured_warned = False
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_INTERVAL_S)
        if stop.is_set():
            return
        try:
            if not await auto_analyze_enabled(pool):
                misconfigured_warned = False
                continue
            rows = await pool.fetch(
                f"""
                SELECT {_INCIDENT_COLS} FROM telemetry_incidents
                WHERE status = 'open' AND verdict IS NULL
                ORDER BY opened_at
                LIMIT $1
                """,  # noqa: S608
                _MAX_PER_PASS,
            )
            for incident in rows:
                try:
                    out = await run_incident_analysis(
                        pool,
                        secret_key=secret_key,
                        go2rtc_url=go2rtc_url,
                        go2rtc_auth=go2rtc_auth,
                        incident=incident,
                    )
                except HTTPException as e:
                    # 400 = no AI provider configured — a standing operator
                    # condition, warn once instead of every minute; anything
                    # else (snapshot 502, camera gone) is per-incident.
                    if e.status_code == 400:
                        if not misconfigured_warned:
                            log.warning(
                                "auto-analyze is ON but unusable: %s", e.detail
                            )
                            misconfigured_warned = True
                        break
                    log.warning(
                        "auto-analyze failed for %s/%s: %s",
                        incident["camera_slug"],
                        incident["kind"],
                        e.detail,
                    )
                    continue
                misconfigured_warned = False
                if out.error:
                    log.warning(
                        "auto-analyze: model call failed for %s/%s: %s",
                        incident["camera_slug"],
                        incident["kind"],
                        out.error,
                    )
                else:
                    log.info(
                        "auto-analyzed %s/%s (%s, %d ms)",
                        incident["camera_slug"],
                        incident["kind"],
                        out.model,
                        out.latency_ms,
                    )
        except Exception:
            log.exception("auto-analyzer pass failed")
