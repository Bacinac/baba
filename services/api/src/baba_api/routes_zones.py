"""Zones CRUD, scoped per camera.

Endpoints:
  GET    /cameras/{camera_id}/zones
  POST   /cameras/{camera_id}/zones
  GET    /zones/{zone_id}
  PATCH  /zones/{zone_id}
  DELETE /zones/{zone_id}

Polygon coordinates are normalized to [0,1]. The DB stores them as JSONB.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from baba_api.audit import diff_fields, write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.models import Zone, ZoneIn, ZonePatch, ZoneRules

log = logging.getLogger(__name__)

zones_router = APIRouter()


_ZONE_FIELDS = "id, camera_id, name, kind, polygon, color, enabled, rules, created_at, updated_at"


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


def _row_to_zone(row) -> Zone:
    raw_poly = row["polygon"]
    polygon = json.loads(raw_poly) if isinstance(raw_poly, str) else raw_poly
    raw_rules = row["rules"]
    rules_obj = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules or {}
    return Zone(
        id=row["id"],
        camera_id=row["camera_id"],
        name=row["name"],
        kind=row["kind"],
        polygon=polygon,
        color=row["color"],
        enabled=row["enabled"],
        rules=ZoneRules.model_validate(rules_obj),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@zones_router.get("/cameras/{camera_id}/zones", response_model=list[Zone])
async def list_zones(camera_id: UUID, request: Request) -> list[Zone]:
    rows = await _pool(request).fetch(
        f"SELECT {_ZONE_FIELDS} FROM zones WHERE camera_id = $1 ORDER BY created_at ASC",  # noqa: S608
        camera_id,
    )
    return [_row_to_zone(r) for r in rows]


@zones_router.post("/cameras/{camera_id}/zones", response_model=Zone, status_code=201)
async def create_zone(
    camera_id: UUID,
    payload: ZoneIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> Zone:
    _validate_polygon(payload.polygon)
    cam = await _pool(request).fetchrow(
        "SELECT id, name, slug FROM cameras WHERE id = $1",
        camera_id,
    )
    if cam is None:
        raise HTTPException(404, "camera not found")
    row = await _pool(request).fetchrow(
        f"""
        INSERT INTO zones (camera_id, name, kind, polygon, color, enabled, rules)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb)
        RETURNING {_ZONE_FIELDS}
        """,  # noqa: S608
        camera_id,
        payload.name,
        payload.kind,
        json.dumps(payload.polygon),
        payload.color,
        payload.enabled,
        json.dumps(payload.rules.model_dump(exclude_none=False)),
    )
    await write_audit(
        _pool(request),
        user=user,
        resource_type="zone",
        op="create",
        resource_id=row["id"],
        payload={
            "camera_id": str(camera_id),
            "camera_name": cam["name"],
            "camera_slug": cam["slug"],
            "zone_name": payload.name,
            "kind": payload.kind,
        },
    )
    return _row_to_zone(row)


@zones_router.get("/zones/{zone_id}", response_model=Zone)
async def get_zone(zone_id: UUID, request: Request) -> Zone:
    row = await _pool(request).fetchrow(f"SELECT {_ZONE_FIELDS} FROM zones WHERE id = $1", zone_id)  # noqa: S608
    if row is None:
        raise HTTPException(404, "zone not found")
    return _row_to_zone(row)


@zones_router.patch("/zones/{zone_id}", response_model=Zone)
async def patch_zone(
    zone_id: UUID,
    payload: ZonePatch,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> Zone:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    if "polygon" in fields:
        _validate_polygon(fields["polygon"])
    pool = _pool(request)
    # Fetch touched fields + identity (camera_id, name) before the
    # update so the audit row carries enough context to render
    # "ZoneName on CameraName" even if the zone is later deleted.
    field_list = ", ".join(fields.keys())
    extra_cols: list[str] = []
    if "name" not in fields:
        extra_cols.append("name")
    extra_cols.append("camera_id")
    sel_cols = field_list + (", " + ", ".join(extra_cols) if extra_cols else "")
    before_row = await pool.fetchrow(
        f"SELECT {sel_cols}, "  # noqa: S608
        "       (SELECT name FROM cameras c WHERE c.id = z.camera_id) AS _cam_name, "
        "       (SELECT slug FROM cameras c WHERE c.id = z.camera_id) AS _cam_slug "
        f"FROM zones z WHERE z.id = $1",
        zone_id,
    )
    if before_row is None:
        raise HTTPException(404, "zone not found")

    # Build the SET args. polygon + rules are JSONB and need json.dumps
    # before being passed to UPDATE; do that on a copy so the audit
    # diff still sees plain Python structures.
    sql_fields = dict(fields)
    if "polygon" in sql_fields:
        sql_fields["polygon"] = json.dumps(sql_fields["polygon"])
    if "rules" in sql_fields:
        sql_fields["rules"] = json.dumps(sql_fields["rules"])
    set_clauses: list[str] = []
    args: list = []
    for i, (k, v) in enumerate(sql_fields.items(), start=1):
        cast = "::jsonb" if k in ("polygon", "rules") else ""
        set_clauses.append(f"{k} = ${i}{cast}")
        args.append(v)
    args.append(zone_id)
    row = await pool.fetchrow(
        f"""
        UPDATE zones SET {", ".join(set_clauses)}
        WHERE id = ${len(args)}
        RETURNING {_ZONE_FIELDS}
        """,  # noqa: S608
        *args,
    )
    if row is None:
        raise HTTPException(404, "zone not found")

    # Audit with real before/after so operators can see what changed.
    # Polygons are a long list of [x,y] vertices that just becomes
    # visual noise in the timeline — summarise as vertex count instead.
    def _audit_value(k: str, v: Any) -> Any:
        if k == "polygon":
            if isinstance(v, str):
                v = json.loads(v)
            return {"vertices": len(v)} if isinstance(v, list) else v
        if k == "rules" and isinstance(v, str):
            return json.loads(v)
        return v

    before_d = {k: _audit_value(k, before_row[k]) for k in fields}
    after_d = {k: _audit_value(k, fields[k]) for k in fields}
    diff_payload = diff_fields(before_d, after_d)
    # Carry zone + camera identity alongside the diff so the timeline
    # can render "ZoneName on CameraName" without an extra join (and
    # still works after the zone is gone).
    diff_payload["zone_name"] = fields.get("name") if "name" in fields else before_row["name"]
    diff_payload["camera_id"] = str(before_row["camera_id"])
    diff_payload["camera_name"] = before_row["_cam_name"]
    diff_payload["camera_slug"] = before_row["_cam_slug"]
    await write_audit(
        pool,
        user=user,
        resource_type="zone",
        op="update",
        resource_id=zone_id,
        payload=diff_payload,
    )
    return _row_to_zone(row)


@zones_router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(
    zone_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = _pool(request)
    pre = await pool.fetchrow(
        "SELECT z.camera_id, z.name, z.kind, c.name AS cam_name, c.slug AS cam_slug "
        "FROM zones z LEFT JOIN cameras c ON c.id = z.camera_id WHERE z.id = $1",
        zone_id,
    )
    res = await pool.execute("DELETE FROM zones WHERE id = $1", zone_id)
    if res.endswith(" 0"):
        raise HTTPException(404, "zone not found")
    await write_audit(
        pool,
        user=user,
        resource_type="zone",
        op="delete",
        resource_id=zone_id,
        payload={
            "camera_id": str(pre["camera_id"]),
            "camera_name": pre["cam_name"],
            "camera_slug": pre["cam_slug"],
            "zone_name": pre["name"],
            "kind": pre["kind"],
        }
        if pre
        else {},
    )
