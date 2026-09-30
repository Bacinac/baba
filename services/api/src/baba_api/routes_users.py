"""User management — admin-only CRUD + password reset.

Authorisation:
- Listing, creating, deleting other users, resetting other users' passwords
  all require admin role.
- Patching `username` is allowed on self (any role) or anyone (admin).
- Patching `role` is admin-only; we also forbid demoting the last admin.
- Deleting self is forbidden, regardless of role.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from home_core.auth import hash_password
from pydantic import BaseModel, Field

from baba_api.audit import diff_fields, write_audit
from baba_api.auth import AuthUser, current_user, require_admin

log = logging.getLogger(__name__)

users_router = APIRouter()


_USER_FIELDS = "id, username, role, created_at, updated_at, last_login_at, totp_enabled"
ALLOWED_ROLES = ("admin", "operator", "viewer")


class UserOut(BaseModel):
    id: UUID
    username: str
    role: str
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    # Surfaced so the admin user list can show a "2FA: on" badge + the
    # "Reset 2FA" button is only meaningful when this is true.
    totp_enabled: bool = False


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field("operator")


class UserPatch(BaseModel):
    username: str | None = Field(None, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    role: str | None = None


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=256)


def _validate_role(role: str | None) -> None:
    if role is not None and role not in ALLOWED_ROLES:
        raise HTTPException(400, f"invalid role; allowed: {', '.join(ALLOWED_ROLES)}")


@users_router.get("/users", response_model=list[UserOut])
async def list_users(
    request: Request,
    _admin: AuthUser = Depends(require_admin),
) -> list[UserOut]:
    rows = await request.app.state.pool.fetch(
        f"SELECT {_USER_FIELDS} FROM users ORDER BY created_at ASC"  # noqa: S608
    )
    return [UserOut(**dict(r)) for r in rows]


@users_router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    request: Request,
    admin: AuthUser = Depends(require_admin),
) -> UserOut:
    _validate_role(payload.role)
    try:
        row = await request.app.state.pool.fetchrow(
            f"""
            INSERT INTO users (username, password_hash, role)
            VALUES ($1, $2, $3)
            RETURNING {_USER_FIELDS}
            """,  # noqa: S608
            payload.username,
            await hash_password(payload.password),
            payload.role,
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, f"username {payload.username!r} already exists") from e
        raise
    await write_audit(
        request.app.state.pool,
        user=admin,
        resource_type="user",
        op="create",
        resource_id=row["id"],
        payload={"username": payload.username, "role": payload.role},
    )
    return UserOut(**dict(row))


@users_router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: UUID,
    request: Request,
    current: AuthUser = Depends(current_user),
) -> UserOut:
    if current.role != "admin" and current.id != user_id:
        raise HTTPException(403, "not authorised")
    row = await request.app.state.pool.fetchrow(
        f"SELECT {_USER_FIELDS} FROM users WHERE id = $1",  # noqa: S608
        user_id,
    )
    if row is None:
        raise HTTPException(404, "user not found")
    return UserOut(**dict(row))


@users_router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: UUID,
    payload: UserPatch,
    request: Request,
    current: AuthUser = Depends(current_user),
) -> UserOut:
    if current.role != "admin" and current.id != user_id:
        raise HTTPException(403, "not authorised")
    fields = payload.model_dump(exclude_unset=True)
    if "role" in fields:
        if current.role != "admin":
            raise HTTPException(403, "only admin can change roles")
        _validate_role(fields["role"])
        # Prevent demoting the last admin.
        if current.id == user_id and fields["role"] != "admin":
            n_admins = await request.app.state.pool.fetchval(
                "SELECT count(*) FROM users WHERE role = 'admin'"
            )
            if n_admins <= 1:
                raise HTTPException(400, "cannot demote the last admin")
    if not fields:
        raise HTTPException(400, "no fields to update")

    # Fetch touched fields before the update so the audit can record
    # before/after values. UserPatch only exposes username + role, so
    # there's no password hash or 2FA secret to worry about here.
    field_list = ", ".join(fields.keys())
    before_row = await request.app.state.pool.fetchrow(
        f"SELECT {field_list} FROM users WHERE id = $1",  # noqa: S608
        user_id,
    )
    if before_row is None:
        raise HTTPException(404, "user not found")

    set_clauses: list[str] = []
    args: list = []
    for i, (k, v) in enumerate(fields.items(), start=1):
        set_clauses.append(f"{k} = ${i}")
        args.append(v)
    args.append(user_id)

    try:
        row = await request.app.state.pool.fetchrow(
            f"UPDATE users SET {', '.join(set_clauses)} "  # noqa: S608
            f"WHERE id = ${len(args)} RETURNING {_USER_FIELDS}",
            *args,
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, "username already exists") from e
        raise
    if row is None:
        raise HTTPException(404, "user not found")
    before_d = {k: before_row[k] for k in fields}
    after_d = {k: fields[k] for k in fields}
    await write_audit(
        request.app.state.pool,
        user=current,
        resource_type="user",
        op="update",
        resource_id=user_id,
        payload=diff_fields(before_d, after_d),
    )
    return UserOut(**dict(row))


@users_router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    request: Request,
    admin: AuthUser = Depends(require_admin),
) -> None:
    if admin.id == user_id:
        raise HTTPException(400, "cannot delete yourself")
    target_role = await request.app.state.pool.fetchval(
        "SELECT role FROM users WHERE id = $1",
        user_id,
    )
    if target_role is None:
        raise HTTPException(404, "user not found")
    if target_role == "admin":
        n_admins = await request.app.state.pool.fetchval(
            "SELECT count(*) FROM users WHERE role = 'admin'"
        )
        if n_admins <= 1:
            raise HTTPException(400, "cannot delete the last admin")
    # Capture the username before delete so the audit row is meaningful
    # even after the user record is gone.
    target_name = await request.app.state.pool.fetchval(
        "SELECT username FROM users WHERE id = $1",
        user_id,
    )
    await request.app.state.pool.execute("DELETE FROM users WHERE id = $1", user_id)
    await write_audit(
        request.app.state.pool,
        user=admin,
        resource_type="user",
        op="delete",
        resource_id=user_id,
        payload={"username": target_name, "role": target_role},
    )


@users_router.post("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: UUID,
    payload: PasswordReset,
    request: Request,
    admin: AuthUser = Depends(require_admin),
) -> None:
    """Admin-initiated reset — no need to know the user's previous password.
    For users changing their own password, use POST /auth/change-password
    which verifies the current password first.

    Bumps `token_version` so the target user is logged out from every
    open session (including across browsers / devices). The admin
    triggering the reset stays signed in because their own tv is
    unaffected."""
    row = await request.app.state.pool.fetchrow(
        "UPDATE users SET password_hash = $1, token_version = token_version + 1 "
        "WHERE id = $2 RETURNING id",
        await hash_password(payload.password),
        user_id,
    )
    if row is None:
        raise HTTPException(404, "user not found")
    # The password itself never lands in the audit — just the fact that
    # an admin reset it. Operator-forced resets are a security event
    # worth tracking even though the new value is opaque.
    await write_audit(
        request.app.state.pool,
        user=admin,
        resource_type="user",
        op="update",
        resource_id=user_id,
        payload={"action": "password_reset"},
    )


@users_router.post("/users/{user_id}/2fa/disable", status_code=204)
async def admin_disable_2fa(
    user_id: UUID,
    request: Request,
    admin: AuthUser = Depends(require_admin),
) -> None:
    """Admin escape hatch — wipes the target user's TOTP secret and
    recovery codes so they can sign in with just their password. The
    self-service flow (POST /auth/2fa/disable) requires the user's own
    password as a cookie-theft guard; this endpoint instead requires
    admin role + writes a loud audit row, because the legitimate use
    case is "operator lost phone AND lost recovery codes" and the
    abuse case ("admin yanks 2FA off victim's account") is exactly
    what we want surfaced in the audit log.

    Side effects (all in one transaction so a partial failure leaves
    the account in a consistent state):
      - users.totp_secret = NULL
      - users.totp_enabled = false
      - users.token_version += 1  (severs every open session — admin
        force-disables 2FA usually because the original device is
        compromised or lost, so kicking the old cookies out is the
        right default)
      - DELETE FROM user_recovery_codes WHERE user_id = $1
    """
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT username, totp_enabled FROM users WHERE id = $1",
        user_id,
    )
    if row is None:
        raise HTTPException(404, "user not found")
    # Forbid self-disable through this endpoint — admins should use
    # the self-service /auth/2fa/disable so the password gate applies.
    # Allowing self-disable here would let a compromised admin cookie
    # remove 2FA on the admin's own account without re-auth.
    if admin.id == user_id:
        raise HTTPException(
            400,
            "use /auth/2fa/disable for your own account (requires password)",
        )

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE users "
            "SET totp_secret = NULL, totp_enabled = false, "
            "    token_version = token_version + 1 "
            "WHERE id = $1",
            user_id,
        )
        await conn.execute(
            "DELETE FROM user_recovery_codes WHERE user_id = $1",
            user_id,
        )

    await write_audit(
        pool,
        user=admin,
        resource_type="user",
        op="update",
        resource_id=user_id,
        payload={
            "action": "admin_disable_2fa",
            "target_username": row["username"],
            "was_enabled": bool(row["totp_enabled"]),
        },
    )
