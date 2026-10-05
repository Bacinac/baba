from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from home_core.auth import encode_session_token, hash_password, verify_password
from pydantic import BaseModel, Field

from baba_api.auth import SESSION_COOKIE, TOKEN_TTL, AuthUser, current_user, session_user
from baba_api.rate_limit import LOGIN

log = logging.getLogger(__name__)

auth_router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    # Optional 6-digit TOTP code. Required when the user has 2FA
    # enabled — login returns 401 with detail="2fa_required" if the
    # code is missing, so the frontend can show a follow-up prompt.
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)


class MeResponse(BaseModel):
    id: str
    username: str
    role: str


@auth_router.post("/auth/login", response_model=MeResponse)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    _rl: None = Depends(LOGIN.enforce_client),
) -> MeResponse:
    LOGIN.enforce_account(body.username)
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT id, username, password_hash, role, token_version, "
        "       totp_secret, totp_enabled "
        "FROM users WHERE username = $1",
        body.username,
    )
    if not await verify_password(body.password, row["password_hash"] if row is not None else None):
        # Deliberately vague — don't reveal which of username/password was wrong.
        raise HTTPException(401, "invalid username or password")

    # 2FA gate. Password verified; now require the TOTP code if the
    # user has 2FA enabled. The 412 + "2fa_required" detail tells the
    # frontend to prompt for the code without bouncing back to the
    # username field. A wrong code falls through to "invalid totp_code"
    # so brute-force on the 1M-space 6-digit code surfaces clearly.
    if row["totp_enabled"] and row["totp_secret"]:
        if not body.totp_code:
            raise HTTPException(412, "2fa_required")
        import pyotp

        totp = pyotp.TOTP(row["totp_secret"])
        # valid_window=1 = accept the previous + next 30s window too,
        # which forgives clock drift up to ~30s on the user's device.
        passed = totp.verify(body.totp_code.strip(), valid_window=1)
        if not passed:
            # Fall back to recovery codes — operator's escape hatch
            # when their authenticator is unreachable. _consume marks
            # the matched code used so it can't be replayed.
            passed = await _consume_recovery_code(pool, row["id"], body.totp_code)
        if not passed:
            raise HTTPException(401, "invalid totp_code")

    await pool.execute(
        "UPDATE users SET last_login_at = $1 WHERE id = $2",
        datetime.now(UTC),
        row["id"],
    )

    token = encode_session_token(
        row["id"],
        request.app.state.secret_key,
        token_version=int(row["token_version"]),
        ttl=TOKEN_TTL,
    )
    SESSION_COOKIE.set(response, token, request)
    return MeResponse(id=str(row["id"]), username=row["username"], role=row["role"])


@auth_router.post("/auth/logout", status_code=204)
async def logout(response: Response) -> None:
    SESSION_COOKIE.clear(response)


@auth_router.get("/auth/me", response_model=MeResponse)
async def me(user: AuthUser = Depends(current_user)) -> MeResponse:
    return MeResponse(id=str(user.id), username=user.username, role=user.role)


class ChangePasswordBody(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


# Whitelist of preference keys the API will accept. Anything else gets
# silently dropped on PATCH so a misbehaving client can't pollute the blob.
_PREF_KEYS: frozenset[str] = frozenset(
    {
        "default_landing",
        "time_format_24h",
        "timezone",
        "theme",
        "locale",
        "clip_preroll_s",
        "clip_postroll_s",
        "clip_autoplay",
        "activity_default_range",
    }
)


class PreferencesPatch(BaseModel):
    """Partial preferences update. Only listed keys ever get persisted."""

    default_landing: str | None = Field(None, max_length=255)
    time_format_24h: bool | None = None
    timezone: str | None = Field(None, max_length=64)
    theme: Literal["light", "dark", "system"] | None = None
    locale: Literal["hr", "en"] | None = None
    # Viewing / playback prefs.
    clip_preroll_s: int | None = Field(None, ge=0, le=30)  # lead-in before event
    clip_postroll_s: int | None = Field(None, ge=0, le=30)  # tail after event
    clip_autoplay: bool | None = None  # autoplay clip on open
    activity_default_range: Literal["hour", "today", "yesterday", "7d", "all"] | None = None


@auth_router.post("/auth/change-password", status_code=204)
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    user: AuthUser = Depends(session_user),
) -> None:
    """Self-service password change. Requires the current password — admins
    reset other users via POST /users/{id}/password without that check.

    Bumps `token_version` so every other open session for this user
    (browser tabs on other devices, leaked-cookie attackers) is
    invalidated. Reissues a fresh cookie for THIS session so the user
    isn't logged out from the tab they just changed the password in."""
    pool = request.app.state.pool
    row = await pool.fetchrow("SELECT password_hash FROM users WHERE id = $1", user.id)
    if row is None or not await verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(401, "current password is wrong")
    new_tv_row = await pool.fetchrow(
        "UPDATE users SET password_hash = $1, token_version = token_version + 1 "
        "WHERE id = $2 RETURNING token_version",
        await hash_password(body.new_password),
        user.id,
    )
    new_tv = int(new_tv_row["token_version"]) if new_tv_row else user.token_version + 1
    token = encode_session_token(user.id, request.app.state.secret_key, token_version=new_tv, ttl=TOKEN_TTL)
    SESSION_COOKIE.set(response, token, request)


@auth_router.get("/auth/preferences")
async def get_preferences(
    request: Request,
    user: AuthUser = Depends(session_user),
) -> dict:
    """Current user's preferences blob (jsonb). Missing keys = use defaults."""
    row = await request.app.state.pool.fetchrow(
        "SELECT preferences FROM users WHERE id = $1",
        user.id,
    )
    if row is None:
        return {}
    raw = row["preferences"]
    # asyncpg may hand back jsonb as a string depending on codec config.
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    # Filter to whitelist so we don't expose stale junk that may have leaked in.
    return {k: v for k, v in (raw or {}).items() if k in _PREF_KEYS}


@auth_router.patch("/auth/preferences")
async def patch_preferences(
    body: PreferencesPatch,
    request: Request,
    user: AuthUser = Depends(session_user),
) -> dict:
    """Merge-in update. Pass `null` for a key to explicitly clear it; keys
    not present in the body are left alone."""
    import json

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return await get_preferences(request, user)  # type: ignore[arg-type]
    pool = request.app.state.pool
    # jsonb_strip_nulls drops keys whose new value was JSON null — that's our
    # "explicitly clear this key" affordance.
    row = await pool.fetchrow(
        """
        UPDATE users
        SET preferences = jsonb_strip_nulls(coalesce(preferences, '{}'::jsonb) || $1::jsonb)
        WHERE id = $2
        RETURNING preferences
        """,
        json.dumps(fields),
        user.id,
    )
    if row is None:
        raise HTTPException(404, "user not found")
    raw = row["preferences"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return {k: v for k, v in (raw or {}).items() if k in _PREF_KEYS}


# --------------------------------------------------------------------------
# 2FA TOTP enrollment endpoints
# --------------------------------------------------------------------------


class TotpStatusResponse(BaseModel):
    enabled: bool
    # Operator-visible state. Three phases: never enrolled (no secret),
    # enrolling (secret set, enabled=false — operator is scanning QR
    # and about to confirm), enabled (secret set, enabled=true).
    enrolling: bool


class TotpSetupResponse(BaseModel):
    # base32-encoded shared secret. Returned ONLY at setup time so the
    # operator can paste it manually if scanning fails; never returned
    # again afterwards.
    secret: str
    # otpauth:// URI ready for a QR generator. Authenticator apps
    # parse this directly via the QR endpoint below.
    otpauth_uri: str


class TotpConfirmBody(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


class TotpDisableBody(BaseModel):
    # Require the current password to disable — otherwise a stolen
    # session cookie could clear the second factor it's supposed to
    # protect against.
    password: str = Field(..., min_length=1, max_length=256)


class RecoveryCodesResponse(BaseModel):
    # Returned ONCE, at enable or regenerate time. The operator must
    # write them down — re-fetching them is intentionally impossible
    # (the backend only stores argon2 hashes).
    codes: list[str]


class RegenerateBody(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


# 10 codes × 8 chars (Crockford base32 alphabet, avoids 0/O 1/I confusion).
# Plenty for "lost my phone, here's the spare" but bounded so the
# operator's list fits on one note. argon2 verify is O(login-attempt)
# which keeps a leaked DB from being brute-forceable in reasonable
# time even against the 32^8 ≈ 10^12 space.
_RECOVERY_CODE_COUNT = 10
_RECOVERY_CODE_LEN = 8
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def _new_recovery_codes() -> list[str]:
    import secrets

    return [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_LEN))
        for _ in range(_RECOVERY_CODE_COUNT)
    ]


def _normalise_code(raw: str) -> str:
    """Recovery codes are case-insensitive + ignore whitespace + dashes
    so an operator reading from a paper note can group them however
    feels natural ("ABCD-EFGH" or "abcd efgh" all match the canonical
    "ABCDEFGH"). Applied at hash-time AND at verify-time."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


async def _store_recovery_codes(pool, user_id, plain_codes: list[str]) -> None:
    """Wipe any existing codes for this user, hash + insert the new set.
    Done in one transaction so a partial failure doesn't leave the
    operator with mixed-generation codes."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM user_recovery_codes WHERE user_id = $1",
            user_id,
        )
        for plain in plain_codes:
            await conn.execute(
                "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES ($1, $2)",
                user_id,
                await hash_password(_normalise_code(plain)),
            )


async def _consume_recovery_code(pool, user_id, raw_code: str) -> bool:
    """Try to match `raw_code` against an unused recovery code. Returns
    True if matched + marks the row used. Returns False without side
    effects if no match."""
    candidate = _normalise_code(raw_code)
    if not candidate:
        return False
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, code_hash FROM user_recovery_codes WHERE user_id = $1 AND used_at IS NULL",
            user_id,
        )
        for r in rows:
            if await verify_password(candidate, r["code_hash"]):
                consumed = await conn.fetchval(
                    "UPDATE user_recovery_codes SET used_at = now() "
                    "WHERE id = $1 AND used_at IS NULL RETURNING id",
                    r["id"],
                )
                return consumed is not None
    return False


def _totp_issuer() -> str:
    """Branding string the authenticator app shows next to the entry.
    Hardcoded for now; future per-deployment config could override
    via an env var if operators want it to say something other than
    `BABA`."""
    return "BABA"


@auth_router.get("/auth/2fa/status", response_model=TotpStatusResponse)
async def totp_status(
    request: Request,
    user: AuthUser = Depends(session_user),
) -> TotpStatusResponse:
    row = await request.app.state.pool.fetchrow(
        "SELECT totp_secret, totp_enabled FROM users WHERE id = $1",
        user.id,
    )
    enabled = bool(row and row["totp_enabled"])
    enrolling = bool(row and row["totp_secret"] and not row["totp_enabled"])
    return TotpStatusResponse(enabled=enabled, enrolling=enrolling)


@auth_router.post("/auth/2fa/setup", response_model=TotpSetupResponse)
async def totp_setup(
    request: Request,
    user: AuthUser = Depends(session_user),
) -> TotpSetupResponse:
    """Generate a fresh secret + provisioning URI. Idempotent for a
    user mid-enrolment: the same secret is returned until they confirm
    or disable. Once enabled, calling this again is a hard error —
    operator must disable + re-setup (preserves the "enabling 2FA was
    deliberate" audit trail)."""
    import pyotp

    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT totp_secret, totp_enabled FROM users WHERE id = $1",
        user.id,
    )
    if row is None:
        raise HTTPException(404, "user not found")
    if row["totp_enabled"]:
        raise HTTPException(409, "2fa_already_enabled")

    secret = row["totp_secret"] or pyotp.random_base32()
    if not row["totp_secret"]:
        await pool.execute(
            "UPDATE users SET totp_secret = $1 WHERE id = $2",
            secret,
            user.id,
        )
    totp = pyotp.TOTP(secret)
    otpauth = totp.provisioning_uri(name=user.username, issuer_name=_totp_issuer())
    return TotpSetupResponse(secret=secret, otpauth_uri=otpauth)


@auth_router.get("/auth/2fa/qr")
async def totp_qr(
    request: Request,
    user: AuthUser = Depends(session_user),
):
    """Render the operator's current otpauth URI as an SVG QR code.
    Available throughout enrolment (secret present + enabled false).
    Once enabled, the QR is wiped from response — viewing it after
    enrolment adds nothing and could leak the secret if the same
    machine is later shared."""
    import io

    import pyotp
    import qrcode
    import qrcode.image.svg as qsvg
    from fastapi.responses import Response

    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT totp_secret, totp_enabled FROM users WHERE id = $1",
        user.id,
    )
    if row is None or not row["totp_secret"]:
        raise HTTPException(404, "2fa_not_enrolling")
    if row["totp_enabled"]:
        raise HTTPException(409, "2fa_already_enabled")

    totp = pyotp.TOTP(row["totp_secret"])
    uri = totp.provisioning_uri(name=user.username, issuer_name=_totp_issuer())

    # SvgPathImage is the smallest of qrcode's SVG factories — one
    # <path> element instead of one <rect> per module.
    img = qrcode.make(uri, image_factory=qsvg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"cache-control": "no-store"},
    )


@auth_router.post("/auth/2fa/enable", response_model=RecoveryCodesResponse)
async def totp_enable(
    body: TotpConfirmBody,
    request: Request,
    user: AuthUser = Depends(session_user),
) -> RecoveryCodesResponse:
    """Verify the operator's first code from their authenticator,
    flip totp_enabled=true, and return one-time recovery codes the
    operator should write down. The codes are the only out the
    operator has if they lose their authenticator — without them they
    need an admin to disable 2FA on their behalf."""
    import pyotp

    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT totp_secret, totp_enabled FROM users WHERE id = $1",
        user.id,
    )
    if row is None or not row["totp_secret"]:
        raise HTTPException(404, "2fa_not_enrolling")
    if row["totp_enabled"]:
        raise HTTPException(409, "2fa_already_enabled")
    totp = pyotp.TOTP(row["totp_secret"])
    if not totp.verify(body.code.strip(), valid_window=1):
        raise HTTPException(401, "invalid totp_code")
    await pool.execute(
        "UPDATE users SET totp_enabled = true WHERE id = $1",
        user.id,
    )
    codes = _new_recovery_codes()
    await _store_recovery_codes(pool, user.id, codes)
    return RecoveryCodesResponse(codes=codes)


@auth_router.post(
    "/auth/2fa/recovery-codes/regenerate",
    response_model=RecoveryCodesResponse,
)
async def totp_regenerate_recovery(
    body: RegenerateBody,
    request: Request,
    user: AuthUser = Depends(session_user),
) -> RecoveryCodesResponse:
    """Replace the operator's recovery codes with a fresh set. Useful
    after one code has been used (the spare set is now one short) or
    if the operator suspects the printed copy was seen by someone else.
    Requires current password — same gate as disable, for the same
    reason (cookie theft shouldn't let an attacker rotate codes)."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT password_hash, totp_enabled FROM users WHERE id = $1",
        user.id,
    )
    if row is None or not await verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "wrong_password")
    if not row["totp_enabled"]:
        raise HTTPException(409, "2fa_not_enabled")
    codes = _new_recovery_codes()
    await _store_recovery_codes(pool, user.id, codes)
    return RecoveryCodesResponse(codes=codes)


@auth_router.post("/auth/2fa/disable", status_code=204)
async def totp_disable(
    body: TotpDisableBody,
    request: Request,
    user: AuthUser = Depends(session_user),
) -> None:
    """Disable 2FA. Requires the current password — a stolen cookie
    must not be able to remove the second factor protecting against
    cookie theft. Wipes the secret so re-enrolment gets a fresh one."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT password_hash FROM users WHERE id = $1",
        user.id,
    )
    if row is None or not await verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "wrong_password")
    # Wipe both the secret AND any leftover recovery codes. The
    # ON DELETE CASCADE on user_recovery_codes also handles this if
    # the user is deleted entirely; explicit DELETE here is so a
    # disable+re-enable round-trip doesn't keep stale codes around.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_enabled = false WHERE id = $1",
            user.id,
        )
        await conn.execute(
            "DELETE FROM user_recovery_codes WHERE user_id = $1",
            user.id,
        )
