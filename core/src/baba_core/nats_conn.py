"""Connection + shutdown helpers for the services' NATS connections.

`connect()` is the one door every service goes through, so the reconnect policy
is set once instead of relying on nats.py's defaults. Those defaults give up
after 60 attempts (~2 min): the client then closes, the consume loop is dead,
and — because the health marker was touched on a timer that never looked at the
bus — the container went on reporting healthy while nothing flowed. Here the
client reconnects forever, and if it ever does close (an unrecoverable error,
or the broker gone for good) `closed_cb` fails the process loud so compose
restarts it, instead of a live process wired to a dead bus.

`drain_quietly()` is the matching shutdown, and the only one. nats.py runs the
same callbacks for a close we asked for as for one the broker forced, so a
connection closed any other way reads as a dead bus and takes its process down
with it — every SSE stream a browser left restarted the api.

It also rides out a broker that went first: when the whole stack is recreated
at once the broker usually goes down first, so by the time a service reaches
its own shutdown the client is mid-reconnect and drain() raises
ConnectionReconnectingError. The service then dies through a traceback instead
of its exit path: a stack trace in the log of every single redeploy, which is
noise that trains you to ignore the one shutdown error that will matter. The
connection being gone is not a failure to shut down — it is shutdown having
already happened at the other end.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import weakref

import nats

log = logging.getLogger(__name__)

_closing_by_us: weakref.WeakSet = weakref.WeakSet()
_DRAIN_TIMEOUT_S = 5.0


async def connect(url: str, *, name: str, **kwargs):
    """Open a NATS connection that never silently gives up.

    Reconnects forever (a broker restart is always ridden out), logs every
    transition, and on a terminal close raises SIGTERM so the service exits
    and compose restarts it rather than lingering attached to a dead bus.
    Extra keyword arguments pass straight through to `nats.connect`.
    """

    client = None

    def _ours() -> bool:
        return client is not None and client in _closing_by_us

    async def _on_disconnected() -> None:
        if not _ours():
            log.warning("nats disconnected (%s); reconnecting", name)

    async def _on_reconnected() -> None:
        log.info("nats reconnected (%s)", name)

    async def _on_error(err: Exception) -> None:
        log.warning("nats error (%s): %s", name, err)

    async def _on_closed() -> None:
        # A process wired to a closed bus does no work, so make the death
        # visible and let compose restart it. os.kill(SIGTERM) unwinds the
        # event loop cleanly where a bare exit would strand the shutdown path.
        if _ours():
            return
        log.error("nats connection closed for good (%s); exiting for restart", name)
        os.kill(os.getpid(), signal.SIGTERM)

    client = await nats.connect(
        url,
        name=name,
        max_reconnect_attempts=-1,
        disconnected_cb=_on_disconnected,
        reconnected_cb=_on_reconnected,
        error_cb=_on_error,
        closed_cb=_on_closed,
        **kwargs,
    )
    return client


async def drain_quietly(nc) -> None:
    """Flush and close `nc`, tolerating a connection that is already gone.

    Falls back to close() when the drain cannot complete, so buffered publishes
    still get their chance and the socket is released either way.
    """
    if nc is None or nc.is_closed:
        return
    _closing_by_us.add(nc)
    try:
        await asyncio.wait_for(nc.drain(), timeout=_DRAIN_TIMEOUT_S)
        return
    except (TimeoutError, nats.errors.Error) as err:
        log.debug("nats drain skipped (%s); closing instead", err)
    with contextlib.suppress(Exception):
        await nc.close()
