"""Notification rules CRUD.

A rule maps a (event_kind, filter) tuple to a fan-out list of
notification_channels. The matching + dispatch happens in
baba_api.rules_dispatcher (background task). This module just exposes
the inventory so the UI can build/edit/list rules.

Filter shape (v1):
    {"camera_ids": [uuid, ...], "class_ids": [int, ...]}
Empty/missing values mean "match all" for that dimension. Adding new
dimensions only needs an evaluator change, no migration.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from baba_core.classes import COCO_CLASSES
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user

log = logging.getLogger(__name__)

rules_router = APIRouter()


# Kinds we accept on rule creation. Same set the events table actually
# emits (see services/event-manager/__main__.py). New kinds get added
# here when the pipeline starts emitting them — a rule referencing an
# unknown kind is allowed (only logged), the dispatcher will simply
# never match it until the pipeline catches up.
_KNOWN_KINDS = {"track_finalized", "zone_enter", "zone_exit", "zone_dwell"}


class RuleOut(BaseModel):
    id: str
    name: str
    enabled: bool
    event_kind: str | None
    filter: dict[str, Any]
    channel_ids: list[str]
    last_fired_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class RuleFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_ids: list[UUID] = Field(default_factory=list)
    class_ids: list[Annotated[int, Field(strict=True, ge=0, lt=len(COCO_CLASSES))]] = Field(
        default_factory=list
    )


class RuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    enabled: bool = True
    event_kind: str | None = Field(default="track_finalized", max_length=64)
    filter: RuleFilter = Field(default_factory=RuleFilter)
    channel_ids: list[UUID] = Field(default_factory=list)


class RulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    event_kind: str | None = Field(default=None, max_length=64)
    filter: RuleFilter | None = None
    channel_ids: list[UUID] | None = None

    @field_validator("name", "enabled", "filter", "channel_ids", mode="before")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value


def _row_to_rule(row: Any) -> RuleOut:
    import json

    raw_filter = row["filter"]
    if isinstance(raw_filter, str):
        flt = json.loads(raw_filter)
    else:
        flt = dict(raw_filter) if raw_filter is not None else {}
    chans = row["channel_ids"] or []
    return RuleOut(
        id=str(row["id"]),
        name=row["name"],
        enabled=row["enabled"],
        event_kind=row["event_kind"],
        filter=flt,
        channel_ids=[str(c) for c in chans],
        last_fired_at=row["last_fired_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@rules_router.get("/notifications/rules", response_model=list[RuleOut])
async def list_rules(request: Request) -> list[RuleOut]:
    rows = await request.app.state.pool.fetch(
        """
        SELECT id, name, enabled, event_kind, filter, channel_ids,
               last_fired_at, last_error, created_at, updated_at
        FROM notification_rules
        ORDER BY created_at ASC
        """
    )
    return [_row_to_rule(r) for r in rows]


@rules_router.post(
    "/notifications/rules",
    response_model=RuleOut,
    status_code=201,
)
async def create_rule(
    payload: RuleIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> RuleOut:
    if payload.event_kind and payload.event_kind not in _KNOWN_KINDS:
        log.info(
            "rule create with unknown event_kind=%s — will never fire until the pipeline emits it",
            payload.event_kind,
        )
    import json as _json

    row = await request.app.state.pool.fetchrow(
        """
        INSERT INTO notification_rules
            (name, enabled, event_kind, filter, channel_ids)
        VALUES ($1, $2, $3, $4::jsonb, $5::uuid[])
        RETURNING id, name, enabled, event_kind, filter, channel_ids,
                  last_fired_at, last_error, created_at, updated_at
        """,
        payload.name,
        payload.enabled,
        payload.event_kind,
        _json.dumps(payload.filter.model_dump(mode="json")),
        [str(c) for c in payload.channel_ids],
    )
    await write_audit(
        request.app.state.pool,
        user=user,
        resource_type="notification_rule",
        op="create",
        resource_id=row["id"],
        payload={
            "feature": "notification_rule",
            "name": payload.name,
            "event_kind": payload.event_kind,
        },
    )
    return _row_to_rule(row)


@rules_router.patch(
    "/notifications/rules/{rule_id}",
    response_model=RuleOut,
)
async def patch_rule(
    rule_id: UUID,
    payload: RulePatch,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> RuleOut:
    fields = payload.model_dump(mode="json", exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    import json as _json

    set_clauses: list[str] = []
    args: list[Any] = []
    for i, (k, v) in enumerate(fields.items(), start=1):
        if k == "filter":
            set_clauses.append(f"filter = ${i}::jsonb")
            args.append(_json.dumps(v))
        elif k == "channel_ids":
            set_clauses.append(f"channel_ids = ${i}::uuid[]")
            args.append([str(c) for c in v])
        else:
            set_clauses.append(f"{k} = ${i}")
            args.append(v)
    args.append(rule_id)
    row = await request.app.state.pool.fetchrow(
        f"""
        UPDATE notification_rules
        SET {", ".join(set_clauses)}
        WHERE id = ${len(args)}
        RETURNING id, name, enabled, event_kind, filter, channel_ids,
                  last_fired_at, last_error, created_at, updated_at
        """,  # noqa: S608
        *args,
    )
    if row is None:
        raise HTTPException(404, "rule not found")
    await write_audit(
        request.app.state.pool,
        user=user,
        resource_type="notification_rule",
        op="update",
        resource_id=rule_id,
        payload={"feature": "notification_rule", "fields_changed": list(fields.keys())},
    )
    return _row_to_rule(row)


@rules_router.delete(
    "/notifications/rules/{rule_id}",
    status_code=204,
)
async def delete_rule(
    rule_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = request.app.state.pool
    pre = await pool.fetchrow(
        "SELECT name FROM notification_rules WHERE id = $1",
        rule_id,
    )
    res = await pool.execute(
        "DELETE FROM notification_rules WHERE id = $1",
        rule_id,
    )
    if res.endswith(" 0"):
        raise HTTPException(404, "rule not found")
    await write_audit(
        pool,
        user=user,
        resource_type="notification_rule",
        op="delete",
        resource_id=rule_id,
        payload={"feature": "notification_rule", "name": pre["name"] if pre else None},
    )
