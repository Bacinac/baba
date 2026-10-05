from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from baba_core.paths import MediaLayout
from baba_core.url import mask_credentials
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from baba_api.audit import diff_fields, write_audit
from baba_api.auth import AuthUser, current_user, role_at_least
from baba_api.models import (
    Camera,
    CameraIn,
    CameraPatch,
    CameraSlugRename,
    LiveOverlay,
    TestConnectionIn,
    TestConnectionOut,
    TrackingDefaults,
    adaptive_rate_error,
    analysis_stream_error,
)
from baba_api.profile_capture import journal_maintain_change, journal_stillness_change
from baba_api.stream_url import FFMPEG_PROTOCOL_WHITELIST, validate_stream_url

log = logging.getLogger(__name__)

router = APIRouter()


_CAMERA_FIELDS = (
    "id, slug, name, stream_url, substream_url, analysis_stream, enabled, target_fps, idle_fps, "
    "stillness_ratio, park_seconds, lost_seconds, reid_lost_seconds, maintain_conf, "
    "light_condition, downscale_max_edge, recording_enabled, "
    "color, created_at, updated_at"
)

# Distinct colours used to tint cameras across the UI (storage bar, legends).
# Same 12-colour set as migration 031; a new camera takes the first one not
# already in use so every camera is visually distinct.
_CAMERA_PALETTE = (
    "#f59e0b",
    "#ef4444",
    "#ec4899",
    "#a855f7",
    "#6366f1",
    "#3b82f6",
    "#06b6d4",
    "#14b8a6",
    "#10b981",
    "#84cc16",
    "#eab308",
    "#f97316",
)


def _pool(request: Request):
    return request.app.state.pool


async def _next_camera_color(pool) -> str:
    """First palette colour not currently assigned to a camera; once all are
    used, cycle by camera count so collisions are at least evenly spread."""
    used = {r["color"] for r in await pool.fetch("SELECT DISTINCT color FROM cameras")}
    for c in _CAMERA_PALETTE:
        if c not in used:
            return c
    n = await pool.fetchval("SELECT count(*) FROM cameras")
    return _CAMERA_PALETTE[int(n or 0) % len(_CAMERA_PALETTE)]


def _camera_seen_by(row, user: AuthUser) -> Camera:
    """The camera as `user` may read it. Only an admin edits a stream URL, so only
    an admin reads the credentials in it; everyone else, a peer included, still
    sees the host and the path."""
    cam = Camera(**dict(row))
    if role_at_least(user.role, "admin"):
        return cam
    return cam.model_copy(update={
        "stream_url": mask_credentials(cam.stream_url),
        "substream_url": cam.substream_url and mask_credentials(cam.substream_url),
    })


@router.get("/cameras", response_model=list[Camera])
async def list_cameras(request: Request, user: AuthUser = Depends(current_user)) -> list[Camera]:
    rows = await _pool(request).fetch(
        f"SELECT {_CAMERA_FIELDS} FROM cameras ORDER BY created_at ASC"  # noqa: S608
    )
    return [_camera_seen_by(r, user) for r in rows]


@router.get("/cameras/stream-access/{stream}")
async def camera_stream_access(
    stream: str, request: Request, user: AuthUser = Depends(current_user)
) -> dict[str, str]:
    registered = await _pool(request).fetchval(
        "SELECT EXISTS (SELECT 1 FROM cameras WHERE enabled AND "
        "(slug = $1 OR (slug || '_sub' = $1 AND substream_url IS NOT NULL)))",
        stream,
    )
    if not registered:
        raise HTTPException(404, "camera stream not found")
    return {"stream": stream, "role": user.role}


@router.post("/cameras", response_model=Camera, status_code=201)
async def create_camera(
    payload: CameraIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> Camera:
    # Reject non-stream URL schemes before persisting (the ingestor hands these
    # straight to ffmpeg). Same guard as the probe/quickadd paths.
    validate_stream_url(payload.stream_url)
    if payload.substream_url:
        validate_stream_url(payload.substream_url)
    # Auto-assign a distinct colour when the client didn't pick one.
    color = payload.color or await _next_camera_color(_pool(request))
    try:
        row = await _pool(request).fetchrow(
            f"""
            INSERT INTO cameras (slug, name, stream_url, substream_url, analysis_stream,
                                 enabled, target_fps,
                                 idle_fps, stillness_ratio, park_seconds,
                                 lost_seconds, reid_lost_seconds,
                                 downscale_max_edge,
                                 recording_enabled, color, maintain_conf)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                    COALESCE($16, 0.30))
            RETURNING {_CAMERA_FIELDS}
            """,  # noqa: S608
            payload.slug,
            payload.name,
            payload.stream_url,
            payload.substream_url,
            payload.analysis_stream,
            payload.enabled,
            payload.target_fps,
            payload.idle_fps,
            payload.stillness_ratio,
            payload.park_seconds,
            payload.lost_seconds,
            payload.reid_lost_seconds,
            payload.downscale_max_edge,
            payload.recording_enabled,
            color,
            # Advertised in `CameraIn`, validated to [0, 1] — and then never
            # written, so a camera created with an explicit maintain floor
            # silently got the column default instead. The default is repeated
            # here rather than left to the column so the COALESCE reads as the
            # decision it is.
            payload.maintain_conf,
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(
                status_code=409, detail=f"slug {payload.slug!r} already exists"
            ) from e
        raise
    await write_audit(
        _pool(request),
        user=user,
        resource_type="camera",
        op="create",
        resource_id=row["id"],
        payload={"slug": payload.slug, "name": payload.name},
    )
    return Camera(**dict(row))


@router.get("/cameras/{camera_id}", response_model=Camera)
async def get_camera(
    camera_id: UUID, request: Request, user: AuthUser = Depends(current_user)
) -> Camera:
    row = await _pool(request).fetchrow(
        f"SELECT {_CAMERA_FIELDS} FROM cameras WHERE id = $1",  # noqa: S608
        camera_id,
    )
    if row is None:
        raise HTTPException(404, "camera not found")
    return _camera_seen_by(row, user)


_CAMERA_NOT_NULL = frozenset({
    "analysis_stream", "color", "downscale_max_edge", "enabled", "maintain_conf", "name",
    "recording_enabled", "stream_url", "target_fps",
})


# Columns that cannot hold NULL, so a patch may not send one. `model_dump(
# exclude_unset=True)` keeps a key that arrived as JSON `null` — every patch
# field is `X | None` and pydantic applies its `ge`/`le`/`min_length` only to
# the non-None branch — and the update loop then emits `SET <col> = NULL`.
#
# The frontend reaches this on an ordinary edit: Svelte's number binding turns
# an emptied `<input type="number">` into `null`, and `JSON.stringify` keeps
# it. Clearing a box to retype it produced either a NotNullViolation or, where
# the value is read before the UPDATE, a TypeError comparing an int against
# None — a bare 500 shown to the operator as an English string, for a typo.
def reject_nulls(fields: dict[str, Any], not_null: frozenset[str]) -> None:
    empty = sorted(k for k in fields if k in not_null and fields[k] is None)
    if empty:
        raise HTTPException(
            422, f"these fields cannot be cleared: {', '.join(empty)}"
        )


# Fields whose change is checked together with a partner field: the check
# runs on the EFFECTIVE pair (sent or current).
_PAIR_GUARDED = frozenset({"idle_fps", "target_fps", "analysis_stream", "substream_url"})


def _reject_pair(fields: dict, current, a: str, b: str, check) -> None:
    if a not in fields and b not in fields:
        return
    err = check(fields.get(a, current[a]), fields.get(b, current[b]))
    if err:
        raise HTTPException(422, err)


def _mask_url_value(v: Any) -> Any:
    if isinstance(v, str) and ("://" in v):
        return mask_credentials(v)
    return v


@router.patch("/cameras/{camera_id}", response_model=Camera)
async def patch_camera(
    camera_id: UUID,
    payload: CameraPatch,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> Camera:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    reject_nulls(fields, _CAMERA_NOT_NULL)
    # Validate any URL change before it's persisted + sent to the ingestor.
    for _url_field in ("stream_url", "substream_url"):
        if fields.get(_url_field):
            validate_stream_url(fields[_url_field])
    pool = _pool(request)
    if _PAIR_GUARDED & fields.keys():
        current = await pool.fetchrow(
            "SELECT target_fps, idle_fps, analysis_stream, substream_url FROM cameras WHERE id = $1",
            camera_id,
        )
        if current is None:
            raise HTTPException(404, "camera not found")
        # Mirrors the DB CHECK (cameras_idle_fps_range) but surfaces as a 422
        # instead of a 500.
        _reject_pair(fields, current, "idle_fps", "target_fps", adaptive_rate_error)
        _reject_pair(fields, current, "analysis_stream", "substream_url", analysis_stream_error)
    # Fetch the touched fields before the update so the audit diff is
    # accurate. One extra query — cheap and worth it for the timeline.
    field_list = ", ".join(fields.keys())
    before_row = await pool.fetchrow(
        f"SELECT {field_list} FROM cameras WHERE id = $1",  # noqa: S608
        camera_id,
    )
    if before_row is None:
        raise HTTPException(404, "camera not found")
    set_clauses = [f"{k} = ${i}" for i, k in enumerate(fields, start=1)]
    args: list = [*fields.values(), camera_id]
    row = await pool.fetchrow(
        f"""
        UPDATE cameras SET {", ".join(set_clauses)}
        WHERE id = ${len(args)}
        RETURNING {_CAMERA_FIELDS}
        """,  # noqa: S608
        *args,
    )
    if row is None:
        raise HTTPException(404, "camera not found")
    # Mask credentials in audit payload so RTSP passwords don't land in
    # the log.
    before_d = {k: _mask_url_value(before_row[k]) for k in fields}
    after_d = {k: _mask_url_value(fields[k]) for k in fields}
    await write_audit(
        pool,
        user=user,
        resource_type="camera",
        op="update",
        resource_id=camera_id,
        payload=diff_fields(before_d, after_d),
    )
    # Lighting-sensitive tuning gets stamped into the camera's profile for
    # its current illumination band + the settings change journal.
    if "stillness_ratio" in fields and before_row["stillness_ratio"] is not None:
        await journal_stillness_change(pool, camera_id, float(before_row["stillness_ratio"]))
    if "maintain_conf" in fields:
        await journal_maintain_change(pool, camera_id, float(before_row["maintain_conf"]))
    return Camera(**dict(row))


# --- slug rename ---
#
# The slug is woven into the system: media segment directory names,
# the recordings.path strings, go2rtc src ids, NATS subjects, and
# the POSIX shm ring name. Renaming it cannot just be a PATCH on the
# row; we need to update referencing strings and rename the media
# folder in lockstep, then let the supervisors' LISTEN/NOTIFY tear
# down and rebuild their per-camera workers under the new slug.
#
# Ordering: rename filesystem dir first (fail fast, fully reversible
# in-process), then update DB inside a single transaction so the
# cameras row and the recordings.path strings change atomically.
# If the DB step fails, roll the filesystem rename back so the system
# stays consistent.


@router.post("/cameras/{camera_id}/rename-slug", response_model=Camera)
async def rename_camera_slug(
    camera_id: UUID,
    payload: CameraSlugRename,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> Camera:
    pool = _pool(request)
    new_slug = payload.slug
    pre = await pool.fetchrow(
        "SELECT slug, name FROM cameras WHERE id = $1",
        camera_id,
    )
    if pre is None:
        raise HTTPException(404, "camera not found")
    old_slug = pre["slug"]
    if old_slug == new_slug:
        # No-op — return the current row unchanged.
        row = await pool.fetchrow(
            f"SELECT {_CAMERA_FIELDS} FROM cameras WHERE id = $1",  # noqa: S608
            camera_id,
        )
        return Camera(**dict(row))
    clash = await pool.fetchval(
        "SELECT 1 FROM cameras WHERE slug = $1 AND id <> $2",
        new_slug,
        camera_id,
    )
    if clash:
        raise HTTPException(409, f"slug {new_slug!r} already in use")

    layout = MediaLayout(request.app.state.config.media_path)
    old_dir = layout.camera_segments(old_slug)
    new_dir = layout.camera_segments(new_slug)
    renamed_fs = False
    if old_dir.exists():
        if new_dir.exists():
            raise HTTPException(
                409,
                f"target media directory {new_dir} already exists; "
                "resolve manually before retrying",
            )
        try:
            old_dir.rename(new_dir)
            renamed_fs = True
        except OSError as e:
            raise HTTPException(500, f"could not rename media directory: {e}") from e

    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE cameras SET slug = $1 WHERE id = $2",
                new_slug,
                camera_id,
            )
            # recordings.path is of the form `segments/<slug>/<file>`.
            # Rewrite only the path prefix so any future change in
            # naming convention won't accidentally clobber other
            # path segments that happen to contain the slug.
            await conn.execute(
                "UPDATE recordings SET path = $1 || substr(path, $2)"
                " WHERE camera_id = $3 AND path LIKE $4",
                MediaLayout.rel_camera_segments(new_slug),
                len(MediaLayout.rel_camera_segments(old_slug)) + 1,
                camera_id,
                MediaLayout.rel_camera_segments(old_slug) + "%",
            )
            row = await conn.fetchrow(
                f"SELECT {_CAMERA_FIELDS} FROM cameras WHERE id = $1",  # noqa: S608
                camera_id,
            )
    except Exception:
        if renamed_fs:
            try:
                new_dir.rename(old_dir)
            except OSError as revert_err:
                log.error(
                    "slug rename: DB update failed and filesystem revert "
                    "from %s back to %s also failed: %s",
                    new_dir,
                    old_dir,
                    revert_err,
                )
        raise

    await write_audit(
        pool,
        user=user,
        resource_type="camera",
        op="rename_slug",
        resource_id=camera_id,
        payload={"old": old_slug, "new": new_slug, "name": pre["name"]},
    )
    return Camera(**dict(row))


@router.delete("/cameras/{camera_id}", status_code=204)
async def delete_camera(
    camera_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    pool = _pool(request)
    # Capture identifying info before the delete so the audit row has
    # something more useful than just the UUID.
    pre = await pool.fetchrow(
        "SELECT slug, name FROM cameras WHERE id = $1",
        camera_id,
    )
    if pre is None:
        raise HTTPException(404, "camera not found")
    # Gather this camera's per-track media paths BEFORE the delete — the
    # `tracks` rows cascade away with the camera, so we can't recover them
    # afterwards. The segment tree is removed wholesale by slug (below).
    track_media = await pool.fetch(
        "SELECT crop_path, thumbnail_path, face_crop_path FROM tracks "
        "WHERE camera_id = $1",
        camera_id,
    )
    result = await pool.execute("DELETE FROM cameras WHERE id = $1", camera_id)
    if result.endswith(" 0"):
        raise HTTPException(404, "camera not found")
    await write_audit(
        pool,
        user=user,
        resource_type="camera",
        op="delete",
        resource_id=camera_id,
        payload={"slug": pre["slug"], "name": pre["name"]},
    )
    # Post-commit media cleanup. The DB cascade drops rows only; without this
    # the camera's entire segments/<slug>/ tree (up to retention_days of video,
    # potentially hundreds of GB) plus every track crop/thumbnail is orphaned on
    # disk forever — invisible to all retention sweeps, so disk-prune starts
    # eating live cameras' history to reclaim space. Run off the event loop
    # (HDD tier, large rmtree). Files-after-rows so a crash can't dangle a row
    # pointing at a deleted file.
    media_root: Path = request.app.state.config.media_path
    slug = pre["slug"]
    rel_files = [
        r[col]
        for r in track_media
        for col in ("crop_path", "thumbnail_path", "face_crop_path")
        if r[col]
    ]

    def _cleanup() -> None:
        seg_dir = MediaLayout(media_root).camera_segments(slug)
        try:
            shutil.rmtree(seg_dir, ignore_errors=True)
        except Exception:
            log.exception("failed to remove segment dir %s", seg_dir)
        for rel in rel_files:
            try:
                (media_root / rel).unlink(missing_ok=True)
            except Exception:
                log.exception("failed to unlink %s", rel)

    await asyncio.to_thread(_cleanup)
    return None


# --- global tracking defaults (the base layer under per-camera overrides) ---

_TRACKING_DEFAULTS_KEY = "tracking_defaults"
# Mirrors the migration 053 seed — served when the row is missing (pre-053
# DB) so the UI always has a complete object to render.
_TRACKING_FALLBACK: dict[str, Any] = {
    "stillness_ratio": 0.08,
    "park_seconds": 60,
    "lost_seconds": 30,
    "reid_lost_seconds": 120,
}


@router.get("/settings/tracking", response_model=TrackingDefaults)
async def get_tracking_defaults(request: Request) -> TrackingDefaults:
    raw = await _pool(request).fetchval(
        "SELECT value FROM app_settings WHERE key = $1", _TRACKING_DEFAULTS_KEY
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    return TrackingDefaults(**{**_TRACKING_FALLBACK, **(raw or {})})


@router.put("/settings/tracking", response_model=TrackingDefaults)
async def put_tracking_defaults(
    payload: TrackingDefaults,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> TrackingDefaults:
    pool = _pool(request)
    before_raw = await pool.fetchval(
        "SELECT value FROM app_settings WHERE key = $1", _TRACKING_DEFAULTS_KEY
    )
    if isinstance(before_raw, str):
        before_raw = json.loads(before_raw)
    # MERGE, never replace. This row is shared: the four motion values are this
    # card's, and `PUT /tunables` keeps the anchor, identity and phantom knobs in
    # the same key. `TrackingDefaults` has four fields and Pydantic drops the
    # rest on the way in, so writing model_dump() wholesale deleted every knob
    # the other card owns — one drag of the Parked slider and the tracker lost
    # its identity margin and both phantom thresholds, silently, with a NOTIFY
    # telling it to go and re-read the row it had just been emptied of.
    after = {**(before_raw or {}), **payload.model_dump()}
    await pool.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        _TRACKING_DEFAULTS_KEY,
        json.dumps(after),
    )
    # The tracker's settings listener subscribes to this channel; cameras
    # inherit the new base immediately (no restart).
    await pool.execute("SELECT pg_notify('app_settings_changed', $1)", _TRACKING_DEFAULTS_KEY)
    await write_audit(
        pool,
        user=user,
        resource_type="settings",
        op="update",
        resource_id=None,
        payload=diff_fields(
            {**_TRACKING_FALLBACK, **(before_raw or {})},
            after,
        ),
    )
    return payload


# --- live overlay (global): boxes + badges on the live views ---

_LIVE_OVERLAY_KEY = "live_overlay"
_LIVE_OVERLAY_FALLBACK: dict[str, Any] = {"boxes": True, "badges": True}


@router.get("/settings/live-overlay", response_model=LiveOverlay)
async def get_live_overlay(request: Request) -> LiveOverlay:
    raw = await _pool(request).fetchval(
        "SELECT value FROM app_settings WHERE key = $1", _LIVE_OVERLAY_KEY
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    return LiveOverlay(**{**_LIVE_OVERLAY_FALLBACK, **(raw or {})})


@router.put("/settings/live-overlay", response_model=LiveOverlay)
async def put_live_overlay(
    payload: LiveOverlay,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> LiveOverlay:
    await _pool(request).execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        _LIVE_OVERLAY_KEY,
        json.dumps(payload.model_dump()),
    )
    await write_audit(
        _pool(request),
        user=user,
        resource_type="settings",
        op="update",
        resource_id=None,
        payload={"live_overlay": payload.model_dump()},
    )
    return payload


# --- snapshot ---
#
# Browser-facing JPEG snapshot for a camera, proxied from go2rtc. We don't
# expose go2rtc directly because (a) it's network_mode=host so the URL is
# environment-dependent and (b) we want the same auth cookie to gate it.


@router.get("/cameras/{camera_id}/snapshot")
async def camera_snapshot(camera_id: UUID, request: Request) -> Response:
    row = await _pool(request).fetchrow("SELECT slug FROM cameras WHERE id = $1", camera_id)
    if row is None:
        raise HTTPException(404, "camera not found")
    go2rtc_url = request.app.state.config.go2rtc_url
    go2rtc_auth = request.app.state.config.go2rtc_auth
    url = f"{go2rtc_url.rstrip('/')}/api/frame.jpeg"

    # go2rtc's /api/frame.jpeg waits for the next I-frame from the live
    # producer and returns 500 when that wait times out internally. High-
    # bitrate H.264 streams with long GOPs (e.g. Hikvision Main profile
    # Level 5.1 used by the patio camera) miss the window often enough
    # that the UI shows "Couldn't fetch snapshot" intermittently. Retry
    # twice with a short backoff — total worst-case ~3.5 s, well within
    # the caller's expectations and cheap when keyframes arrive on time.
    last_status: int | None = None
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=10, auth=go2rtc_auth) as client:
        for attempt in range(3):
            try:
                r = await client.get(url, params={"src": row["slug"]})
            except httpx.HTTPError as e:
                last_exc = e
                last_status = None
            else:
                if r.status_code == 200 and r.content:
                    return Response(
                        content=r.content,
                        media_type="image/jpeg",
                        headers={"cache-control": "no-store"},
                    )
                last_status = r.status_code
                last_exc = None
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        raise HTTPException(502, f"snapshot fetch failed: {last_exc}") from last_exc
    raise HTTPException(502, f"snapshot fetch failed: status {last_status}")


# --- test connection ---
#
# ffprobe is the fastest way to validate a stream URL — protocol-agnostic
# (RTSP / HTTP-FLV / RTMP / file all work), returns codec/dimensions/fps
# from the first I-frame, exits in <2s on success or <10s timeout on
# unreachable host. We run it as a subprocess and parse the JSON output.

_FFPROBE_TIMEOUT_S = 10


@router.post("/cameras/test-connection", response_model=TestConnectionOut)
async def test_connection(payload: TestConnectionIn) -> TestConnectionOut:
    validate_stream_url(payload.stream_url)
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        FFMPEG_PROTOCOL_WHITELIST,
        "-rtsp_transport",
        "tcp",  # most cameras default to TCP; harmless for non-RTSP
        "-timeout",
        str(_FFPROBE_TIMEOUT_S * 1_000_000),  # microseconds
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        payload.stream_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_S + 2)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return TestConnectionOut(
            ok=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            error="probe timed out",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace").strip()
        # Trim noise — ffprobe prefixes lines like "[rtsp @ 0x...] ".
        first_line = err.split("\n", 1)[0] if err else "probe failed"
        return TestConnectionOut(ok=False, duration_ms=duration_ms, error=first_line[:400])

    try:
        data = json.loads(stdout)
    except ValueError:
        return TestConnectionOut(
            ok=False, duration_ms=duration_ms, error="could not parse ffprobe output"
        )

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        return TestConnectionOut(ok=False, duration_ms=duration_ms, error="no video stream found")

    # avg_frame_rate is "N/D" — evaluate. Fall back to r_frame_rate.
    def _eval_rate(s: str | None) -> float | None:
        if not s or s == "0/0":
            return None
        try:
            num, den = s.split("/")
            n, d = float(num), float(den)
        except (ValueError, AttributeError):
            return None
        return n / d if d else None

    fps = _eval_rate(video.get("avg_frame_rate")) or _eval_rate(video.get("r_frame_rate"))
    return TestConnectionOut(
        ok=True,
        codec=video.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        fps=fps,
        duration_ms=duration_ms,
    )
