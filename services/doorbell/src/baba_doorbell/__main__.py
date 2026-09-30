from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from baba_core import setup_logging
from baba_core.runtime import run_service
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_doorbell.config import DoorbellConfig
from baba_doorbell.subscriber import DoorbellSubscriber

log = logging.getLogger("baba.doorbell")


async def run(config: DoorbellConfig) -> None:
    sub = DoorbellSubscriber(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await sub.start()

    tick = spawn(HealthMarker("baba", "doorbell").run_loop(), name="doorbell-health")
    try:
        await stop.wait()
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick
        await sub.stop()


def main() -> None:
    setup_logging("doorbell")
    config = DoorbellConfig.from_env()
    run_service(run(config))


if __name__ == "__main__":
    main()
