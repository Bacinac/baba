"""Ignore zones for the tracker: operator-declared dead ground.

Zones have existed since migration 008 but were strictly ADDITIVE — they
only ever generated zone_enter/zone_dwell/zone_exit events, and only the
event-manager ever loaded them. No polygon could suppress anything, and the
detector and tracker were entirely zone-unaware. The `no_go` kind ("Zabranjena
zona" in the UI) reads like the missing feature but is a pure label: the AI
zone-suggest prompt actively proposes it for "neighbour property" while
nothing in the pipeline consumes it.

The `ignore` kind added alongside this module is the real thing, and it lives
HERE rather than in the event-manager on purpose. Filtering at the event layer
still pays for the track, the OSNet ReID crop and the DINOv2 embedding on a
neighbour's driveway — the expensive part. Gating at birth in the tracker
means those pixels cost one point-in-polygon test per detection and nothing
else. The detector stays zone-unaware (it is shared across cameras and
batched; per-camera polygons do not belong in it).

This is the operator's manual override, deliberately NOT the answer to static
clutter — a hose that reads as `person` is handled by the self-learning birth
registry (phantom_spots), which needs no configuration and cannot be forgotten
when a camera is re-aimed. Ignore zones are for what evidence cannot infer:
ground where real, moving subjects genuinely appear and the operator simply
does not want them (the neighbour's terrace, a public pavement).

Anchor is the bbox BOTTOM-CENTRE — the subject's foot position, the same
anchor the event-manager's zone tests and the heatmap use, so a polygon drawn
once behaves identically everywhere it is evaluated.

Even-odd point-in-polygon is hand-rolled rather than pulling shapely into the
tracker image: this is one point test per detection on the hot path, the
polygons are small, and shapely's value in the event-manager is the
polygon-vs-polygon surface we don't need here.
"""

from __future__ import annotations

import json
import logging

import asyncpg
from baba_core.pg_listen import ResilientListener
from baba_core.task_owner import TaskOwner

log = logging.getLogger(__name__)

# The zones.kind value. Free-form text in the DB by design (migration 008:
# "kept as text so we can add new kinds without a migration").
KIND_IGNORE = "ignore"

# Parking zones (Carport etc.). A vehicle legitimately sits here, static, for
# days — it banks no mover (a car proves itself only when it DRIVES) and looks
# identical to a phantom, so the birth registry learns its spot and blanks it.
# Spots whose foot sits inside a parking zone are exempt from that suppression:
# the operator drew the zone precisely because things park (and stay) here.
KIND_PARKING = "parking"


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd ray casting. Boundary cases are not special-cased: a subject
    exactly on the edge of an ignore zone is a coin flip either way, and the
    tracker re-tests every tick."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            denom = yj - yi
            if denom != 0.0 and x < (xj - xi) * (y - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


class ZoneMasks:
    """Slug-keyed cache of ignore + parking polygons, refreshed on zones_changed."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._tasks = TaskOwner("tracker-zone-masks", log)
        # slug → list of polygons, each a list of (x, y) in [0,1].
        self._by_slug: dict[str, list[list[tuple[float, float]]]] = {}
        # slug → parking polygons; a spot whose foot is inside one is exempt
        # from static-phantom suppression (see KIND_PARKING).
        self._parking_by_slug: dict[str, list[list[tuple[float, float]]]] = {}

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        self._listener = ResilientListener(
            self._dsn,
            ["zones_changed", "cameras_changed"],
            on_notify=lambda _c, _p: self._schedule_refresh(),
            on_connect=self._refresh,
            name="tracker-zones-listen",
        )
        await self._listener.start()

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        await self._tasks.stop()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def ignored(self, slug: str, nbox: tuple[float, float, float, float]) -> bool:
        """True when this bbox's foot position sits inside an ignore zone."""
        polys = self._by_slug.get(slug)
        if not polys:
            return False
        cx = (nbox[0] + nbox[2]) / 2.0
        cy = nbox[3]
        return any(_point_in_polygon(cx, cy, p) for p in polys)

    def in_parking(self, slug: str, nbox: tuple[float, float, float, float]) -> bool:
        """True when this bbox's foot sits inside a parking zone — the caller
        uses it to exempt the spot from static-phantom suppression. Same
        bottom-centre anchor as `ignored`, so a polygon behaves identically
        wherever it is tested."""
        polys = self._parking_by_slug.get(slug)
        if not polys:
            return False
        cx = (nbox[0] + nbox[2]) / 2.0
        cy = nbox[3]
        return any(_point_in_polygon(cx, cy, p) for p in polys)

    def _schedule_refresh(self) -> None:
        self._tasks.spawn(self._refresh_safe())

    async def _refresh_safe(self) -> None:
        try:
            await self._refresh()
        except Exception:
            log.exception("ignore zones refresh failed")

    async def _refresh(self) -> None:
        assert self._pool is not None
        rows = await self._pool.fetch(
            "SELECT c.slug AS slug, z.id AS id, z.kind AS kind, z.polygon AS polygon "
            "FROM zones z JOIN cameras c ON c.id = z.camera_id "
            "WHERE z.enabled AND z.kind = ANY($1)",
            [KIND_IGNORE, KIND_PARKING],
        )
        by_slug: dict[str, list[list[tuple[float, float]]]] = {}
        parking_by_slug: dict[str, list[list[tuple[float, float]]]] = {}
        for r in rows:
            raw = r["polygon"]
            pts = json.loads(raw) if isinstance(raw, str) else raw
            poly = _parse_polygon(pts)
            if poly is None:
                # A degenerate polygon must not silently ignore the WHOLE
                # camera (nor nothing at all, unremarked) — say so.
                log.warning(
                    "%s zone %s on %s has an unusable polygon — skipping it",
                    r["kind"],
                    r["id"],
                    r["slug"],
                )
                continue
            target = parking_by_slug if r["kind"] == KIND_PARKING else by_slug
            target.setdefault(r["slug"], []).append(poly)
        changed = by_slug != self._by_slug or parking_by_slug != self._parking_by_slug
        self._by_slug = by_slug
        self._parking_by_slug = parking_by_slug
        if changed:
            log.info(
                "zone masks: ignore=%d cams/%d polys, parking=%d cams/%d polys",
                len(by_slug),
                sum(len(v) for v in by_slug.values()),
                len(parking_by_slug),
                sum(len(v) for v in parking_by_slug.values()),
            )


def _parse_polygon(pts: object) -> list[tuple[float, float]] | None:
    if not isinstance(pts, list) or len(pts) < 3:
        return None
    out: list[tuple[float, float]] = []
    for p in pts:
        if not isinstance(p, list | tuple) or len(p) < 2:
            return None
        try:
            out.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError):
            return None
    return out
