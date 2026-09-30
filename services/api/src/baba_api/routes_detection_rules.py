"""Detection rules CRUD — layers 1 (global) and 2 (per-camera).

Layer 3 (per-zone refinement) is exposed via the existing zones router
through the `rules` JSONB column on each zone — see routes_zones.py.

The three layers are documented in db/migrations/034_detection_rules.sql.
Live-reloaded by the detector service via `detection_rules_changed`
NOTIFY, so the operator's PUT is reflected in the next frame's
filtering without a restart.

Endpoints:
  GET    /detection-rules/classes
  GET    /detection-rules/global
  PUT    /detection-rules/global/{class_name}
  DELETE /detection-rules/global/{class_name}
  GET    /cameras/{camera_id}/detection-rules
  GET    /cameras/{camera_id}/detection-rules/effective
  PUT    /cameras/{camera_id}/detection-rules/{class_name}
  DELETE /cameras/{camera_id}/detection-rules/{class_name}
"""

from __future__ import annotations

import logging
from uuid import UUID

from baba_core import COCO_CLASSES
from baba_core.rule_resolve import ClassRule, resolve
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.profile_capture import journal_rule_change

# COCO catalogue is owned by baba_core.classes (same list the detector's
# postprocessor maps ids with). Aliased to the name this module's helpers use.
COCO_CLASS_LIST: tuple[str, ...] = COCO_CLASSES

log = logging.getLogger(__name__)

detection_rules_router = APIRouter(tags=["detection-rules"])


# --- request / response models ---------------------------------------------


class DetectionClass(BaseModel):
    """One entry in the COCO class catalogue.  Used by the UI to render
    the picker — the operator chooses which classes to enable per
    layer.  Group lets the UI bucket them (person / vehicle / animal /
    other) so the picker isn't an 80-item flat list."""

    class_id: int
    name: str
    group: str


class GlobalRule(BaseModel):
    class_name: str
    min_confidence: float = Field(..., ge=0.0, le=1.0)
    enabled: bool
    # Minimum bbox size as % of the frame (max of w/frame_w, h/frame_h).
    # 0 = no size floor.
    min_box_pct: float = Field(0.0, ge=0.0, le=100.0)


class GlobalRuleIn(BaseModel):
    min_confidence: float = Field(..., ge=0.0, le=1.0)
    enabled: bool = True
    min_box_pct: float = Field(0.0, ge=0.0, le=100.0)


class EffectiveRule(BaseModel):
    """What a camera ACTUALLY applies for one class, resolved server-side.

    The UI used to derive this from the two raw lists and got the important
    case wrong: with no global row it fell back to a made-up 0.25 and rendered
    the class as enabled, while the detector drops it outright. `source` is
    what lets the card say WHY, instead of showing a number that runs nowhere.
    """

    class_name: str
    enabled: bool
    min_confidence: float
    min_box_pct: float
    source: str  # unconfigured | global | override | not-allowed


class CameraRule(BaseModel):
    """Per-camera override.  Both fields may be `null` — meaning
    "inherit from global for this class".  At least one should be set
    or the row is redundant."""

    camera_id: UUID
    class_name: str
    min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    enabled: bool | None = None
    min_box_pct: float | None = Field(None, ge=0.0, le=100.0)


class CameraRuleIn(BaseModel):
    min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    enabled: bool | None = None
    min_box_pct: float | None = Field(None, ge=0.0, le=100.0)


# --- COCO class catalogue ---------------------------------------------------
#
# Kept in sync with services/detector/src/baba_detector/postprocess.py
# COCO_CLASSES (same index order — 80 classes from the standard COCO
# detection benchmark).  Groups are arbitrary editorial buckets so the
# UI can show a categorised picker.

_GROUPS = {
    "person": ["person"],
    "vehicle": ["bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat"],
    "animal": [
        "bird",
        "cat",
        "dog",
        "horse",
        "sheep",
        "cow",
        "elephant",
        "bear",
        "zebra",
        "giraffe",
    ],
}

def _group_for(name: str) -> str:
    for group, members in _GROUPS.items():
        if name in members:
            return group
    return "other"


def _pool(request: Request):
    return request.app.state.pool


def _validate_class_name(name: str) -> None:
    if name not in COCO_CLASS_LIST:
        raise HTTPException(400, f"unknown COCO class: {name}")


# --- routes: catalogue ------------------------------------------------------


@detection_rules_router.get("/detection-rules/classes", response_model=list[DetectionClass])
async def list_classes() -> list[DetectionClass]:
    """Return the full COCO class catalogue with groups.  Source of
    truth for the UI's class picker."""
    return [
        DetectionClass(class_id=i, name=name, group=_group_for(name))
        for i, name in enumerate(COCO_CLASS_LIST)
    ]


# --- routes: global rules ---------------------------------------------------


@detection_rules_router.get("/detection-rules/global", response_model=list[GlobalRule])
async def list_global_rules(request: Request) -> list[GlobalRule]:
    rows = await _pool(request).fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct "
        "FROM detector_global_rules ORDER BY class_name"
    )
    return [
        GlobalRule(
            class_name=r["class_name"],
            min_confidence=float(r["min_confidence"]),
            enabled=bool(r["enabled"]),
            min_box_pct=float(r["min_box_pct"] or 0.0),
        )
        for r in rows
    ]


@detection_rules_router.put("/detection-rules/global/{class_name}", response_model=GlobalRule)
async def upsert_global_rule(
    class_name: str,
    payload: GlobalRuleIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> GlobalRule:
    _validate_class_name(class_name)
    row = await _pool(request).fetchrow(
        """
        INSERT INTO detector_global_rules (class_name, min_confidence, enabled, min_box_pct)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (class_name) DO UPDATE
        SET min_confidence = EXCLUDED.min_confidence,
            enabled        = EXCLUDED.enabled,
            min_box_pct    = EXCLUDED.min_box_pct
        RETURNING class_name, min_confidence, enabled, min_box_pct
        """,
        class_name,
        payload.min_confidence,
        payload.enabled,
        payload.min_box_pct,
    )
    await write_audit(
        _pool(request),
        user=user,
        resource_type="detection_rule_global",
        op="upsert",
        resource_id=None,
        payload={
            "class_name": class_name,
            "min_confidence": payload.min_confidence,
            "enabled": payload.enabled,
            "min_box_pct": payload.min_box_pct,
        },
    )
    return GlobalRule(
        class_name=row["class_name"],
        min_confidence=float(row["min_confidence"]),
        enabled=bool(row["enabled"]),
        min_box_pct=float(row["min_box_pct"] or 0.0),
    )


@detection_rules_router.delete("/detection-rules/global/{class_name}", status_code=204)
async def delete_global_rule(
    class_name: str,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    _validate_class_name(class_name)
    res = await _pool(request).execute(
        "DELETE FROM detector_global_rules WHERE class_name = $1",
        class_name,
    )
    if res.endswith(" 0"):
        raise HTTPException(404, "rule not found")
    await write_audit(
        _pool(request),
        user=user,
        resource_type="detection_rule_global",
        op="delete",
        resource_id=None,
        payload={"class_name": class_name},
    )


# --- routes: per-camera rules -----------------------------------------------


@detection_rules_router.get("/cameras/{camera_id}/detection-rules", response_model=list[CameraRule])
async def list_camera_rules(
    camera_id: UUID,
    request: Request,
) -> list[CameraRule]:
    rows = await _pool(request).fetch(
        """
        SELECT camera_id, class_name, min_confidence, enabled, min_box_pct
        FROM camera_detection_rules
        WHERE camera_id = $1
        ORDER BY class_name
        """,
        camera_id,
    )
    return [
        CameraRule(
            camera_id=r["camera_id"],
            class_name=r["class_name"],
            min_confidence=(
                float(r["min_confidence"]) if r["min_confidence"] is not None else None
            ),
            enabled=r["enabled"],
            min_box_pct=(
                float(r["min_box_pct"]) if r["min_box_pct"] is not None else None
            ),
        )
        for r in rows
    ]


@detection_rules_router.get(
    "/cameras/{camera_id}/detection-rules/effective",
    response_model=list[EffectiveRule],
)
async def list_camera_rules_effective(
    camera_id: UUID,
    request: Request,
) -> list[EffectiveRule]:
    """Resolved rules for this camera, through the detector's own code path."""
    pool = _pool(request)
    globals_ = await pool.fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct FROM detector_global_rules"
    )
    overrides = await pool.fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct "
        "FROM camera_detection_rules WHERE camera_id = $1",
        camera_id,
    )
    g_by_class = {
        r["class_name"]: ClassRule(r["enabled"], r["min_confidence"], r["min_box_pct"])
        for r in globals_
    }
    o_by_class = {
        r["class_name"]: ClassRule(r["enabled"], r["min_confidence"], r["min_box_pct"])
        for r in overrides
    }
    out: list[EffectiveRule] = []
    for cls in sorted(set(g_by_class) | set(o_by_class)):
        eff = resolve(g_by_class.get(cls), o_by_class.get(cls), any_global_rules=bool(g_by_class))
        out.append(
            EffectiveRule(
                class_name=cls,
                enabled=eff.enabled,
                min_confidence=eff.min_confidence,
                min_box_pct=eff.min_box_pct,
                source=eff.source,
            )
        )
    return out


@detection_rules_router.put(
    "/cameras/{camera_id}/detection-rules/{class_name}",
    response_model=CameraRule,
)
async def upsert_camera_rule(
    camera_id: UUID,
    class_name: str,
    payload: CameraRuleIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> CameraRule:
    _validate_class_name(class_name)
    if payload.min_confidence is None and payload.enabled is None and payload.min_box_pct is None:
        raise HTTPException(
            400,
            "at least one of min_confidence, enabled or min_box_pct must be set",
        )
    cam = await _pool(request).fetchrow(
        "SELECT id FROM cameras WHERE id = $1",
        camera_id,
    )
    if cam is None:
        raise HTTPException(404, "camera not found")
    before_rule_row = await _pool(request).fetchrow(
        "SELECT min_confidence, enabled, min_box_pct FROM camera_detection_rules "
        "WHERE camera_id = $1 AND class_name = $2",
        camera_id,
        class_name,
    )
    row = await _pool(request).fetchrow(
        """
        INSERT INTO camera_detection_rules (camera_id, class_name, min_confidence, enabled, min_box_pct)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (camera_id, class_name) DO UPDATE
        SET min_confidence = EXCLUDED.min_confidence,
            enabled        = EXCLUDED.enabled,
            min_box_pct    = EXCLUDED.min_box_pct
        RETURNING camera_id, class_name, min_confidence, enabled, min_box_pct
        """,
        camera_id,
        class_name,
        payload.min_confidence,
        payload.enabled,
        payload.min_box_pct,
    )
    await write_audit(
        _pool(request),
        user=user,
        resource_type="detection_rule_camera",
        op="upsert",
        resource_id=camera_id,
        payload={
            "class_name": class_name,
            "min_confidence": payload.min_confidence,
            "enabled": payload.enabled,
            "min_box_pct": payload.min_box_pct,
        },
    )
    await journal_rule_change(
        _pool(request),
        camera_id,
        class_name,
        dict(before_rule_row) if before_rule_row is not None else None,
    )
    return CameraRule(
        camera_id=row["camera_id"],
        class_name=row["class_name"],
        min_confidence=(
            float(row["min_confidence"]) if row["min_confidence"] is not None else None
        ),
        enabled=row["enabled"],
        min_box_pct=(
            float(row["min_box_pct"]) if row["min_box_pct"] is not None else None
        ),
    )


@detection_rules_router.delete(
    "/cameras/{camera_id}/detection-rules/{class_name}",
    status_code=204,
)
async def delete_camera_rule(
    camera_id: UUID,
    class_name: str,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    _validate_class_name(class_name)
    before_rule_row = await _pool(request).fetchrow(
        "SELECT min_confidence, enabled, min_box_pct FROM camera_detection_rules "
        "WHERE camera_id = $1 AND class_name = $2",
        camera_id,
        class_name,
    )
    res = await _pool(request).execute(
        "DELETE FROM camera_detection_rules WHERE camera_id = $1 AND class_name = $2",
        camera_id,
        class_name,
    )
    if res.endswith(" 0"):
        raise HTTPException(404, "rule not found")
    await write_audit(
        _pool(request),
        user=user,
        resource_type="detection_rule_camera",
        op="delete",
        resource_id=camera_id,
        payload={"class_name": class_name},
    )
    await journal_rule_change(
        _pool(request),
        camera_id,
        class_name,
        dict(before_rule_row) if before_rule_row is not None else None,
    )
