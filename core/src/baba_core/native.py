import asyncio
import contextvars
import logging
import os
from concurrent.futures import Executor
from functools import partial

log = logging.getLogger(__name__)


async def run_native(function, *args, timeout_s: float = 120.0,
                     executor: Executor | None = None, **kwargs):
    loop = asyncio.get_running_loop()
    operation = loop.run_in_executor(executor, contextvars.copy_context().run,
                                     partial(function, *args, **kwargs))
    deadline = loop.time() + timeout_s
    cancelled = False
    # Cancellation cannot stop the native thread or release its resource ownership.
    while not operation.done():
        try:
            done, _ = await asyncio.wait((operation,), timeout=max(0.0, deadline - loop.time()))
        except asyncio.CancelledError:
            cancelled = True
            continue
        if not done:
            log.critical("native operation exceeded %.1fs: %s; exiting for restart",
                         timeout_s, getattr(function, "__qualname__", type(function).__name__))
            os._exit(1)
    if cancelled:
        operation.exception()
        raise asyncio.CancelledError
    return operation.result()
