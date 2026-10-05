import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from home_core.tasks import spawn


async def finish_on_cancel(coro: Coroutine[Any, Any, Any], *, name: str, log: logging.Logger):
    task = spawn(coro, name=name, log=log)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        task.exception()
        raise asyncio.CancelledError
    return task.result()


class TaskOwner:
    def __init__(self, name: str, log: logging.Logger) -> None:
        self._name = name
        self._log = log
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> None:
        if self._stopping:
            coro.close()
            return
        task = spawn(coro, name=name or self._name, log=self._log)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self, *, cancel: bool = True) -> None:
        self._stopping = True
        tasks = tuple(self._tasks)
        if cancel:
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
