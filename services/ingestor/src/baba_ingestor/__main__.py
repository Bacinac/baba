from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from baba_core import StatsCollector, drain_quietly, setup_logging
from baba_core.nats_conn import connect as nats_connect
from baba_core.runtime import run_service
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_ingestor.config import SupervisorConfig
from baba_ingestor.supervisor import IngestorSupervisor

log = logging.getLogger("baba.ingestor")


async def run(config: SupervisorConfig) -> None:
    # Dedicated NATS connection for the stats publisher — the per-camera
    # workers each own their own frame-publisher connection, so the
    # supervisor level has none of its own to piggyback on.
    nc = await nats_connect(config.nats_url, name="ingestor-stats")
    stats = StatsCollector(service="ingestor")
    supervisor = IngestorSupervisor(config, stats=stats)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await supervisor.start()
    await stats.start(nc)

    # The supervisor task waits passively on `stop`; per-camera workers
    # have their own retry loops. The health touch lives at this level
    # so the marker reflects the supervisor's own event loop, not any
    # one camera's RTSP state. A camera that won't connect is the
    # supervisor's "expected" state — not a process restart trigger.
    tick = spawn(HealthMarker("baba", "ingestor").run_loop(), name="ingestor-health")

    try:
        await stop.wait()
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick
        await stats.stop()
        await supervisor.stop()
        await drain_quietly(nc)


def main() -> None:
    setup_logging("ingestor")
    config = SupervisorConfig.from_env()
    run_service(run(config))


if __name__ == "__main__":
    main()
