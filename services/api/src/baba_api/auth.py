"""Who is signed in to BABA: users by UUID with a role, and the peer key a
sister system presents. Passwords, the signed session token and its cookie are
home_core.auth.

Stateless JWT keeps the DB out of the per-request hot path; a password change,
reset or role change bumps the user's `token_version`, which every older token
no longer matches.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import asyncpg
from fastapi import Depends, HTTPException, Request
from home_core.auth import SessionCookie, decode_session_token

TOKEN_TTL = timedelta(days=7)

SESSION_COOKIE = SessionCookie(
    "baba_session",
    TOKEN_TTL,
    always_secure=os.environ.get("BABA_COOKIE_SECURE", "false").lower() == "true",
)

# --- Peer (system-to-system) authorization ------------------------------------
# A sister SYSTEM (DIDA's server-side camera proxy pulling events/clips) is not
# a person: no users row, no password flow, no cookie. A request carrying
# `X-Peer-Key` matching an entry in BABA_PEER_KEYS (CSV of `name:key`)
# authenticates as the synthetic read-only viewer `peer:<name>`. Rotation and
# revocation = edit the env + restart the api; nothing in the DB. This is the
# explicit form of the trust NATS/go2rtc already extend to DIDA at network level.
PEER_HEADER = "X-Peer-Key"
_PEER_UUID = UUID(int=0)


def _peer_from_key(presented: str) -> str | None:
    """Name of the peer whose key matches, or None. Constant-time compares."""
    for item in os.environ.get("BABA_PEER_KEYS", "").split(","):
        name, _, key = item.strip().partition(":")
        if name and key and hmac.compare_digest(presented, key):
            return name
    return None


@dataclass(slots=True, frozen=True)
class AuthUser:
    id: UUID
    username: str
    role: str
    # Per-user JWT revocation counter. Bumped on password change /
    # admin password reset / role change. current_user compares against
    # the token's `tv` claim and rejects stale tokens.
    token_version: int = 0

    @property
    def is_peer(self) -> bool:
        return self.id == _PEER_UUID


async def fetch_user(pool: asyncpg.Pool, user_id: UUID) -> AuthUser | None:
    row = await pool.fetchrow(
        "SELECT id, username, role, token_version FROM users WHERE id = $1",
        user_id,
    )
    if row is None:
        return None
    return AuthUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        token_version=int(row["token_version"]),
    )


async def current_user(request: Request) -> AuthUser:
    """FastAPI dependency. Reads cookie, validates, fetches user, and
    confirms the token's `tv` claim still matches the live DB row.
    A mismatch means the user changed password / had a password reset /
    was role-changed since the token was minted — reject with 401 so
    the client falls back to the login screen."""
    presented = request.headers.get(PEER_HEADER)
    if presented:
        peer = _peer_from_key(presented)
        if peer is None:
            raise HTTPException(401, "invalid peer key")
        return AuthUser(id=_PEER_UUID, username=f"peer:{peer}", role="viewer")
    token = request.cookies.get(SESSION_COOKIE.name)
    if not token:
        raise HTTPException(401, "not authenticated")
    state = request.app.state
    user_id, token_tv = decode_session_token(token, state.secret_key, UUID)
    user = await fetch_user(state.pool, user_id)
    if user is None:
        # Token was valid but user was deleted — surface as unauthenticated.
        raise HTTPException(401, "user no longer exists")
    if user.token_version != token_tv:
        # Per-user JWT revocation. See note above.
        raise HTTPException(401, "session revoked, please sign in again")
    return user


# Role hierarchy: viewer (read-only) < operator (read + operational writes) <
# admin (everything, incl. user/config/secrets). Unknown roles rank below
# viewer so a corrupt/legacy role value is denied, not silently elevated.
_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def session_user(user: AuthUser = Depends(current_user)) -> AuthUser:
    """A signed-in person, for the routes that change their own account. A
    peer key speaks for a service, which has no account to change."""
    if user.is_peer:
        raise HTTPException(403, "a peer key has no account")
    return user


def role_at_least(role: str, minimum: str) -> bool:
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK[minimum]


async def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    """Stricter variant of current_user — also enforces admin role."""
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def require_role(minimum: str):
    """Dependency factory: authenticated AND role >= `minimum` for ALL methods.
    Use for routers whose reads are themselves privileged (user list, audit
    trail)."""

    async def _dep(user: AuthUser = Depends(current_user)) -> AuthUser:
        if not role_at_least(user.role, minimum):
            raise HTTPException(403, f"requires {minimum} role or higher")
        return user

    return _dep


def require_role_for_writes(minimum: str):
    """Dependency factory: any authenticated user may read (GET/HEAD/OPTIONS);
    mutating methods (POST/PUT/PATCH/DELETE) require role >= `minimum`.

    This is the workhorse for RBAC enforcement — attached at router-include
    time so a `viewer` can browse but never mutate, and config/secret routers
    additionally require `admin` to write."""

    async def _dep(
        request: Request, user: AuthUser = Depends(current_user)
    ) -> AuthUser:
        if request.method in _READ_METHODS:
            return user
        if not role_at_least(user.role, minimum):
            raise HTTPException(403, f"{minimum} role required for this action")
        return user

    return _dep


def peer_or_operator_writes():
    """Reads for anyone authenticated; writes for an operator OR a peer.

    Deliberately its own dependency and not a flag on `require_role_for_writes`,
    which would be a thing to sprinkle. A peer is a read-only viewer everywhere
    else and stays one: this exists for the single surface where the house is
    the one who knows the answer — DIDA measures the light outside and turns the
    camera's floodlight on for it — and where letting BABA form a second opinion
    would give the house two notions of darkness.

    It is not a widening of what a peer key can reach in general. Attach it to
    that router and nothing else, and if a second such surface ever appears,
    make the reasoning for it as explicit as this one.
    """

    async def _dep(
        request: Request, user: AuthUser = Depends(current_user)
    ) -> AuthUser:
        if request.method in _READ_METHODS:
            return user
        if user.is_peer or role_at_least(user.role, "operator"):
            return user
        raise HTTPException(403, "operator role or a peer key required")

    return _dep
