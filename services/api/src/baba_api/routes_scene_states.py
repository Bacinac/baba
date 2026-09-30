"""Scene-state regions CRUD + capture + live test, scoped per camera.

A scene region is a fixed polygon whose *persistent state* (gate open/closed,
garage door up/down, light on/off) the state-evaluator service classifies via
DINOv2 few-shot prototypes — orthogonal to zones, which test transient tracks.
See db/migrations/042_scene_states.sql and services/state-evaluator.

Endpoints:
  GET    /cameras/{camera_id}/scene-regions
  POST   /cameras/{camera_id}/scene-regions
  GET    /scene-regions/{region_id}
  PATCH  /scene-regions/{region_id}
  DELETE /scene-regions/{region_id}
  POST   /scene-regions/{region_id}/capture     (capture a labelled prototype,
                                                 live or from a recorded `at`)
  GET    /scene-regions/{region_id}/evaluate     (classify now, no commit)
  DELETE /scene-prototypes/{prototype_id}

This router owns only metadata. All pixel + inference work (capture, evaluate)
lives in the state-evaluator, reached over NATS request/reply — the API
container has no frame-ring access, so it must not try to read frames itself.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import nats.errors
from baba_core.paths import MediaLayout
from baba_core.wire import SUBJECT_STATE_CAPTURE, SUBJECT_STATE_EVAL
from fastapi import APIRouter, Depends, HTTPException, Request

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.models import (
    SceneCaptureIn,
    SceneCaptureOut,
    SceneEvalOut,
    ScenePrototype,
    SceneRegion,
    SceneRegionIn,
    SceneRegionPatch,
    SceneRegionStatus,
)
from baba_api.routes import reject_nulls

log = logging.getLogger(__name__)

scene_states_router = APIRouter()

# A live capture reads the SHM ring and answers in milliseconds; capturing from
# a recorded moment makes the evaluator probe + seek + rescale a segment, whose
# own ceilings there sum to ~40s. The request must be allowed to outlive them:
# a premature 504 here would not stop the evaluator, it would just orphan a
# prototype nobody is waiting for. Still well inside Cloudflare's ~100s proxy
# timeout, so the operator gets a real answer rather than a 524.
_CAPTURE_FROM_RECORDING_TIMEOUT_S = 60.0

_REGION_FIELDS = (
    "id, camera_id, name, place, polygon, states, sample_interval_s, "
    "hysteresis_n, unknown_margin, color, enabled, created_at, updated_at"
)
_STATUS_FIELDS = (
    "region_id, current_state, current_state_since, last_eval_at, "
    "last_label_raw, last_distance"
)
_PROTO_FIELDS = "id, region_id, state_label, crop_path, captured_at"

_SAFE_CROP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jpe?g$")


def _pool(request: Request):
    return request.app.state.pool


def _validate_polygon(poly: list[list[float]]) -> None:
    if len(poly) < 3 or len(poly) > 64:
        raise HTTPException(400, "polygon must have 3..64 vertices")
    for pt in poly:
        if len(pt) != 2:
            raise HTTPException(400, "each polygon vertex must be [x, y]")
        x, y = pt
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise HTTPException(400, "polygon coords must be normalized to [0,1]")


def _to_model(row, status_row, proto_rows) -> SceneRegion:
    polygon = json.loads(row["polygon"]) if isinstance(row["polygon"], str) else row["polygon"]
    status = (
        SceneRegionStatus(
            current_state=status_row["current_state"],
            current_state_since=status_row["current_state_since"],
            last_eval_at=status_row["last_eval_at"],
            last_label_raw=status_row["last_label_raw"],
            last_distance=status_row["last_distance"],
        )
        if status_row is not None
        else None
    )
    return SceneRegion(
        id=row["id"],
        camera_id=row["camera_id"],
        name=row["name"],
        place=row["place"],
        polygon=polygon,
        states=list(row["states"] or []),
        sample_interval_s=row["sample_interval_s"],
        hysteresis_n=row["hysteresis_n"],
        unknown_margin=float(row["unknown_margin"]),
        color=row["color"],
        enabled=row["enabled"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=status,
        prototypes=[
            ScenePrototype(
                id=p["id"],
                state_label=p["state_label"],
                crop_path=p["crop_path"],
                captured_at=p["captured_at"],
            )
            for p in proto_rows
        ],
    )


async def _load_one(pool, region_id: UUID) -> SceneRegion | None:
    row = await pool.fetchrow(
        f"SELECT {_REGION_FIELDS} FROM scene_regions WHERE id = $1", region_id  # noqa: S608
    )
    if row is None:
        return None
    status_row = await pool.fetchrow(
        f"SELECT {_STATUS_FIELDS} FROM scene_region_status WHERE region_id = $1",  # noqa: S608
        region_id,
    )
    proto_rows = await pool.fetch(
        f"SELECT {_PROTO_FIELDS} FROM scene_region_prototypes "  # noqa: S608
        "WHERE region_id = $1 ORDER BY captured_at ASC",
        region_id,
    )
    return _to_model(row, status_row, proto_rows)


# --- CRUD -------------------------------------------------------------------


@scene_states_router.get(
    "/cameras/{camera_id}/scene-regions", response_model=list[SceneRegion]
)
async def list_scene_regions(camera_id: UUID, request: Request) -> list[SceneRegion]:
    pool = _pool(request)
    rows = await pool.fetch(
        # By name, not by when they were drawn: P1 P2 P3 is how an operator
        # reads a row of parking spaces, and creation order put P2 first on the
        # shed camera simply because that is the one that was traced first.
        f"SELECT {_REGION_FIELDS} FROM scene_regions WHERE camera_id = $1 "  # noqa: S608
        "ORDER BY name ASC, created_at ASC",
        camera_id,
    )
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    status_rows = await pool.fetch(
        f"SELECT {_STATUS_FIELDS} FROM scene_region_status WHERE region_id = ANY($1::uuid[])",  # noqa: S608
        ids,
    )
    proto_rows = await pool.fetch(
        f"SELECT {_PROTO_FIELDS} FROM scene_region_prototypes "  # noqa: S608
        "WHERE region_id = ANY($1::uuid[]) ORDER BY captured_at ASC",
        ids,
    )
    status_by = {s["region_id"]: s for s in status_rows}
    protos_by: dict[UUID, list] = {}
    for p in proto_rows:
        protos_by.setdefault(p["region_id"], []).append(p)
    return [_to_model(r, status_by.get(r["id"]), protos_by.get(r["id"], [])) for r in rows]


@scene_states_router.post(
    "/cameras/{camera_id}/scene-regions", response_model=SceneRegion, status_code=201
)
async def create_scene_region(
    camera_id: UUID,
    payload: SceneRegionIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> SceneRegion:
    _validate_polygon(payload.polygon)
    pool = _pool(request)
    cam = await pool.fetchrow("SELECT id, name, slug FROM cameras WHERE id = $1", camera_id)
    if cam is None:
        raise HTTPException(404, "camera not found")
    row = await pool.fetchrow(
        f"""
        INSERT INTO scene_regions
            (camera_id, name, place, polygon, states, sample_interval_s,
             hysteresis_n, unknown_margin, color, enabled)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
        RETURNING {_REGION_FIELDS}
        """,  # noqa: S608
        camera_id,
        payload.name,
        payload.place,
        json.dumps(payload.polygon),
        payload.states,
        payload.sample_interval_s,
        payload.hysteresis_n,
        payload.unknown_margin,
        payload.color,
        payload.enabled,
    )
    await write_audit(
        pool,
        user=user,
        resource_type="scene_region",
        op="create",
        resource_id=row["id"],
        payload={
            "camera_id": str(camera_id),
            "camera_name": cam["name"],
            "camera_slug": cam["slug"],
            "region_name": payload.name,
            "states": payload.states,
        },
    )
    return _to_model(row, None, [])


@scene_states_router.get("/scene-regions/{region_id}", response_model=SceneRegion)
async def get_scene_region(region_id: UUID, request: Request) -> SceneRegion:
    region = await _load_one(_pool(request), region_id)
    if region is None:
        raise HTTPException(404, "scene region not found")
    return region


# See `reject_nulls` in routes.py — same defect, same shape: a patch key sent
# as JSON `null` survives `exclude_unset` and reaches `SET <col> = NULL`.
_REGION_NOT_NULL = frozenset({
    "color", "enabled", "hysteresis_n", "name", "polygon",
    "sample_interval_s", "states", "unknown_margin",
})


@scene_states_router.patch("/scene-regions/{region_id}", response_model=SceneRegion)
async def patch_scene_region(
    region_id: UUID,
    payload: SceneRegionPatch,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> SceneRegion:
    fields = payload.model_dump(exclude_unset=True)
    reject_nulls(fields, _REGION_NOT_NULL)
    if not fields:
        raise HTTPException(400, "no fields to update")
    if "polygon" in fields:
        _validate_polygon(fields["polygon"])
    pool = _pool(request)

    set_clauses: list[str] = []
    args: list = []
    for i, (k, v) in enumerate(fields.items(), start=1):
        if k == "polygon":
            set_clauses.append(f"{k} = ${i}::jsonb")
            args.append(json.dumps(v))
        else:
            set_clauses.append(f"{k} = ${i}")
            args.append(v)
    args.append(region_id)
    row = await pool.fetchrow(
        f"UPDATE scene_regions SET {', '.join(set_clauses)} "  # noqa: S608
        f"WHERE id = ${len(args)} RETURNING {_REGION_FIELDS}",
        *args,
    )
    if row is None:
        raise HTTPException(404, "scene region not found")
    audit_payload = {k: (len(v) if k == "polygon" else v) for k, v in fields.items()}
    audit_payload["region_name"] = row["name"]
    audit_payload["camera_id"] = str(row["camera_id"])
    await write_audit(
        pool,
        user=user,
        resource_type="scene_region",
        op="update",
        resource_id=region_id,
        payload=audit_payload,
    )
    region = await _load_one(pool, region_id)
    assert region is not None
    return region


@scene_states_router.delete("/scene-regions/{region_id}", status_code=204)
async def delete_scene_region(
    region_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = _pool(request)
    pre = await pool.fetchrow(
        "SELECT r.camera_id, r.name, c.name AS cam_name, c.slug AS cam_slug "
        "FROM scene_regions r LEFT JOIN cameras c ON c.id = r.camera_id "
        "WHERE r.id = $1",
        region_id,
    )
    # Collect prototype crops to unlink after the row delete (CASCADE drops
    # the DB rows; the JPEGs on disk need explicit cleanup).
    crops = await pool.fetch(
        "SELECT crop_path FROM scene_region_prototypes WHERE region_id = $1", region_id
    )
    res = await pool.execute("DELETE FROM scene_regions WHERE id = $1", region_id)
    if res.endswith(" 0"):
        raise HTTPException(404, "scene region not found")
    for c in crops:
        _unlink_crop(request, c["crop_path"])
    await write_audit(
        pool,
        user=user,
        resource_type="scene_region",
        op="delete",
        resource_id=region_id,
        payload={
            "camera_id": str(pre["camera_id"]),
            "camera_name": pre["cam_name"],
            "camera_slug": pre["cam_slug"],
            "region_name": pre["name"],
        }
        if pre
        else {},
    )


# --- capture + evaluate (delegated to the state-evaluator over NATS) ---------


async def _state_request(
    request: Request, subject: str, payload: dict, reply_timeout_s: float = 8.0
) -> dict:
    nc = getattr(request.app.state, "nats", None)
    if nc is None:
        raise HTTPException(503, "state-evaluator control bus unavailable")
    try:
        resp = await nc.request(subject, json.dumps(payload).encode(), timeout=reply_timeout_s)
    except nats.errors.NoRespondersError:
        raise HTTPException(503, "state-evaluator service is not running") from None
    except (TimeoutError, nats.errors.TimeoutError):
        raise HTTPException(504, "state-evaluator did not respond") from None
    try:
        return json.loads(resp.data)
    except ValueError as e:
        raise HTTPException(502, f"bad state-evaluator reply: {e}") from None


@scene_states_router.post(
    "/scene-regions/{region_id}/capture", response_model=SceneCaptureOut
)
async def capture_prototype(
    region_id: UUID,
    payload: SceneCaptureIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> SceneCaptureOut:
    pool = _pool(request)
    row = await pool.fetchrow("SELECT states FROM scene_regions WHERE id = $1", region_id)
    if row is None:
        raise HTTPException(404, "scene region not found")
    states = list(row["states"] or [])
    if payload.state_label not in states:
        raise HTTPException(
            400, f"state_label must be one of the region's states: {states}"
        )
    at = payload.at
    if at is not None:
        # A naive value is read as UTC on both sides; the web UI resolves the
        # operator's local wall-clock against their browser timezone first.
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if at > datetime.now(UTC):
            raise HTTPException(400, "at must be in the past")
    reply = await _state_request(
        request,
        SUBJECT_STATE_CAPTURE,
        {
            "region_id": str(region_id),
            "state_label": payload.state_label,
            "created_by": str(user.id),
            "at": at.isoformat() if at else None,
        },
        reply_timeout_s=_CAPTURE_FROM_RECORDING_TIMEOUT_S if at else 8.0,
    )
    if reply.get("error"):
        # Not an HTTP error: the request was well-formed and we answered it.
        # "the archive has no segment covering that moment" or "the camera has
        # no live frame yet" are ordinary outcomes the operator acts on, and
        # SceneCaptureOut.error exists precisely to carry the evaluator's
        # stable slug through to a localised message. (This used to raise 422,
        # which reached the UI as a raw `422: {"detail": …}` string and left
        # the client's error branch dead.)
        return SceneCaptureOut(
            prototype_id=None, crop_path=None, error=str(reply["error"])
        )
    await write_audit(
        pool,
        user=user,
        resource_type="scene_region",
        op="capture",
        resource_id=region_id,
        payload={
            "state_label": payload.state_label,
            "prototype_id": reply.get("prototype_id"),
            # Which moment taught this state — a live capture is self-evidently
            # `captured_at`, one from the archive is not.
            "at": at.isoformat() if at else None,
        },
    )
    return SceneCaptureOut(
        prototype_id=reply.get("prototype_id"),
        crop_path=reply.get("crop_path"),
        error=None,
    )


@scene_states_router.get(
    "/scene-regions/{region_id}/evaluate", response_model=SceneEvalOut
)
async def evaluate_region(region_id: UUID, request: Request) -> SceneEvalOut:
    exists = await _pool(request).fetchval(
        "SELECT 1 FROM scene_regions WHERE id = $1", region_id
    )
    if not exists:
        raise HTTPException(404, "scene region not found")
    reply = await _state_request(request, SUBJECT_STATE_EVAL, {"region_id": str(region_id)})
    return SceneEvalOut(
        state=reply.get("state"),
        raw_label=reply.get("raw_label"),
        distance=reply.get("distance"),
        per_state=reply.get("per_state") or {},
        error=reply.get("error"),
    )


@scene_states_router.delete("/scene-prototypes/{prototype_id}", status_code=204)
async def delete_prototype(
    prototype_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = _pool(request)
    pre = await pool.fetchrow(
        "SELECT region_id, state_label, crop_path FROM scene_region_prototypes WHERE id = $1",
        prototype_id,
    )
    if pre is None:
        raise HTTPException(404, "prototype not found")
    await pool.execute("DELETE FROM scene_region_prototypes WHERE id = $1", prototype_id)
    _unlink_crop(request, pre["crop_path"])
    # Bump the parent region so the evaluator reconciles its in-memory
    # prototype cache (prototypes have no NOTIFY of their own; the region's
    # scene_regions_changed trigger does the wake-up).
    await pool.execute(
        "UPDATE scene_regions SET updated_at = now() WHERE id = $1", pre["region_id"]
    )
    await write_audit(
        pool,
        user=user,
        resource_type="scene_region",
        op="prototype_delete",
        resource_id=pre["region_id"],
        payload={"prototype_id": str(prototype_id), "state_label": pre["state_label"]},
    )


def _unlink_crop(request: Request, crop_path: str | None) -> None:
    """Best-effort removal of a prototype's reference JPEG. crop_path is the
    DB-stored relative path 'scene_crops/<uuid>.jpg'; validate the basename
    against the same allow-list the media server uses before touching disk."""
    if not crop_path:
        return
    name = Path(crop_path).name
    if not _SAFE_CROP_NAME.match(name):
        log.warning("refusing to unlink suspicious crop_path %r", crop_path)
        return
    media_root = MediaLayout(Path(request.app.state.config.media_path)).scene_crops
    full = (media_root / name).resolve()
    try:
        full.relative_to(media_root.resolve())
    except ValueError:
        return
    try:
        full.unlink(missing_ok=True)
    except OSError as e:
        log.warning("failed to unlink scene crop %s: %s", full, e)
