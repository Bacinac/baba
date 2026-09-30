"""Pipeline tuning: read and write the thresholds the services resolve live.

The form is generated from `baba_core.tunables`, not declared here. That is the
point of the catalogue: adding a knob means adding one row there, and it
appears in the API and in the UI with its range already enforced. A hand-kept
list of fields in this module would be exactly the parallel copy the whole
exercise exists to remove.

`default` in the response is what the DEPLOYMENT falls back to — each service
publishes its env-resolved values into `app_settings.pipeline_defaults` at
startup, because this stack's `.env` genuinely differs from the code defaults
(`BABA_EVENT_MIN_OBSERVATIONS=5` against a code default of 3). Reading them
back is the only way the form can honestly say what "reset" will give you.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from baba_core.tunables import GROUPS
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user, require_role

log = logging.getLogger(__name__)

tunables_router = APIRouter(tags=["tunables"])

DEFAULTS_KEY = "pipeline_defaults"


class Tunable(BaseModel):
    name: str
    group: str
    value: float
    default: float
    lo: float
    hi: float
    help_key: str
    # False = running on the deployment default. The UI badges these as
    # inherited so "unset" and "happens to equal the default" stay distinct.
    is_set: bool


class TunablesIn(BaseModel):
    """Partial update. A key set to null clears the operator value and
    returns that knob to the deployment default — the reset path."""

    values: dict[str, float | None]


async def _row(pool, key: str) -> dict[str, Any]:
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", key)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


@tunables_router.get("/tunables", response_model=list[Tunable])
async def list_tunables(request: Request) -> list[Tunable]:
    pool = request.app.state.pool
    published = await _row(pool, DEFAULTS_KEY)
    out: list[Tunable] = []
    for group, bounds in GROUPS.items():
        stored = await _row(pool, group)
        for name, b in bounds.items():
            # A service that has not started since this shipped has published
            # nothing; fall back to the stored value, then to the low bound,
            # rather than inventing a number the pipeline does not use.
            default = published.get(name)
            if not isinstance(default, int | float):
                default = stored.get(name)
            if not isinstance(default, int | float):
                default = b.lo
            value = stored.get(name)
            is_set = isinstance(value, int | float) and not isinstance(value, bool)
            out.append(
                Tunable(
                    name=name,
                    group=group,
                    value=float(value) if is_set else float(default),
                    default=float(default),
                    lo=b.lo,
                    hi=b.hi,
                    help_key=b.help_key,
                    is_set=is_set,
                )
            )
    return out


@tunables_router.put(
    "/tunables",
    response_model=list[Tunable],
    dependencies=[Depends(require_role("admin"))],
)
async def put_tunables(
    body: TunablesIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> list[Tunable]:
    known = {name: (group, b) for group, bounds in GROUPS.items() for name, b in bounds.items()}
    unknown = sorted(set(body.values) - set(known))
    if unknown:
        raise HTTPException(400, f"unknown tunable(s): {', '.join(unknown)}")

    # Clamp here as well as in the services. The services clamp because a row
    # can be edited by hand; the API clamps so the operator gets a 400-free
    # save that stores what will actually run, instead of a value the pipeline
    # silently corrects behind their back.
    by_group: dict[str, dict[str, float | None]] = {}
    for name, raw in body.values.items():
        group, b = known[name]
        if raw is None:
            by_group.setdefault(group, {})[name] = None
            continue
        v = max(b.lo, min(b.hi, float(raw)))
        by_group.setdefault(group, {})[name] = v

    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        for group, changes in by_group.items():
            current = await _row(pool, group)
            merged = dict(current)
            for name, v in changes.items():
                if v is None:
                    merged.pop(name, None)
                else:
                    merged[name] = v
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                group,
                json.dumps(merged),
            )
        # One NOTIFY for the whole save: the tracker and the event-manager both
        # re-read their row on this channel.
        await conn.execute("SELECT pg_notify('app_settings_changed', 'tunables')")

    await write_audit(
        pool,
        user=user,
        resource_type="tunables",
        op="update",
        payload={"after": {k: v for k, v in body.values.items()}},
    )
    return await list_tunables(request)
