"""The camera's floodlight, for the system that knows when it is dark.

West's light ran on a fixed 21:00-05:00 schedule, which cannot be right twice
in a row: measured on 28.08 it was genuinely dark from 20:00, so the light came
on an hour late, and in December — dark by half four — it would come on four
and a half hours late. The house already measures the thing that schedule is
guessing at, and it is DIDA that holds the measurement.

So the decision lives there and the actuation lives here, which is the only
split that keeps one notion of darkness in the house and one writer to the
camera. See PROPOSAL-CAMERA-CONTROL.md in the DIDA repo.

Reads state off the DEVICE every time. The camera can be changed from the
vendor's own app, and answering from what we last wrote would be answering
about a camera that no longer exists.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.camera_ctl import (
    armed_mode_of,
    creds_for,
    get_light,
    has_no_light,
    note_armed_mode,
    set_light,
)

log = logging.getLogger(__name__)

camera_light_router = APIRouter()


class LightState(BaseModel):
    on: bool
    # Absent leaves the camera's own brightness alone: the operator set it and
    # a caller that only wants the light on should not silently restyle it.
    bright: int | None = Field(default=None, ge=0, le=100)


async def _creds(request: Request, camera_id: UUID):
    """Addressed by UUID, like every other camera route and like the identity a
    consumer binds to — a renamed camera must stay the same entity."""
    row = await request.app.state.pool.fetchrow(
        "SELECT slug, stream_url FROM cameras WHERE id = $1 AND enabled", camera_id
    )
    if row is None:
        raise HTTPException(404, "camera not found")
    creds = creds_for(row["stream_url"])
    if creds is None:
        raise HTTPException(
            404, "camera has no reachable address or login in its stream"
        )
    return row["slug"], creds


def _light_error(slug: str, exc: Exception) -> HTTPException:
    """404 when this camera HAS no light, 502 when it did not answer.

    The two must not be one code. A consumer that cannot tell them apart either
    drops the light entity the first time the camera is briefly away, or keeps
    a light entity forever on a model that has no lamp. The vendor answers a
    missing feature with a clean error, not a connection failure.
    """
    if has_no_light(exc):
        log.info("camera %s has no floodlight this system can reach: %s", slug, exc)
        return HTTPException(404, "camera has no floodlight")
    log.warning("camera %s: the light did not answer: %s", slug, exc)
    return HTTPException(502, f"camera did not answer: {exc}")


@camera_light_router.get("/cameras/{camera_id}/light")
async def read_camera_light(camera_id: UUID, request: Request) -> dict:
    slug, creds = await _creds(request, camera_id)
    try:
        led = await get_light(creds)
    except Exception as e:
        # Never answer "off" for a camera that did not answer at all: a caller
        # would turn it on again forever and call that working.
        raise _light_error(slug, e) from e
    # Learned here rather than chosen by us. A lamp reads as armed for most of
    # the day, so by the time anything switches it off — the write that destroys
    # the only record of how it was armed — this has already seen it.
    await note_armed_mode(request.app.state.pool, camera_id, led.get("mode"))
    return {
        # Armed, not lit. `state` says whether the lamp is burning at this
        # instant under the camera's own trigger and goes back to 0 on its own;
        # a caller polling for idempotency needs the standing answer.
        "on": bool(led.get("mode")),
        "lit": bool(led.get("state")),
        "bright": led.get("bright"),
        # Informational: the vendor field behind `on`, and the camera's own
        # schedule, which stays the fallback for any night this system is down.
        "mode": led.get("mode"),
        "schedule": led.get("LightingSchedule"),
    }


@camera_light_router.post("/cameras/{camera_id}/light")
async def write_camera_light(
    camera_id: UUID, body: LightState, request: Request
) -> dict:
    """Idempotent by construction — the same request twice changes nothing.
    DIDA sends on a state change, and again whenever its automation restarts."""
    slug, creds = await _creds(request, camera_id)
    pool = request.app.state.pool
    try:
        led, was = await set_light(
            creds,
            on=body.on,
            bright=body.bright,
            armed_mode=await armed_mode_of(pool, camera_id),
        )
    except Exception as e:
        raise _light_error(slug, e) from e
    await note_armed_mode(pool, camera_id, was)
    log.info("camera %s light %s", slug, "on" if body.on else "off")
    return {"on": bool(led.get("mode")), "bright": led.get("bright")}
