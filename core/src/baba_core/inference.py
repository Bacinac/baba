import asyncio
import logging
import os

log = logging.getLogger(__name__)


async def run_inference(function, *args, timeout_s: float = 120.0, **kwargs):
    deadline = asyncio.timeout(timeout_s)
    try:
        async with deadline:
            return await asyncio.to_thread(function, *args, **kwargs)
    except TimeoutError:
        if not deadline.expired():
            raise
        log.critical("native inference exceeded %.1fs: %s; exiting for restart",
                     timeout_s, getattr(function, "__qualname__", type(function).__name__))
        os._exit(1)
