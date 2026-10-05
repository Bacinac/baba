"""Resilient Postgres LISTEN connection.

asyncpg binds `add_listener()` to a single underlying socket. If that socket
dies — DB restart, network blip, idle TCP reset, a `docker compose up` that
recreates postgres — the listener **silently stops** and every subsequent
NOTIFY is lost until the process restarts. In BABA this repeatedly bit the
supervisor/reconcile listeners (`cameras_changed`, `zones_changed`,
`detection_rules_changed`): a camera/zone/rule added while a long-running
service's listener was dead was never picked up, so the new camera didn't
record / didn't sync to go2rtc / its zone was treated as absent, etc.

`ResilientListener` owns a dedicated connection, re-registers the listeners
on every (re)connect, and runs a heartbeat so a silently-dead socket is
detected within `heartbeat_s` instead of never. On every successful
(re)connect it awaits `on_connect` — point that at a full reconcile so state
that changed while the listener was down is reconciled (the missed NOTIFYs).

Lazy-imports asyncpg inside methods so `baba_core` stays asyncpg-free for
consumers that don't touch Postgres (e.g. the tracker).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from home_core.tasks import spawn

log = logging.getLogger(__name__)

# Per-NOTIFY callback: (channel, payload). Kept deliberately small — the
# common case just sets an asyncio.Event to wake a reconcile loop.
NotifyCallback = Callable[[str, str], None]
ConnectCallback = Callable[[], Awaitable[None]]


class ResilientListener:
    def __init__(
        self,
        dsn: str,
        channels: list[str],
        on_notify: NotifyCallback,
        on_connect: ConnectCallback | None = None,
        *,
        heartbeat_s: float = 15.0,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        name: str = "pg-listen",
    ) -> None:
        self._dsn = dsn
        self._channels = list(channels)
        self._on_notify = on_notify
        self._on_connect = on_connect
        self._heartbeat_s = heartbeat_s
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._name = name
        self._conn = None  # asyncpg.Connection | None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._dead = asyncio.Event()  # set when the conn is known-dead → wake supervisor

    async def start(self) -> None:
        """Connect, register listeners, run the initial reconcile (awaited so
        the caller has state before it declares itself up — and so a DB that's
        unreachable at boot fails loud, same as the old code), then spawn the
        supervisor that keeps the connection alive across drops."""
        self._stopping = False
        try:
            await self._connect()
            if self._on_connect is not None:
                await self._on_connect()
        except BaseException:
            await self._close_conn()
            raise
        self._task = spawn(self._supervise(), name=self._name, log=log)

    async def stop(self) -> None:
        self._stopping = True
        self._dead.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_conn()

    # --- internals ---

    def _dispatch(self, _conn, _pid, channel, payload) -> None:
        if self._stopping:
            return
        try:
            self._on_notify(channel, payload)
        except Exception:
            log.exception("[%s] notify callback failed", self._name)

    def _on_termination(self, _conn) -> None:
        # asyncpg calls this when the server closes the connection — flips the
        # supervisor into reconnect immediately instead of waiting a heartbeat.
        if not self._stopping:
            log.warning("[%s] LISTEN connection terminated; reconnecting", self._name)
        self._dead.set()

    async def _connect(self) -> None:
        import asyncpg  # lazy: keep core importable without asyncpg installed

        self._conn = await asyncpg.connect(self._dsn)
        self._conn.add_termination_listener(self._on_termination)
        for ch in self._channels:
            await self._conn.add_listener(ch, self._dispatch)
        self._dead.clear()
        log.info("[%s] LISTEN connected (%s)", self._name, ",".join(self._channels))

    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close(timeout=2)

    async def _reconnect(self) -> None:
        await self._close_conn()
        backoff = self._reconnect_min_s
        while not self._stopping:
            try:
                await self._connect()
                if self._on_connect is not None:
                    # Catch up on whatever NOTIFYs we missed while down.
                    await self._on_connect()
                return
            except asyncio.CancelledError:
                await self._close_conn()
                raise
            except Exception as e:
                await self._close_conn()
                log.warning(
                    "[%s] reconnect failed: %s; retry in %.1fs", self._name, e, backoff, exc_info=True
                )
                await asyncio.sleep(backoff)
                backoff = min(self._reconnect_max_s, backoff * 2)

    async def _supervise(self) -> None:
        import asyncpg

        while not self._stopping:
            try:
                # Wake on explicit death, else fall through every heartbeat_s
                # to probe a silently-dead socket with a cheap round-trip.
                try:
                    await asyncio.wait_for(self._dead.wait(), timeout=self._heartbeat_s)
                except TimeoutError:
                    try:
                        await self._conn.fetchval("SELECT 1")
                        continue
                    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                        log.warning("[%s] heartbeat failed (%s); reconnecting", self._name, e)
                if self._stopping:
                    break
                await self._reconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] supervisor loop error", self._name)
                await asyncio.sleep(self._reconnect_min_s)
