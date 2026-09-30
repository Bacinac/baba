"""The one place that writes to a camera.

Two callers want the device now — DIDA turns West's light on when the house
decides it is dark, and the plate reader swaps West into an IR profile for the
few seconds a headlight is drowning the registration — and two writers to one
camera produce state nobody can explain. Both come through here.

Everything is read-modify-write against the device, never against our own idea
of it: the vendor's app can change a camera without telling us, and the vendor
API rejects a partial settings object anyway (`param error / rspCode -4`,
measured 29.08).

A temporary profile carries a deadline that survives this process. Held only in
memory it is a promise a restart breaks, and the camera would sit in the
override until somebody noticed in the morning — with the operator's own night
view gone. `camera_profile_override` holds the baseline and the moment it must
be undone by; `sweep_overrides()` puts back anything past it, and does so on
startup too, because a row that survived a restart is the crash this exists for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from baba_core.light_profiles import record_change

log = logging.getLogger(__name__)

__all__ = [
    "PLATE_PROFILE",
    "CameraCreds",
    "NotThisApi",
    "apply_profile",
    "armed_mode_of",
    "creds_for",
    "get_light",
    "has_no_light",
    "note_armed_mode",
    "read_settings",
    "restore",
    "serve_profile_requests",
    "set_light",
    "sweep_overrides",
    "write_settings",
]

# The device answers in tens of milliseconds on the LAN (measured: 184-223 ms
# for a settings write). A camera that has stopped answering must fail the
# request rather than hold a request thread.
_TIMEOUT_S = 6.0

# Nothing may hold a camera out of its owner's profile for longer than this,
# whatever the caller asked for. The headlight override wants ~30 s; a bug that
# asks for a day does not get one.
_MAX_OVERRIDE = timedelta(minutes=5)


@dataclass(frozen=True)
class CameraCreds:
    host: str
    user: str
    password: str
    stream_port: int


_DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "http": 80, "https": 443}


def creds_for(stream_url: str) -> CameraCreds | None:
    """The device's own address and login, taken from the stream we already
    hold. There is no second place to keep camera passwords — a second place is
    a second thing to rotate and forget.

    Two shapes, because the same vendor writes both: RTSP puts the login in the
    userinfo, and the HTTP-FLV stream puts it in the query string. Reading only
    the first quietly excluded the camera over the driveway, which answered
    "this camera has no floodlight" while having one.
    """
    try:
        parts = urlsplit(stream_url)
    except ValueError:
        return None
    if not parts.hostname:
        return None
    user, password = parts.username, parts.password
    if not user:
        query = parse_qs(parts.query)
        user = (query.get("user") or [""])[0]
        password = (query.get("password") or [""])[0]
    if not user:
        return None
    try:
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 0)
    except ValueError:
        return None
    return CameraCreds(parts.hostname, user, password or "", port)


class NotThisApi(RuntimeError):
    """The device answered, and does not speak this API.

    Its own kind of failure because it is PERMANENT, and the caller has to be
    able to tell it from a camera that is briefly away. The Hikvision on the
    patio answers this endpoint with an HTTP 404 and a page saying it cannot
    locate the document (measured 29.08) — treating that as "the device did not
    answer" asks a consumer to retry a camera that will never speak, forever.
    """


_API_PORT = 80
_PROBE_TIMEOUT_S = 2.0


async def _accepts(host: str, port: int) -> bool | None:
    """True when the port takes a connection, False when the host refuses it,
    None when nothing answered at all."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), _PROBE_TIMEOUT_S
        )
    except ConnectionRefusedError:
        return False
    except (OSError, TimeoutError):
        return None
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def _refuses_this_api(creds: CameraCreds) -> bool:
    """A camera that is up, streaming, and has nothing listening where this API
    lives.

    Cabin's two TP-Links (measured 15.09) refuse port 80 while 554, 443, 2020
    and 8800 all accept. Read as "the device did not answer", that is a 502 and
    a warning in DIDA every half hour for as long as the cameras stand there.
    Refused alone is not enough: a camera that is rebooting refuses everything
    for a moment, and one whose stream is served on port 80 itself is simply
    away. Only a refusal next to a live stream port is permanent.
    """
    if creds.stream_port in (0, _API_PORT):
        return False
    if await _accepts(creds.host, _API_PORT) is not False:
        return False
    return await _accepts(creds.host, creds.stream_port) is True


# Answers that mean the endpoint is not there at all, as opposed to a request
# it refused. The vendor itself always answers 200 and puts failure in the body.
_NO_SUCH_API = frozenset({404, 405, 501})


async def _call(creds: CameraCreds, cmd: str, body: list[dict]) -> Any:
    """One vendor API call. Raises on anything that is not a clean success —
    a camera that did not take the change must never look like one that did."""
    url = f"http://{creds.host}/cgi-bin/api.cgi"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                params={"cmd": cmd, "user": creds.user, "password": creds.password},
                json=body,
            )
    except httpx.ConnectError as e:
        if await _refuses_this_api(creds):
            raise NotThisApi(
                f"{cmd}: {creds.host} streams on {creds.stream_port} and refuses {url}"
            ) from e
        raise
    if resp.status_code in _NO_SUCH_API:
        raise NotThisApi(f"{cmd}: this camera has no {url}")
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as e:
        raise NotThisApi(f"{cmd}: answer is not this vendor's") from e
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{cmd}: unreadable answer")
    first = payload[0]
    if first.get("code") != 0:
        detail = (first.get("error") or {}).get("detail", "")
        raise RuntimeError(f"{cmd}: {detail or first.get('code')}")
    return first.get("value") or {}


def has_no_light(exc: Exception) -> bool:
    """Whether this failure means the camera HAS no light we can reach, as
    opposed to one that is briefly away.

    The two must not read the same. A consumer that cannot tell them apart
    either drops a light entity the first time a camera blinks, or keeps
    retrying a camera that will never speak this API.
    """
    return isinstance(exc, NotThisApi) or "no light in answer" in str(exc)


async def read_settings(creds: CameraCreds) -> dict:
    """The camera's FULL imaging settings, as the device has them now."""
    value = await _call(
        creds, "GetIsp", [{"cmd": "GetIsp", "action": 0, "param": {"channel": 0}}]
    )
    isp = value.get("Isp")
    if not isinstance(isp, dict):
        raise RuntimeError("GetIsp: no settings in answer")
    return isp


async def write_settings(creds: CameraCreds, changes: dict) -> dict:
    """Apply `changes` on top of what the device currently has, and return the
    settings as they were BEFORE — the caller's baseline.

    The whole object goes back, not the changed keys: a partial one is refused
    outright, which is the sort of thing that looks like a working call in a
    log until somebody checks the camera.
    """
    baseline = await read_settings(creds)
    merged = {**baseline, **changes}
    await _call(
        creds, "SetIsp", [{"cmd": "SetIsp", "action": 0, "param": {"Isp": merged}}]
    )
    return baseline


async def get_light(creds: CameraCreds) -> dict:
    """The floodlight's state, read off the device."""
    value = await _call(
        creds,
        "GetWhiteLed",
        [{"cmd": "GetWhiteLed", "action": 0, "param": {"channel": 0}}],
    )
    led = value.get("WhiteLed")
    if not isinstance(led, dict):
        raise RuntimeError("GetWhiteLed: no light in answer")
    return led


# What "armed" means on the device. Measured 29.08 on West, outside its own
# schedule: writing `state` is a momentary flash, not a state — the camera put
# it back to 0 by itself three seconds later, because in this mode `state`
# REPORTS whether the lamp is lit right now under the camera's AI trigger.
# `mode` is what persists, and it is what "the light is on" has to mean here:
# armed, so the camera lights it when it sees somebody. That is the behaviour
# the operator likes and did not ask anyone to replace — his complaint was only
# that the WINDOW in which it is allowed was a fixed clock.
# And "armed" is not one value across this yard: west is in the AI mode, the
# camera over the gate in its night mode (3 and 1, read off both devices 29.08).
# Writing one of them everywhere would convert the other camera's behaviour the
# first time the house switched its light back on, silently — the lamp lights
# either way. So a camera is put back in the mode IT was armed in, and this is
# only the fallback for one we have never yet seen armed.
_MODE_ARMED = 3
_MODE_OFF = 0

# The device's own timer, opened all the way, because the MODE is the switch and
# the timer is only in its way. Measured on west at 21:04 on 03.09, reading the
# lamp back after each write: mode 3 burns for as long as the timer allows and
# mode 1 does not burn at all — so what decides whether the yard is lit is which
# mode we write, and the window only decides whether that write is allowed to
# mean anything.
#
# Left at the factory 21:00-05:00 it silently overruled everything. Proved from
# the archive on 26.08: at 20:48 a car with headlights and two people in frame,
# the camera armed, the lamp off; at 21:00:45 the luma jumped to 108 and the
# picture went colour. Moving the decision to DIDA, which measures the dark, was
# the whole point, and a fixed clock underneath it cannot be right twice in a
# row — an hour late in August, four and a half in December.
#
# So: open the window, and let on and off be on and off.
_LAMP_RAIL = {"StartHour": 0, "StartMin": 0, "EndHour": 23, "EndMin": 59}


async def set_light(
    creds: CameraCreds,
    *,
    on: bool,
    bright: int | None = None,
    armed_mode: int | None = None,
) -> tuple[dict, int | None]:
    """Arm or disarm the floodlight, optionally setting brightness.

    Returns what was written and the mode the device was in before — that mode
    is the only record of how this lamp is meant to be armed, and switching it
    off is what destroys it.

    Arming also widens the device's own timer to `_LAMP_RAIL`. Left alone it is
    a second opinion about darkness, checked after ours and quietly winning.

    Callers say on/off; which vendor field carries that is this module's
    problem and must not leak to them.
    """
    led = await get_light(creds)
    was = led.get("mode")
    mode = (armed_mode or was or _MODE_ARMED) if on else _MODE_OFF
    body = {**led, "channel": 0, "mode": int(mode)}
    if on:
        body["LightingSchedule"] = dict(_LAMP_RAIL)
    if bright is not None:
        body["bright"] = max(0, min(100, int(bright)))
    await _call(
        creds,
        "SetWhiteLed",
        [{"cmd": "SetWhiteLed", "action": 0, "param": {"WhiteLed": body}}],
    )
    return body, (int(was) if was else None)


async def armed_mode_of(pool: Any, camera_id: Any) -> int | None:
    """The mode this camera's lamp was last seen armed in, if we ever saw it."""
    return await pool.fetchval(
        "SELECT light_armed_mode FROM cameras WHERE id = $1", camera_id
    )


async def note_armed_mode(pool: Any, camera_id: Any, mode: int | None) -> None:
    """Remember an armed mode read off the device. Off is never remembered —
    it is the value that carries no information about how the lamp is meant to
    work, and it is written by us."""
    if not mode:
        return
    await pool.execute(
        "UPDATE cameras SET light_armed_mode = $2 WHERE id = $1 "
        "AND light_armed_mode IS DISTINCT FROM $2",
        camera_id, int(mode),
    )


async def _journal(
    pool: Any, camera_id: Any, source: str, reason: str, *, before: dict, after: dict
) -> None:
    """Write a temporary profile hold into the settings journal.

    It went unrecorded, and the cost of that was a question nobody could
    answer. Ana's car crossed west's plate zone on 02.09 with the zone 48 to
    77 per cent saturated for twenty-one seconds and no plate was read; whether
    the swap to the plate profile fired at all, and when, could only be guessed
    at afterwards, because the journal held `profile_switch` and `manual` and
    nothing else. The override table cannot answer it either — it is deleted on
    the way back out.

    NOTHING here may change the outcome of the profile change. The device has
    already taken it and the override row is already written by the time this
    runs, so a throw out of here would report a hold that physically happened as
    not applied — and the headlight watcher, reading that, would never set its
    hold or its cooldown, would fold its own overridden frames back into the
    baseline, and would leave the camera in the plate profile until the sweeper
    expired it. A missing journal row is a lost record; a raised journal row was
    a broken camera. So this catches everything and says so.
    """
    try:
        async with pool.acquire() as conn:
            band = await conn.fetchval(
                "SELECT light_condition FROM cameras WHERE id = $1", camera_id
            )
            await record_change(
                conn, camera_id, source=source,
                light_condition=band or "unknown", before=before, after=after,
            )
    except Exception:
        log.exception(
            "camera %s: %s for %r not journalled", camera_id, source, reason
        )


async def apply_profile(
    pool: Any, camera_id: Any, creds: CameraCreds, changes: dict,
    *, reason: str, seconds: float,
) -> bool:
    """Hold a camera in a temporary profile, with the way back written down.

    Returns False when the camera is already held — the first override owns it
    until it expires. Two callers fighting over one device is how a baseline
    becomes whatever the loser happened to read.
    """
    held = await pool.fetchval(
        "SELECT reason FROM camera_profile_override WHERE camera_id = $1", camera_id
    )
    if held is not None:
        log.info("camera %s already held for %r — leaving it", camera_id, held)
        return False
    baseline = await write_settings(creds, changes)
    # Written AFTER the device took the change: a row with no override behind it
    # would restore settings the camera never left, and on the next pass would
    # look like a crash that never happened.
    await pool.execute(
        """
        INSERT INTO camera_profile_override (camera_id, baseline, reason, expires_at)
        VALUES ($1, $2::jsonb, $3, now() + make_interval(secs => $4))
        ON CONFLICT (camera_id) DO NOTHING
        """,
        camera_id, json.dumps(baseline), reason,
        min(float(seconds), _MAX_OVERRIDE.total_seconds()),
    )
    await _journal(pool, camera_id, "hold", reason, before=baseline, after=changes)
    log.info("camera %s held for %r, %.0f s", camera_id, reason, seconds)
    return True


async def restore(pool: Any, camera_id: Any, creds: CameraCreds) -> bool:
    """Put a held camera back the way its owner left it."""
    row = await pool.fetchrow(
        "SELECT baseline, reason FROM camera_profile_override WHERE camera_id = $1",
        camera_id,
    )
    if row is None:
        return False
    baseline = row["baseline"]
    baseline = json.loads(baseline) if isinstance(baseline, str) else baseline
    await _call(
        creds, "SetIsp", [{"cmd": "SetIsp", "action": 0, "param": {"Isp": baseline}}]
    )
    # Only after the device took it back. A row deleted first, on a call that
    # then failed, is a camera nobody knows is still overridden.
    await pool.execute(
        "DELETE FROM camera_profile_override WHERE camera_id = $1", camera_id
    )
    # The other end of the pair. Without it the journal shows a camera going
    # into a profile and never coming out, and how long it was held is the
    # thing you want when a plate went unread. `before` is empty by necessity:
    # the override row persists only the baseline, so by the time a camera
    # comes back nothing on record says what it was wearing.
    await _journal(pool, camera_id, "release", row["reason"],
                   before={}, after=baseline)
    log.info("camera %s restored after %r", camera_id, row["reason"])
    return True


async def sweep_overrides(pool: Any, *, expired_only: bool = True) -> int:
    """Put back every camera that is past its deadline.

    `expired_only=False` on startup: a row that survived a restart means the
    process that promised to undo the override is gone, so the deadline is
    already meaningless and the camera is put back now.
    """
    where = "WHERE expires_at <= now()" if expired_only else ""
    rows = await pool.fetch(
        f"""
        SELECT o.camera_id, c.slug, c.stream_url, o.reason
        FROM camera_profile_override o JOIN cameras c ON c.id = o.camera_id
        {where}
        """  # noqa: S608
    )
    done = 0
    for r in rows:
        creds = creds_for(r["stream_url"])
        if creds is None:
            log.warning("camera %s is held but has no reachable address — "
                        "cannot restore", r["slug"])
            continue
        try:
            if await restore(pool, r["camera_id"], creds):
                done += 1
                log.warning("camera %s put back after %r ran out", r["slug"], r["reason"])
        except Exception:
            # Loud, and left in the table: the next sweep tries again rather
            # than the camera staying overridden with nothing recording that.
            log.exception("camera %s could not be restored — will retry", r["slug"])
    return done


# The profile a camera wears while a headlight is drowning the plate zone.
# Measured on West, 29.08, against a night image whose mean brightness in colour
# was 15/255 — the camera is effectively blind — and 83 in this one. It is also
# what makes the registration reachable at all: `Color` keeps the IR-cut filter
# in, so the illuminator the plate would retro-reflect never gets to the sensor.
#
# Not a permanent setting. `Black&White` would leave the camera monochrome by
# day, and the operator wants the colour night view he has. It is worn for the
# seconds the bloom lasts and taken off again.
PLATE_PROFILE = {
    "dayNight": "Black&White",
    "exposure": "Anti-Smearing",
    "gain": {"max": 25, "min": 1},
    "backLight": "DynamicRangeControl",
}

PROFILE_SUBJECT = "baba.camera.profile"


async def serve_profile_requests(nc: Any, pool: Any) -> Any:
    """Answer in-house requests to hold a camera in a temporary profile.

    Over the bus rather than by importing this module elsewhere, because the
    point is that ONE process writes to the device. A second importer would be
    a second writer with its own idea of the baseline, and the override table
    could only tell them apart after the damage.
    """

    async def _on_request(msg) -> None:
        reply: dict = {"applied": False, "error": None}
        try:
            req = json.loads(msg.data)
            camera_id = req["camera_id"]
            row = await pool.fetchrow(
                "SELECT slug, stream_url FROM cameras WHERE id = $1::uuid AND enabled",
                camera_id,
            )
            if row is None:
                reply["error"] = "unknown_camera"
            else:
                creds = creds_for(row["stream_url"])
                if creds is None:
                    reply["error"] = "no_address"
                elif req.get("release"):
                    reply["applied"] = await restore(pool, camera_id, creds)
                else:
                    reply["applied"] = await apply_profile(
                        pool, camera_id, creds, PLATE_PROFILE,
                        reason=str(req.get("reason") or "plate"),
                        seconds=float(req.get("seconds") or 30),
                    )
        except Exception as e:
            log.exception("camera profile request failed")
            reply["error"] = f"internal: {e}"
        if msg.reply:
            await msg.respond(json.dumps(reply).encode())

    return await nc.subscribe(PROFILE_SUBJECT, cb=_on_request)
