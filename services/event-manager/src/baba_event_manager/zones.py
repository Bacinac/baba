"""Zones support for the event-manager.

Loads `zones` rows from Postgres, holds parsed shapely Polygons in
memory, refreshes on `zones_changed` NOTIFY. Provides a `transitions()`
helper that maps a normalised (x, y) point to the set of zone ids it
sits inside.

Zone polygons are stored as JSONB arrays of [x, y] points in [0,1]
coords. The event-manager normalises track bbox centres to the same
range using the detector-published frame_width/frame_height, so polygon
edits don't need to know about camera resolution.

Why shapely over a hand-rolled even-odd test:
  - We may want polygon-vs-polygon intersection later (zone-in-zone
    rules) and shapely already covers that surface.
  - The C library performs better than pure-Python on N>8 vertices and
    is statically compiled into the wheel — no apt install needed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ZoneClassRule:
    """Per-class refinement applied to enter/dwell/exit events in a zone.

    All four fields are optional.  `None` means "inherit" — for dwell
    that's the global `dwell_threshold_ms`, for the rest it's "no
    further restriction".  Stored in zones.rules JSONB under
    `enabled_classes.<class_name>` (see migration 034)."""

    min_confidence: float | None = None
    min_area_pct: float | None = None
    min_dwell_ms: int | None = None
    cooldown_s: int | None = None
    # Motion gating: when set, this rule fires zone events only for
    # tracks in a matching motion_state. None means "no gating —
    # legacy behaviour where parked + active tracks both fire".
    #   "moving_only" — suppress while motion_state is parked/stationary
    # Anything else / unset → legacy ("any motion"). Arrived/departed
    # semantics live in the place registry, not in zone gating.
    motion_gate: str | None = None


@dataclass(slots=True)
class _Zone:
    id: UUID
    camera_id: UUID
    name: str
    kind: str
    enabled: bool
    polygon: Any  # shapely.geometry.Polygon
    # Parsed rules.  `class_rules` is None when the zone has no
    # `enabled_classes` set — i.e. the operator hasn't restricted
    # which classes fire events here.  When it IS a dict, only the
    # listed classes are eligible; everything else is silently
    # ignored even if the polygon contains the bbox.
    class_rules: dict[str, ZoneClassRule] | None = field(default=None)


class ZonesResolver:
    """Per-camera zone cache. Lookup by camera SLUG (matches the rest of
    event-manager's keying; CameraResolver maps slug → uuid)."""

    def __init__(self) -> None:
        # camera_slug → list[_Zone]. Empty list means "camera has no
        # zones"; missing key means "we haven't seen this camera yet"
        # (resolver hits the DB on first lookup as a fallback).
        self._by_slug: dict[str, list[_Zone]] = {}
        # uuid → slug for fast DELETE-by-zone-id reconciliation.
        self._slug_by_camera_id: dict[UUID, str] = {}

    async def refresh(self, conn: asyncpg.Connection) -> None:
        """Full reload. Cheap at v1 scale (zones table is small)."""
        from shapely.geometry import Polygon

        # `ignore` zones are excluded outright: they are a SUPPRESSION
        # instruction, and this resolver only ever produces enter/dwell/exit
        # events. The tracker already drops those tracks at birth (zone_masks)
        # using the same bottom-centre anchor, so in normal operation nothing
        # inside one can reach us anyway — but if the tracker's zone load ever
        # fails, an ignore zone must degrade to silence, not start emitting
        # the very events the operator drew it to prevent.
        rows = await conn.fetch(
            """
            SELECT z.id, z.camera_id, z.name, z.kind, z.enabled, z.polygon,
                   z.rules, c.slug AS camera_slug
            FROM zones z
            JOIN cameras c ON c.id = z.camera_id
            WHERE z.enabled AND z.kind <> 'ignore'
            """
        )
        new_by_slug: dict[str, list[_Zone]] = {}
        new_id_map: dict[UUID, str] = {}
        for r in rows:
            raw = r["polygon"]
            pts = json.loads(raw) if isinstance(raw, str) else raw
            try:
                poly = Polygon(pts)
                if not poly.is_valid:
                    # shapely will silently treat self-intersecting
                    # polygons as MULTIPOLYGONs after buffer(0); try
                    # that as a fallback, otherwise drop the zone.
                    poly = poly.buffer(0)
                    if poly.is_empty or not poly.is_valid:
                        log.warning(
                            "zone %s on %s has invalid polygon — dropping",
                            r["id"],
                            r["camera_slug"],
                        )
                        continue
            except Exception:
                log.exception(
                    "zone %s on %s: shapely refused polygon",
                    r["id"],
                    r["camera_slug"],
                )
                continue
            class_rules = _parse_class_rules(r.get("rules"))
            new_by_slug.setdefault(r["camera_slug"], []).append(
                _Zone(
                    id=r["id"],
                    camera_id=r["camera_id"],
                    name=r["name"],
                    kind=r["kind"],
                    enabled=r["enabled"],
                    polygon=poly,
                    class_rules=class_rules,
                )
            )
            new_id_map[r["camera_id"]] = r["camera_slug"]

        self._by_slug = new_by_slug
        self._slug_by_camera_id = new_id_map
        log.info(
            "zones refreshed: %d cameras with zones, %d zones total",
            len(self._by_slug),
            sum(len(v) for v in self._by_slug.values()),
        )

    def zones_for(self, camera_slug: str) -> list[_Zone]:
        return self._by_slug.get(camera_slug, [])

    def inside(self, camera_slug: str, x_norm: float, y_norm: float) -> frozenset[UUID]:
        """Return the set of zone ids that contain the normalised point.
        Coords are [0,1] in camera frame space."""
        from shapely.geometry import Point

        zones = self.zones_for(camera_slug)
        if not zones:
            return frozenset()
        pt = Point(x_norm, y_norm)
        return frozenset(z.id for z in zones if z.polygon.contains(pt))

    def zone_meta(self, camera_slug: str, zone_id: UUID) -> _Zone | None:
        for z in self.zones_for(camera_slug):
            if z.id == zone_id:
                return z
        return None


def _parse_class_rules(
    raw: Any,
) -> dict[str, ZoneClassRule] | None:
    """Parse the zones.rules JSONB column.

    Returns None when the zone has no class allowlist (the default
    state for zones created before migration 034 or zones where the
    operator hasn't touched detection rules).  Returns a dict
    `{class_name → ZoneClassRule}` when `enabled_classes` is set —
    only listed classes fire events in this zone.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled_classes")
    if not isinstance(enabled, dict) or not enabled:
        return None
    out: dict[str, ZoneClassRule] = {}
    for cls, body in enabled.items():
        if not isinstance(body, dict):
            # Bare `"person": true` shorthand — accept with empty rule.
            out[str(cls)] = ZoneClassRule()
            continue
        out[str(cls)] = ZoneClassRule(
            min_confidence=_as_float(body.get("min_confidence")),
            min_area_pct=_as_float(body.get("min_area_pct")),
            min_dwell_ms=_as_int(body.get("min_dwell_ms")),
            cooldown_s=_as_int(body.get("cooldown_s")),
            motion_gate=_opt_motion_gate(body.get("motion_gate")),
        )
    return out


# Allowlist for the motion_gate value so a typo in the JSONB column
# doesn't silently degrade to "legacy / no gate". Anything other than
# the two known modes resolves to None and falls back to legacy.
_MOTION_GATE_VALUES = frozenset({"moving_only"})


def _opt_motion_gate(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    return s if s in _MOTION_GATE_VALUES else None


# Plain coercion, no clamping: what lands in this JSONB has already been
# bounded by ZoneClassRule (Pydantic) on the way in. See routes_ai._fraction
# for the sanitising counterpart that guards the LLM path.
def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
