from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from urllib.parse import parse_qs, urlsplit

import asyncpg
from baba_core.task_owner import TaskOwner
from home_core.tasks import spawn
from reolink_aio.api import Host
from reolink_aio.exceptions import ReolinkError

from baba_doorbell.config import DoorbellConfig

log = logging.getLogger("baba.doorbell")


def _parse_stream_url(url: str) -> tuple[str, str, str, int, bool]:
    """Pull host + credentials + port out of a camera `stream_url`. Reolink
    doorbells are stored as an HTTP-FLV URL whose creds live in the query
    string (`?user=&password=`); RTSP-style creds (`user:pass@host`) are also
    handled. The API port is plain 80/443 (the FLV media port is irrelevant to
    the control channel)."""
    s = urlsplit(url)
    q = parse_qs(s.query)
    host = s.hostname or ""
    user = s.username or (q.get("user", [""])[0])
    pw = s.password or (q.get("password", [""])[0]) or (q.get("pass", [""])[0])
    https = s.scheme == "https"
    port = 443 if https else 80
    return host, user, pw, port, https


class DoorbellConfigError(RuntimeError):
    """A configured doorbell cannot be watched. Fatal on purpose: a bell nobody
    listens to is indistinguishable from a bell nobody rings, so the service
    dies loudly instead of reporting healthy over a camera it never found."""


class DoorbellSubscriber:
    """Watches each configured doorbell camera and turns a physical button
    press into a `doorbell_press` row in the `events` table — the same table
    the vision pipeline writes to, so the api events-bridge mirrors it onto
    NATS (`baba.events.<camera_id>`) with zero extra wiring, and DIDA reacts.

    Primary path is Baichuan push (instant); a short get_states() poll is a
    backstop so a ring is never dropped."""

    def __init__(self, config: DoorbellConfig) -> None:
        self._cfg = config
        self._pool: asyncpg.Pool | None = None
        self._tasks: list[asyncio.Task] = []
        self._rings = TaskOwner("doorbell-rings", log)
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(dsn=self._cfg.dsn, min_size=1, max_size=2)
        # Keep `cameras.doorbell` in step with our configured slugs. That flag
        # is what the roster — and therefore an off-box consumer's catalog —
        # reads, but the operator knob is this service's env. Syncing on every
        # start makes the DB the single source consumers read without
        # duplicating the env there, and converges a deployment whose slugs
        # differ from the migration's seed.
        await self._pool.execute(
            "UPDATE cameras SET doorbell = (slug = ANY($1::text[])) "
            "WHERE doorbell IS DISTINCT FROM (slug = ANY($1::text[]))",
            list(self._cfg.slugs),
        )
        rows = await self._pool.fetch(
            "SELECT id, slug, name, stream_url FROM cameras WHERE slug = ANY($1::text[])",
            list(self._cfg.slugs),
        )
        missing = [s for s in self._cfg.slugs if s not in {r["slug"] for r in rows}]
        if missing:
            known = await self._pool.fetch("SELECT slug FROM cameras ORDER BY slug")
            raise DoorbellConfigError(
                f"BABA_DOORBELL_SLUGS names {missing}, which no camera has "
                f"(cameras: {[r['slug'] for r in known]})"
            )
        if not self._cfg.slugs:
            log.info("doorbell disabled: no cameras configured")
        for row in rows:
            task = spawn(self._run_camera(dict(row)), name=f"doorbell-{row['slug']}", log=log)
            task.add_done_callback(self._camera_died)
            self._tasks.append(task)

    def _camera_died(self, task: asyncio.Task) -> None:
        """A camera loop only ends on stop; anything else is fatal. Exiting is the
        only lever — the restart policy then makes it visible as a crash loop."""
        if task.cancelled() or self._stop.is_set():
            return
        exc = task.exception()
        log.critical("doorbell: %s stopped watching (%s) — exiting", task.get_name(), exc)
        os._exit(1)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self._rings.stop(cancel=False)
        if self._pool is not None:
            await self._pool.close()

    async def _run_camera(self, row: dict) -> None:
        slug = row["slug"]
        camera_id = row["id"]
        host, user, pw, port, https = _parse_stream_url(row["stream_url"])
        if not host or not user:
            raise DoorbellConfigError(f"{slug}: stream_url carries no host/credentials")

        while not self._stop.is_set():
            h = Host(host, user, pw, port=port, use_https=https)
            try:
                await h.get_host_data()
                if not h.is_doorbell(0):
                    raise DoorbellConfigError(f"{slug} ({host}) is not a doorbell")
                state = {"last": bool(h.visitor_detected(0))}

                # Baichuan TCP push (primary, instant). Best-effort: if a given
                # firmware/library version rejects it, we fall back to polling.
                pushing = False
                try:
                    await h.baichuan.subscribe_events()

                    # h/state bound at definition, not looked up when the
                    # callback fires: both are rebound on every reconnect of the
                    # enclosing loop, so a late push arriving on the previous
                    # connection would otherwise read the NEW host and write the
                    # NEW edge-detect state — dropping or duplicating a ring
                    # exactly around a reconnect.
                    def _on_push(h=h, state=state) -> None:
                        cur = bool(h.visitor_detected(0))
                        if cur and not state["last"]:
                            self._rings.spawn(self._insert_ring(camera_id, slug, "baichuan"))
                        state["last"] = cur

                    h.baichuan.register_callback("dida-doorbell", _on_push)
                    pushing = True
                    log.info("doorbell: watching %s (%s) via Baichuan push", slug, host)
                except (ReolinkError, OSError) as exc:
                    log.warning("doorbell %s: Baichuan push unavailable (%s); polling only", slug, exc)

                if not pushing:
                    log.info("doorbell: watching %s (%s) via %.0fs poll", slug, host, self._cfg.poll_seconds)

                # Keep the session alive; poll as a liveness probe + backstop
                # edge-detect for a ring that a push may have missed.
                while not self._stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.poll_seconds)
                    if self._stop.is_set():
                        break
                    if pushing and not h.baichuan.events_active:
                        raise ConnectionError("Baichuan push dropped")
                    await h.get_states()
                    cur = bool(h.visitor_detected(0))
                    if cur and not state["last"]:
                        await self._insert_ring(camera_id, slug, "poll")
                    state["last"] = cur
            except (asyncio.CancelledError, DoorbellConfigError):
                raise
            except (ReolinkError, OSError) as exc:
                log.warning(
                    "doorbell %s: %s; reconnecting in %.0fs", slug, exc, self._cfg.reconnect_seconds
                )
            except Exception:
                log.exception(
                    "doorbell %s: unexpected failure; reconnecting in %.0fs", slug, self._cfg.reconnect_seconds
                )
            finally:
                with contextlib.suppress(Exception):
                    await h.baichuan.unsubscribe_events()
                with contextlib.suppress(Exception):
                    await h.logout()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.reconnect_seconds)

    async def _insert_ring(self, camera_id, slug: str, source: str) -> None:
        if self._pool is None:
            return
        try:
            await self._pool.execute(
                "INSERT INTO events (camera_id, kind, payload) VALUES ($1, 'doorbell_press', $2::jsonb)",
                camera_id,
                json.dumps({"action": "ring", "source": f"reolink_{source}"}),
            )
            log.info("doorbell RING: %s → doorbell_press event (%s)", slug, source)
        except Exception:
            log.exception("doorbell %s: failed to insert ring event", slug)
