"""Notification channels CRUD + test-send.

Channel config (URLs, tokens, SMTP passwords) is encrypted at rest via
the same Fernet wrapper that protects AI provider credentials. Read
endpoints never return the raw config — only metadata + the operator-
supplied `target_preview` so listings can show "smtp ops@example.com"
or "slack #alerts" without decrypt access.

This module handles only the inventory and the manual "test" button.
Rule binding (which event triggers which channel) is a follow-up once
the rules engine lands.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.crypto import decrypt_secret, encrypt_secret
from baba_api.notifications import NotificationError, dispatch
from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

notifications_router = APIRouter()


_ALLOWED_KINDS = {"webhook", "slack", "telegram", "smtp"}


class ChannelOut(BaseModel):
    id: str
    name: str
    kind: str
    enabled: bool
    target_preview: str | None
    last_used_at: datetime | None
    last_error: str | None
    # 0 = unlimited. Otherwise: cap on deliveries per minute through
    # this channel. Excess attempts are recorded in notification_deliveries
    # with ok=false and error="rate_limited" so the operator can see
    # what got dropped + tune the limit up if alerts are missing.
    rate_limit_per_min: int
    created_at: datetime
    updated_at: datetime


class ChannelIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., max_length=32)
    enabled: bool = True
    # Per-kind config blob. Shape validated post-decode; see send_*
    # functions in notifications.py for required keys per kind.
    config: dict[str, Any]
    target_preview: str | None = Field(default=None, max_length=200)
    rate_limit_per_min: int = Field(default=0, ge=0, le=100_000)


class ChannelPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    # Optional — if omitted, existing ciphertext is kept (so partial
    # updates don't force re-entering secrets). Pass to overwrite.
    config: dict[str, Any] | None = None
    target_preview: str | None = Field(default=None, max_length=200)
    rate_limit_per_min: int | None = Field(default=None, ge=0, le=100_000)


class TestResult(BaseModel):
    ok: bool
    error: str | None = None


class DeliveryRow(BaseModel):
    id: str
    at: datetime
    rule_id: str | None
    rule_name: str | None
    channel_id: str | None
    channel_name: str | None
    event_id: str | None
    event_kind: str
    channel_kind: str
    ok: bool
    error: str | None
    duration_ms: int | None


def _row_to_channel(row: Any) -> ChannelOut:
    return ChannelOut(
        id=str(row["id"]),
        name=row["name"],
        kind=row["kind"],
        enabled=row["enabled"],
        target_preview=row["target_preview"],
        last_used_at=row["last_used_at"],
        last_error=row["last_error"],
        rate_limit_per_min=int(row.get("rate_limit_per_min", 0) or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_kind(kind: str) -> None:
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(
            400,
            f"unknown kind {kind!r}; supported: {sorted(_ALLOWED_KINDS)}",
        )


def _secret_key(request: Request) -> str:
    sk = getattr(request.app.state, "secret_key", None)
    if not sk:
        raise HTTPException(500, "secret key not configured")
    return sk


@notifications_router.get("/notifications/channels", response_model=list[ChannelOut])
async def list_channels(request: Request) -> list[ChannelOut]:
    rows = await request.app.state.pool.fetch(
        """
        SELECT id, name, kind, enabled, target_preview,
               last_used_at, last_error, rate_limit_per_min,
               created_at, updated_at
        FROM notification_channels
        ORDER BY created_at ASC
        """
    )
    return [_row_to_channel(r) for r in rows]


@notifications_router.post(
    "/notifications/channels",
    response_model=ChannelOut,
    status_code=201,
)
async def create_channel(
    payload: ChannelIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> ChannelOut:
    _validate_kind(payload.kind)
    ct = encrypt_secret(json.dumps(payload.config), _secret_key(request))
    row = await request.app.state.pool.fetchrow(
        """
        INSERT INTO notification_channels
            (name, kind, enabled, config_encrypted, target_preview,
             rate_limit_per_min)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, name, kind, enabled, target_preview,
                  last_used_at, last_error, rate_limit_per_min,
                  created_at, updated_at
        """,
        payload.name,
        payload.kind,
        payload.enabled,
        ct,
        payload.target_preview,
        payload.rate_limit_per_min,
    )
    await write_audit(
        request.app.state.pool,
        user=user,
        resource_type="notification_channel",
        op="create",
        resource_id=row["id"],
        payload={"name": payload.name, "kind": payload.kind, "feature": "notification_channel"},
    )
    return _row_to_channel(row)


@notifications_router.patch(
    "/notifications/channels/{channel_id}",
    response_model=ChannelOut,
)
async def patch_channel(
    channel_id: UUID,
    payload: ChannelPatch,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> ChannelOut:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")

    # Encrypt config in-place if provided so the SQL builder loop
    # below treats it as a plain bytea column.
    if "config" in fields and fields["config"] is not None:
        fields["config_encrypted"] = encrypt_secret(
            json.dumps(fields["config"]),
            _secret_key(request),
        )
        del fields["config"]
    elif "config" in fields:
        del fields["config"]

    set_clauses: list[str] = []
    args: list[Any] = []
    for i, (k, v) in enumerate(fields.items(), start=1):
        set_clauses.append(f"{k} = ${i}")
        args.append(v)
    args.append(channel_id)
    row = await request.app.state.pool.fetchrow(
        f"""
        UPDATE notification_channels
        SET {", ".join(set_clauses)}
        WHERE id = ${len(args)}
        RETURNING id, name, kind, enabled, target_preview,
                  last_used_at, last_error, rate_limit_per_min,
                  created_at, updated_at
        """,  # noqa: S608
        *args,
    )
    if row is None:
        raise HTTPException(404, "channel not found")
    await write_audit(
        request.app.state.pool,
        user=user,
        resource_type="notification_channel",
        op="update",
        resource_id=channel_id,
        payload={"feature": "notification_channel", "fields_changed": list(fields.keys())},
    )
    return _row_to_channel(row)


@notifications_router.delete(
    "/notifications/channels/{channel_id}",
    status_code=204,
)
async def delete_channel(
    channel_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = request.app.state.pool
    pre = await pool.fetchrow(
        "SELECT name, kind FROM notification_channels WHERE id = $1",
        channel_id,
    )
    res = await pool.execute(
        "DELETE FROM notification_channels WHERE id = $1",
        channel_id,
    )
    if res.endswith(" 0"):
        raise HTTPException(404, "channel not found")
    await write_audit(
        pool,
        user=user,
        resource_type="notification_channel",
        op="delete",
        resource_id=channel_id,
        payload={
            "feature": "notification_channel",
            "name": pre["name"] if pre else None,
            "kind": pre["kind"] if pre else None,
        },
    )


@notifications_router.post(
    "/notifications/channels/{channel_id}/test",
    response_model=TestResult,
)
async def test_channel(
    channel_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> TestResult:
    """Send a small operator-triggered test payload through the channel.
    Updates `last_used_at` (success or failure) and `last_error` (only
    on failure) so the listing UI can show a status badge."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT kind, config_encrypted FROM notification_channels WHERE id = $1",
        channel_id,
    )
    if row is None:
        raise HTTPException(404, "channel not found")

    try:
        config = json.loads(
            decrypt_secret(bytes(row["config_encrypted"]), _secret_key(request)),
        )
    except (ValueError, json.JSONDecodeError) as e:
        await pool.execute(
            "UPDATE notification_channels SET last_used_at = $1, last_error = $2 WHERE id = $3",
            datetime.now(UTC),
            f"config decrypt failed: {e}",
            channel_id,
        )
        return TestResult(ok=False, error="config decrypt failed (rotate BABA_SECRET_KEY?)")

    payload = {
        "title": "BABA test notification",
        "subject": "BABA test notification",
        "body": (
            f"Triggered by {user.username} at {datetime.now(UTC).isoformat(timespec='seconds')}."
        ),
        "test": True,
    }
    try:
        await dispatch(row["kind"], config, payload)
        await pool.execute(
            "UPDATE notification_channels SET last_used_at = $1, last_error = NULL WHERE id = $2",
            datetime.now(UTC),
            channel_id,
        )
        return TestResult(ok=True)
    except NotificationError as e:
        await pool.execute(
            "UPDATE notification_channels SET last_used_at = $1, last_error = $2 WHERE id = $3",
            datetime.now(UTC),
            str(e),
            channel_id,
        )
        return TestResult(ok=False, error=str(e))


# --------------------------------------------------------------------------
# /notifications/deliveries — per-fire audit timeline
# --------------------------------------------------------------------------


@notifications_router.get(
    "/notifications/deliveries",
    response_model=list[DeliveryRow],
)
async def list_deliveries(
    request: Request,
    rule_id: UUID | None = None,
    channel_id: UUID | None = None,
    ok: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[DeliveryRow]:
    """Per-delivery timeline, newest first. Use `until` as cursor for
    pagination (pass oldest visible row's `at`). LEFT JOIN to rules +
    channels so the UI shows names; deletes survive (ON DELETE SET NULL
    on FKs) and just show as `NULL` rule_id/channel_id."""
    if limit < 1 or limit > 2000:
        raise HTTPException(400, "limit must be 1..2000")
    pool = request.app.state.pool
    flt = SqlFilter()

    if rule_id is not None:
        flt.add("d.rule_id = ?", rule_id)
    if channel_id is not None:
        flt.add("d.channel_id = ?", channel_id)
    if ok is not None:
        flt.add("d.ok = ?", ok)
    if since is not None:
        flt.add("d.at >= ?", since)
    if until is not None:
        flt.add("d.at < ?", until)

    where = flt.where()
    limit_ph = flt.bind(limit)
    sql = f"""
        SELECT
            d.id, d.at, d.rule_id, d.channel_id, d.event_id,
            d.event_kind, d.channel_kind, d.ok, d.error, d.duration_ms,
            r.name AS rule_name,
            c.name AS channel_name
        FROM notification_deliveries d
        LEFT JOIN notification_rules r ON r.id = d.rule_id
        LEFT JOIN notification_channels c ON c.id = d.channel_id
        {where}
        ORDER BY d.at DESC
        LIMIT {limit_ph}
    """  # noqa: S608
    rows = await pool.fetch(sql, *flt.args)
    return [
        DeliveryRow(
            id=str(r["id"]),
            at=r["at"],
            rule_id=str(r["rule_id"]) if r["rule_id"] is not None else None,
            rule_name=r["rule_name"],
            channel_id=str(r["channel_id"]) if r["channel_id"] is not None else None,
            channel_name=r["channel_name"],
            event_id=str(r["event_id"]) if r["event_id"] is not None else None,
            event_kind=r["event_kind"],
            channel_kind=r["channel_kind"],
            ok=r["ok"],
            error=r["error"],
            duration_ms=r["duration_ms"],
        )
        for r in rows
    ]
