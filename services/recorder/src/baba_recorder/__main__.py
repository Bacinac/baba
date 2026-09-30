from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from baba_core import setup_logging
from baba_core.runtime import run_service
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_recorder.config import RecorderConfig
from baba_recorder.supervisor import RecorderSupervisor

log = logging.getLogger("baba.recorder")


async def run(config: RecorderConfig) -> None:
    sup = RecorderSupervisor(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await sup.start()

    health = HealthMarker("baba", "recorder")

    async def _health_tick() -> None:
        while not stop.is_set():
            try:
                flowing, stale = await sup.recording_health()
            except Exception:
                # A health probe that itself errors must not mask a live loop;
                # treat as flowing but say so.
                log.exception("recorder health probe failed")
                flowing, stale = True, []
            if stale:
                log.warning(
                    "recorder: no fresh segments from camera(s) %s", ", ".join(sorted(stale))
                )
            # Withhold the marker only when the recorder produces NOTHING at all
            # (stuck) — a single dead camera stays loud in the log above without
            # restarting its still-recording peers.
            if flowing:
                health.touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)

    tick = spawn(_health_tick(), name="recorder-health", log=log)

    try:
        await stop.wait()
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick
        await sup.stop()


def main() -> None:
    setup_logging("recorder")
    config = RecorderConfig.from_env()
    run_service(run(config))


if __name__ == "__main__":
    main()
