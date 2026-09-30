"""Read-only admin audit log endpoint.

`admin_audit` is populated by `write_audit()` calls in the CRUD routes.
These endpoints expose the log to the UI so an operator can answer "who
changed X and when", and let an admin retention-purge it.

Pagination is cursor-based on `at DESC` — same shape as events.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from baba_api.audit import AuditOp, AuditResource, write_audit
from baba_api.auth import AuthUser, require_admin
from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

audit_router = APIRouter()


class AuditEntry(BaseModel):
    id: str
    at: datetime
    user_id: str | None
    username: str | None
    resource_type: str
    resource_id: str | None
    op: str
    payload: dict[str, Any]


@audit_router.get("/audit", response_model=list[AuditEntry])
async def list_audit(
    request: Request,
    resource_type: AuditResource | None = Query(default=None),
    resource_id: UUID | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    op: AuditOp | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[AuditEntry]:
    """Admin audit timeline, newest first.

    All filters optional. `until` is strict-less-than to support
    cursor-based pagination (pass the oldest visible row's `at`).
    Joined with users so the UI can show a username instead of a UUID;
    the join is LEFT because system-initiated rows have NULL user_id.
    """
    pool = request.app.state.pool
    flt = SqlFilter()

    if resource_type is not None:
        flt.add("a.resource_type = ?", resource_type)
    if resource_id is not None:
        flt.add("a.resource_id = ?", resource_id)
    if user_id is not None:
        flt.add("a.user_id = ?", user_id)
    if op is not None:
        flt.add("a.op = ?", op)
    if since is not None:
        flt.add("a.at >= ?", since)
    if until is not None:
        flt.add("a.at < ?", until)

    where = flt.where()
    limit_ph = flt.bind(limit)
    sql = f"""
        SELECT
            a.id, a.at, a.user_id, a.resource_type, a.resource_id, a.op, a.payload,
            u.username AS username
        FROM admin_audit a
        LEFT JOIN users u ON u.id = a.user_id
        {where}
        ORDER BY a.at DESC
        LIMIT {limit_ph}
    """  # noqa: S608
    rows = await pool.fetch(sql, *flt.args)
    out: list[AuditEntry] = []
    for r in rows:
        # asyncpg returns jsonb as either dict or str depending on
        # codec registration; handle both.
        raw_payload = r["payload"]
        if isinstance(raw_payload, str):
            import json

            payload = json.loads(raw_payload)
        else:
            payload = dict(raw_payload) if raw_payload is not None else {}
        out.append(
            AuditEntry(
                id=str(r["id"]),
                at=r["at"],
                user_id=str(r["user_id"]) if r["user_id"] is not None else None,
                username=r["username"],
                resource_type=r["resource_type"],
                resource_id=str(r["resource_id"]) if r["resource_id"] is not None else None,
                op=r["op"],
                payload=payload,
            )
        )
    return out


class AuditFacets(BaseModel):
    resource_types: list[str]
    ops: list[str]


@audit_router.get("/audit/facets", response_model=AuditFacets)
async def audit_facets(request: Request) -> AuditFacets:
    """What the log actually holds, so the filters offer nothing that finds no row."""
    rows = await request.app.state.pool.fetch(
        "SELECT DISTINCT resource_type, op FROM admin_audit"
    )
    return AuditFacets(
        resource_types=sorted({r["resource_type"] for r in rows}),
        ops=sorted({r["op"] for r in rows}),
    )


class PurgeResponse(BaseModel):
    deleted: int
    older_than: datetime


@audit_router.delete("/audit", response_model=PurgeResponse)
async def purge_audit(
    request: Request,
    older_than_days: int = Query(..., ge=0, le=3650),
    admin: AuthUser = Depends(require_admin),
) -> PurgeResponse:
    """Retention-purge audit rows older than `older_than_days`.

    Admin-only. The purge itself is recorded as a meta-audit row
    (resource_type="audit", op="purge") so the cleanup is itself
    audited — a row that says "admin X purged N entries older than Y"
    stays in the log even after the purge runs. `older_than_days=0`
    nukes everything except the meta-row this call writes."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        deleted = await conn.fetchval(
            "WITH d AS (DELETE FROM admin_audit WHERE at < $1 RETURNING 1) SELECT count(*) FROM d",
            cutoff,
        )
        # Write the meta-row inside the same transaction so a
        # crash between DELETE and the meta-row write can't leave
        # the log silently truncated.
        await write_audit(
            conn,
            user=admin,
            resource_type="audit",
            op="purge",
            resource_id=None,
            payload={
                "deleted": int(deleted or 0),
                "older_than_days": older_than_days,
                "older_than": cutoff.isoformat(),
            },
        )
    return PurgeResponse(deleted=int(deleted or 0), older_than=cutoff)
