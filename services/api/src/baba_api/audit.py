"""Tiny helper around the `admin_audit` table.

Keep callers honest: pass the AuthUser, the resource type, and the op.
The payload is free-form jsonb; for updates we usually pass
`{"before": {...}, "after": {...}}` with only the diffed fields.

Failures here are caught + logged but never re-raised. Audit is a
secondary concern — a failure to record "operator deleted camera X"
shouldn't roll back the delete itself. The pattern lets us add
`spawn(write_audit(...))` later for fire-and-forget
without changing callers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from uuid import UUID

import asyncpg

from baba_api.auth import AuthUser

log = logging.getLogger(__name__)

# The whole vocabulary of the log: the filter accepts these and the web names
# each one, so a new kind of row is added here or the gate refuses it.
AuditResource = Literal[
    "audit",
    "camera",
    "detection_rule_camera",
    "detection_rule_global",
    "face_recognition",
    "notification_channel",
    "notification_rule",
    "recordings",
    "scene_region",
    "settings",
    "telemetry_incident",
    "telemetry_settings",
    "tunables",
    "user",
    "zone",
]
AuditOp = Literal[
    "apply",
    "capture",
    "create",
    "delete",
    "dismiss",
    "prototype_delete",
    "purge",
    "rename_slug",
    "revert",
    "settings",
    "update",
    "upsert",
]


async def write_audit(
    pool_or_conn: asyncpg.Pool | asyncpg.Connection,
    *,
    user: AuthUser | None,
    resource_type: AuditResource,
    op: AuditOp,
    resource_id: UUID | str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert an admin_audit row. Never raises — secondary concern."""
    try:
        # asyncpg's Pool.execute and Connection.execute have the same
        # signature for parameterised queries, so the two-type union is
        # safe. Pass a Connection inside a transaction when the caller
        # wants atomic CRUD + audit; pass the Pool otherwise.
        await pool_or_conn.execute(
            """
            INSERT INTO admin_audit
                (user_id, resource_type, resource_id, op, payload)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            user.id if user is not None else None,
            resource_type,
            UUID(str(resource_id)) if resource_id is not None else None,
            op,
            json.dumps(payload or {}),
        )
    except Exception:
        log.exception(
            "audit: failed to record %s %s op=%s (continuing)",
            resource_type,
            resource_id,
            op,
        )


def diff_fields(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return only the changed fields for an update audit payload.

    Keeps the JSONB small even when a partial PATCH only touches one
    of fifteen columns. Both dicts are expected to have the same keys
    (caller pre-filters by what the patch actually touched).
    """
    diff_before: dict[str, Any] = {}
    diff_after: dict[str, Any] = {}
    for k in before.keys() | after.keys():
        b = before.get(k)
        a = after.get(k)
        if b != a:
            diff_before[k] = b
            diff_after[k] = a
    return {"before": diff_before, "after": diff_after}
