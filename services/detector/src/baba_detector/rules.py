"""Live-reloadable detection rules for the detector service.

Resolves the effective `(enabled, min_confidence)` for a given
`(camera_slug, class_name)` pair using two layers:

  1. detector_global_rules        — per-class floor for the whole system
  2. camera_detection_rules       — per-camera overrides (NULLable fields
                                    fall back to the global value)

Rows are loaded from Postgres on `start()` and refreshed on every
`detection_rules_changed` and `cameras_changed` NOTIFY.  The detector
main loop calls `effective(camera_slug, class_name)` after `decode()`
and drops detections that fail the check before publishing.

A class that has NO global row is treated as `enabled=False` — the
seeded set in migration 034 is the BABA "out of the box" allowlist;
new classes are an explicit opt-in via the Settings → AI page.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from baba_core.pg_listen import ResilientListener
from baba_core.rule_resolve import ClassRule, resolve
from home_core.tasks import spawn

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _GlobalRule:
    enabled: bool
    min_confidence: float
    # Minimum bbox size as % of the frame — the LARGER of w/frame_w and
    # h/frame_h. 0 disables the floor. Confidence can't separate a 25 px
    # hedge-phantom "person" from a real distant one; a size floor can.
    min_box_pct: float


@dataclass(slots=True, frozen=True)
class _CameraRule:
    enabled: bool | None
    min_confidence: float | None
    min_box_pct: float | None


class DetectionRules:
    """In-memory snapshot of layers 1 + 2, plus a slug↔uuid map for
    cameras.  Detector keys frames by slug, but rule overrides key by
    camera_id — the map bridges the two.  Refreshed live on NOTIFY."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._stop = asyncio.Event()
        # class_name → _GlobalRule
        self._global: dict[str, _GlobalRule] = {}
        # camera_id (uuid) → class_name → _CameraRule
        self._per_camera: dict[UUID, dict[str, _CameraRule]] = {}
        # slug → uuid.  Detector keys by slug; we need uuid for the
        # per_camera lookup.  Refreshed on cameras_changed.
        self._uuid_by_slug: dict[str, UUID] = {}
        # slug → per-camera maintain-confidence floor (cameras.maintain_conf,
        # NOT NULL).  The two-threshold tracker keeps a track alive down to
        # this floor; it's per-camera (and light-profiled) so night IR can
        # lower it without touching the birth threshold.  Refreshed with the
        # camera map.
        self._maintain_by_slug: dict[str, float] = {}
        # camera_id (uuid) → from_class → to_class.  Per-camera class remap
        # (e.g. west: suitcase→car) applied in the detector before dedup +
        # the rules filter, to recover a vehicle the nano mislabels as a
        # disabled indoor-object class.  Refreshed on the same NOTIFY.
        self._remap: dict[UUID, dict[str, str]] = {}

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        # Resilient LISTEN: initial load via on_connect (_refresh_all also
        # refreshes the camera slug map), and a full refresh after any DB
        # drop so rule/camera edits made while the listener was down are
        # picked up without a detector restart.
        self._listener = ResilientListener(
            self._dsn,
            ["detection_rules_changed", "cameras_changed"],
            on_notify=self._on_notify,
            on_connect=self._refresh_all,
            name="detector-rules-listen",
        )
        await self._listener.start()
        log.info(
            "detection rules loaded: %d global, %d cameras with overrides",
            len(self._global),
            len(self._per_camera),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # --- public API ------------------------------------------------------

    def effective(self, camera_slug: str, class_name: str) -> tuple[bool, float, float]:
        """`(enabled, min_confidence, min_box_pct)` for this camera + class.

        Resolution lives in `baba_core.rule_resolve` so the api answers the
        same question with the same code — the camera rules card used to work
        it out on its own and showed a fabricated 0.25 for classes this drops.
        """
        cam_id = self._uuid_by_slug.get(camera_slug)
        override = (
            self._per_camera.get(cam_id, {}).get(class_name) if cam_id is not None else None
        )
        gl = self._global.get(class_name)
        eff = resolve(
            ClassRule(gl.enabled, gl.min_confidence, gl.min_box_pct) if gl else None,
            ClassRule(override.enabled, override.min_confidence, override.min_box_pct)
            if override
            else None,
            any_global_rules=bool(self._global),
        )
        return eff.enabled, eff.min_confidence, eff.min_box_pct

    def maintain(self, camera_slug: str) -> float | None:
        """Per-camera maintain-confidence floor (`cameras.maintain_conf`), or
        None if the camera map hasn't loaded yet (cold start before the camera
        refresh). No global fallback — the caller treats None as "maintain ==
        birth" (strict) until the per-camera value lands on the next refresh."""
        return self._maintain_by_slug.get(camera_slug)

    def remap_class(self, camera_slug: str, class_name: str) -> str:
        """Per-camera class rewrite (e.g. west: suitcase→car).

        Returns the target class name, or `class_name` unchanged when no
        remap is configured for this camera + source class.  The detector
        applies this right after decode, so the corrected detection dedups,
        passes the rules filter, tracks and re-IDs as the intended class.
        """
        cam_id = self._uuid_by_slug.get(camera_slug)
        if cam_id is None:
            return class_name
        return self._remap.get(cam_id, {}).get(class_name, class_name)

    # --- NOTIFY plumbing -------------------------------------------------

    def _on_notify(self, channel: str, payload: str) -> None:
        # asyncpg notify callbacks can't await; schedule the right refresh.
        if channel == "cameras_changed":
            spawn(self._refresh_cameras_safe())
        else:
            spawn(self._refresh_all_safe())

    async def _refresh_all_safe(self) -> None:
        try:
            await self._refresh_all()
        except Exception:
            log.exception("detection rules refresh failed")

    async def _refresh_cameras_safe(self) -> None:
        try:
            await self._refresh_cameras()
        except Exception:
            log.exception("camera slug map refresh failed")

    async def _refresh_all(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await self._refresh_cameras(conn)
            await self._refresh_rules(conn)

    async def _refresh_cameras(self, conn: asyncpg.Connection | None = None) -> None:
        async def _do(c: asyncpg.Connection) -> None:
            try:
                rows = await c.fetch("SELECT id, slug, maintain_conf FROM cameras")
            except asyncpg.UndefinedColumnError:
                # Cold-start ordering: detector up before the API applied
                # migration 056. Load the map now, pick up maintain on the
                # NOTIFY that fires once the API finishes migrating.
                rows = await c.fetch("SELECT id, slug FROM cameras")
                self._uuid_by_slug = {r["slug"]: r["id"] for r in rows}
                self._maintain_by_slug = {}
                return
            self._uuid_by_slug = {r["slug"]: r["id"] for r in rows}
            self._maintain_by_slug = {r["slug"]: float(r["maintain_conf"]) for r in rows}

        if conn is not None:
            await _do(conn)
        else:
            assert self._pool is not None
            async with self._pool.acquire() as c:
                await _do(c)

    async def _refresh_rules(self, conn: asyncpg.Connection) -> None:
        # Tolerate cold-start ordering: detector may come up before the
        # API has applied migration 034.  Treat missing tables as
        # "no rules yet" and rely on NOTIFY to wake us once API
        # finishes migrating + seeding.
        try:
            gl_rows = await conn.fetch(
                "SELECT class_name, min_confidence, enabled, min_box_pct "
                "FROM detector_global_rules"
            )
        except asyncpg.UndefinedTableError:
            log.warning(
                "detector_global_rules table not present yet — "
                "running in pass-through mode until migrations apply"
            )
            self._global = {}
            self._per_camera = {}
            return
        self._global = {
            r["class_name"]: _GlobalRule(
                enabled=bool(r["enabled"]),
                min_confidence=float(r["min_confidence"]),
                min_box_pct=float(r["min_box_pct"] or 0.0),
            )
            for r in gl_rows
        }
        cam_rows = await conn.fetch(
            "SELECT camera_id, class_name, min_confidence, enabled, min_box_pct "
            "FROM camera_detection_rules"
        )
        per_cam: dict[UUID, dict[str, _CameraRule]] = {}
        for r in cam_rows:
            per_cam.setdefault(r["camera_id"], {})[r["class_name"]] = _CameraRule(
                enabled=r["enabled"],
                min_confidence=(
                    float(r["min_confidence"]) if r["min_confidence"] is not None else None
                ),
                min_box_pct=(
                    float(r["min_box_pct"]) if r["min_box_pct"] is not None else None
                ),
            )
        self._per_camera = per_cam
        # Per-camera class remap (optional table — tolerate a cold start
        # before migration 054 has applied; NOTIFY wakes us once it does).
        try:
            remap_rows = await conn.fetch(
                "SELECT camera_id, from_class, to_class FROM camera_class_remap"
            )
        except asyncpg.UndefinedTableError:
            self._remap = {}
            return
        remap: dict[UUID, dict[str, str]] = {}
        for r in remap_rows:
            remap.setdefault(r["camera_id"], {})[r["from_class"]] = r["to_class"]
        self._remap = remap
