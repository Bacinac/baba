"""Live per-camera settings for the tracker, loaded from Postgres.

The tracker is NATS-driven by design and historically had NO database
connection — which meant per-camera tuning couldn't reach it and a disabled
camera's state leaked until the idle sweeper caught it. This module gives it
the same resilient LISTEN pattern the detector's DetectionRules uses: load
`cameras` on start, refresh on every `cameras_changed` NOTIFY, and invoke a
callback so the main loop can apply new stillness parameters to LIVE
MotionTrackers (state preserved) and evict disabled cameras immediately.

Since migration 053 the settings are LAYERED: per-camera columns are
NULLable overrides on top of the `app_settings.tracking_defaults` global
row (edited at Settings → Detection), with the env-derived config values
as the last-resort bootstrap for a fresh install whose migrations haven't
run yet. Resolution: per-camera override > global > env default. The
illumination-profile subsystem stays the top layer by writing explicit
per-camera values when it applies a profile.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
from baba_core.pg_listen import ResilientListener
from baba_core.pipeline_settings import Settings, publish_defaults

log = logging.getLogger(__name__)

_GLOBALS_KEY = "tracking_defaults"


@dataclass(slots=True, frozen=True)
class CameraMotion:
    stillness_ratio: float | None
    park_ms: int | None
    lost_seconds: float | None
    reid_lost_seconds: float | None
    enabled: bool


class CameraSettings:
    """Slug-keyed snapshot of per-camera tracker settings + the global layer."""

    def __init__(
        self, dsn: str, on_change: Callable[[], None], tunables: Settings | None = None
    ) -> None:
        self._dsn = dsn
        # The global-only tunables ride the SAME row and the SAME listener as
        # the per-camera layer — a second watcher would mean a second LISTEN
        # connection for values that arrive in the same NOTIFY.
        self._tunables = tunables
        self._on_change = on_change
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._by_slug: dict[str, CameraMotion] = {}
        # app_settings.tracking_defaults — None until loaded (fall through
        # to the env defaults the callers pass in).
        self._globals: dict[str, Any] = {}

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        self._listener = ResilientListener(
            self._dsn,
            ["cameras_changed", "app_settings_changed"],
            on_notify=lambda _c, _p: self._schedule_refresh(),
            on_connect=self._refresh,
            name="tracker-cameras-listen",
        )
        await self._listener.start()
        log.info(
            "camera settings loaded: %d cameras, globals=%s",
            len(self._by_slug),
            self._globals or "env",
        )

    async def publish_defaults(self, tunables: Settings) -> None:
        """Publish this deployment's env defaults for the settings form."""
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await publish_defaults(conn, tunables)

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # --- public API ------------------------------------------------------

    def _resolve(self, slug: str, field: str, global_key: str, default: float) -> float:
        cam = self._by_slug.get(slug)
        override = getattr(cam, field) if cam is not None else None
        if override is not None:
            return float(override)
        g = self._globals.get(global_key)
        return float(g) if g is not None else default

    def stillness_ratio(self, slug: str, default: float) -> float:
        return self._resolve(slug, "stillness_ratio", "stillness_ratio", default)

    def park_ms(self, slug: str, default: int) -> int:
        cam = self._by_slug.get(slug)
        if cam is not None and cam.park_ms is not None:
            return cam.park_ms
        g = self._globals.get("park_seconds")
        return int(g) * 1000 if g is not None else default

    def lost_seconds(self, slug: str, default: float) -> float:
        return self._resolve(slug, "lost_seconds", "lost_seconds", default)

    def reid_lost_seconds(self, slug: str, default: float) -> float:
        return self._resolve(slug, "reid_lost_seconds", "reid_lost_seconds", default)

    def disabled_slugs(self) -> set[str]:
        return {s for s, cam in self._by_slug.items() if not cam.enabled}

    # --- refresh plumbing --------------------------------------------------

    def _schedule_refresh(self) -> None:
        from home_core.tasks import spawn

        spawn(self._refresh_safe())

    async def _refresh_safe(self) -> None:
        try:
            await self._refresh()
        except Exception:
            log.exception("camera settings refresh failed")

    async def _refresh(self) -> None:
        assert self._pool is not None
        try:
            rows = await self._pool.fetch(
                "SELECT slug, enabled, stillness_ratio, park_seconds, "
                "lost_seconds, reid_lost_seconds FROM cameras"
            )
        except asyncpg.UndefinedColumnError:
            # Cold-start ordering: tracker may come up before the API has
            # applied migration 053. Run on env defaults until NOTIFY.
            log.warning(
                "cameras tracking columns not present yet — using env defaults "
                "until migrations apply"
            )
            self._by_slug = {}
            return
        try:
            raw = await self._pool.fetchval(
                "SELECT value FROM app_settings WHERE key = $1", _GLOBALS_KEY
            )
            if isinstance(raw, str):
                raw = json.loads(raw)
            self._globals = dict(raw) if isinstance(raw, dict) else {}
        except (asyncpg.UndefinedTableError, json.JSONDecodeError):
            self._globals = {}
        if self._tunables is not None:
            self._tunables.apply(self._globals)
        self._by_slug = {
            r["slug"]: CameraMotion(
                stillness_ratio=(
                    float(r["stillness_ratio"]) if r["stillness_ratio"] is not None else None
                ),
                park_ms=(int(r["park_seconds"]) * 1000 if r["park_seconds"] is not None else None),
                lost_seconds=(
                    float(r["lost_seconds"]) if r["lost_seconds"] is not None else None
                ),
                reid_lost_seconds=(
                    float(r["reid_lost_seconds"]) if r["reid_lost_seconds"] is not None else None
                ),
                enabled=bool(r["enabled"]),
            )
            for r in rows
        }
        self._on_change()
