"""Live-grid stills: the detector's own downscaled SHM frames, JPEG-encoded
with detection boxes baked in, served one per request.

Why this exists
---------------
The live grid used to pull one full-resolution go2rtc video per tile over
MSE. Seven ~4K H.264 streams is seven hardware VIDEO-decode contexts, which a
browser exhausts (tiles go black, GPU decode stalls — operator hit this
2026-07-14). The ingestor ALREADY downscales every camera to an NV12 SHM ring
for the detector; serving those frames as JPEG:

  * uses the browser's IMAGE decoder, never a video-decode context, so any
    number of tiles is cheap and the exhaustion class of bug can't happen;
  * costs nothing extra to produce (we decoded+downscaled once for the AI);
  * works for every camera regardless of substream config; and
  * carries the detector's boxes in the SAME coordinate space as the frame,
    so the overlay aligns pixel-perfectly.

The per-camera detail view (`/live/<slug>`) keeps the full MSE video for smooth
playback; this is only the multi-camera grid overview.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from uuid import UUID

import cv2
import msgspec
from baba_core import cap_long_edge
from baba_core.frame_ring import PIXEL_FORMAT_NV12, FrameRingReader
from baba_core.parked import parked_assignments
from baba_core.wire import TracksMessage
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from baba_api.auth import AuthUser, current_user

log = logging.getLogger("baba.api.live")

live_router = APIRouter()

_TRACKS_SUBJECT = "baba.tracks.*"
# Overlay boxes older than this are dropped: the tracker publishes even on
# empty frames, so silence means the camera stopped — better a clean frame
# than stale boxes frozen over moved-on objects.
_BOX_TTL_S = 2.0
_JPEG_QUALITY = 70

# Long-edge cap applied before JPEG encoding. The ring resolution is chosen for
# the DETECTOR (and, on face cameras, for the face stack — those run native, up
# to 3040 px on patio), not for a grid tile a few hundred pixels wide. Encoding
# a 4 MP frame costs ~30 ms and ~300 KB per tile per request — real CPU and
# bandwidth spent on pixels the browser immediately throws away.
# 960 matches what the tiles were already receiving before the face-resolution
# work, so this caps the cost rather than changing what the operator sees.
# `_draw_boxes` derives its scale from the actual array size vs the wire's
# frame_width, so boxes follow the downscale with no extra maths here.
_GRID_MAX_EDGE = 960

# class_id -> BGR, mirroring the web detectionColors PALETTE (person amber,
# car violet, truck yellow, …) so a class reads as the same hue as the
# detail-view overlay. cv2 is BGR; the web hexes are RGB, hence the swaps.
_PALETTE_BGR: tuple[tuple[int, int, int], ...] = (
    (0x00, 0xB3, 0xFF),  # 0 person   amber
    (0xFF, 0xE5, 0x00),  # 1 bicycle  cyan
    (0xFF, 0x5A, 0xBF),  # 2 car      violet
    (0x88, 0xFF, 0x00),  # 3 motorcycle green
    (0x9B, 0x2D, 0xFF),  # 4          pink
    (0x00, 0x7A, 0xFF),  # 5 bus      orange
    (0xFF, 0x8B, 0x3D),  # 6 (dog)    blue
    (0x00, 0xEE, 0xFF),  # 7 truck    yellow
    (0x55, 0x33, 0xFF),  # 8          red
    (0x00, 0xFF, 0xB4),  # 9 (cat)    lime
)
# Motion overrides — a stationary/parked object is drawn in its own hue so it
# reads as "the detector still sees it, but it's not moving".
_MOTION_BGR = {"stationary": (0x00, 0x91, 0xFF), "parked": (0xFF, 0xE5, 0x00)}


def _class_color(class_id: int) -> tuple[int, int, int]:
    return _PALETTE_BGR[class_id % len(_PALETTE_BGR)]


class TrackCache:
    """Latest TracksMessage per camera slug, fed by a single `baba.tracks.*`
    subscription. Read by the still-frame endpoint to paint boxes. One instance
    lives on app.state for the api process lifetime."""

    def __init__(self) -> None:
        self._by_slug: dict[str, tuple[float, TracksMessage]] = {}
        # Recent message-arrival monotonic timestamps per camera — the tracker
        # publishes one per processed frame (even 0-detection ones), so their
        # rate IS the camera's real effective (adaptive) fps.
        self._arrivals: dict[str, deque[float]] = {}
        self._decoder = msgspec.msgpack.Decoder(TracksMessage)
        self._sub = None

    async def start(self, nc) -> None:
        self._sub = await nc.subscribe(_TRACKS_SUBJECT, cb=self._on_message)
        log.info("live track cache subscribed to %s", _TRACKS_SUBJECT)

    async def _on_message(self, msg) -> None:
        try:
            tm = self._decoder.decode(msg.data)
        except msgspec.DecodeError:
            return
        now = time.monotonic()
        self._by_slug[tm.camera_id] = (now, tm)
        arr = self._arrivals.get(tm.camera_id)
        if arr is None:
            arr = deque(maxlen=30)
            self._arrivals[tm.camera_id] = arr
        arr.append(now)

    def latest(self, slug: str) -> TracksMessage | None:
        entry = self._by_slug.get(slug)
        if entry is None:
            return None
        ts, tm = entry
        if time.monotonic() - ts > _BOX_TTL_S:
            return None
        return tm

    def _fps(self, slug: str, now: float) -> float:
        """Measured effective fps: message arrivals within the last window.
        Returns 0.0 when the camera has gone quiet (no arrival for >1.5 s), so
        a stopped tile reads 0 rather than a stale rate."""
        arr = self._arrivals.get(slug)
        if not arr or now - arr[-1] > 1.5:
            return 0.0
        recent = [t for t in arr if now - t <= 5.0]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return round((len(recent) - 1) / span, 1) if span > 0 else 0.0

    def status(self) -> dict[str, dict]:
        """Per-camera live status as `{slug: {fps, detections}}`. `fps` is the
        measured effective rate (feeds the tile label); `detections` is the
        current per-class counts with a `moving` flag (feeds the badges) —
        empty list when nothing is detected but the camera is still running."""
        now = time.monotonic()
        out: dict[str, dict] = {}
        for slug, (ts, tm) in self._by_slug.items():
            fps = self._fps(slug, now)
            fresh = now - ts <= _BOX_TTL_S
            if fps == 0.0 and not fresh:
                continue  # camera stopped entirely — drop from the grid status
            detections: list[dict] = []
            if fresh and tm.tracks:
                by_class: dict[int, dict] = {}
                for t in tm.tracks:
                    c = by_class.setdefault(
                        t.class_id,
                        {
                            "class_name": t.class_name,
                            "class_id": t.class_id,
                            "count": 0,
                            "moving": False,
                        },
                    )
                    c["count"] += 1
                    if (t.motion_state or "active").lower() == "active":
                        c["moving"] = True
                detections = sorted(by_class.values(), key=lambda d: -d["count"])
            out[slug] = {"fps": fps, "detections": detections}
        return out

    async def stop(self) -> None:
        if self._sub is not None:
            with contextlib.suppress(Exception):
                await self._sub.unsubscribe()
            self._sub = None


def _draw_boxes(bgr, tm: TracksMessage | None) -> None:
    if tm is None or not tm.tracks:
        return
    fh, fw = bgr.shape[0], bgr.shape[1]
    # Track coords are in the detector's frame space (the SHM resolution). The
    # SHM frame we drew from IS that resolution, so this scale is 1.0 in the
    # normal case; keep it for the rare in-flight resolution change.
    sx = fw / tm.frame_width if tm.frame_width else 1.0
    sy = fh / tm.frame_height if tm.frame_height else 1.0
    for t in tm.tracks:
        x1, y1 = int(t.x1 * sx), int(t.y1 * sy)
        x2, y2 = int(t.x2 * sx), int(t.y2 * sy)
        motion = (t.motion_state or "active").lower()
        color = _MOTION_BGR.get(motion) or _class_color(t.class_id)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            bgr,
            t.class_name,
            (x1 + 2, max(12, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            color,
            1,
            cv2.LINE_AA,
        )


def _encode_latest(slug: str, cache: TrackCache | None) -> bytes | None:
    """Newest SHM frame for this camera as a JPEG, boxes optionally drawn on.

    Attach, read, encode, detach: the grid asks once a second, so a
    long-lived reader per open browser tab buys nothing."""
    reader = FrameRingReader(slug)
    try:
        rf = reader.get_latest()
        if rf is None:
            return None
        # NV12 → BGR (cv2.imencode wants BGR). RGB rings (dev/CPU) just need
        # the channel swap.
        if rf.pixel_format == PIXEL_FORMAT_NV12:
            bgr = cv2.cvtColor(rf.pixels, cv2.COLOR_YUV2BGR_NV12)
        else:
            bgr = cv2.cvtColor(rf.pixels, cv2.COLOR_RGB2BGR)
        bgr = cap_long_edge(bgr, _GRID_MAX_EDGE)
        if cache is not None:
            _draw_boxes(bgr, cache.latest(slug))
        ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
        return jpg.tobytes() if ok else None
    finally:
        with contextlib.suppress(Exception):
            reader.detach()


@live_router.get("/cameras/{camera_id}/live.jpg")
async def camera_live_jpeg(
    camera_id: UUID,
    request: Request,
    boxes: bool = Query(True, description="draw detection boxes on the frame"),
    _user: AuthUser = Depends(current_user),
) -> Response:
    """One frame: the newest downscaled SHM picture with detection boxes drawn
    on. The grid asks each tile for one of these a second.

    This replaced a Motion-JPEG stream, and the reason is the failure mode
    rather than the cost. An `<img>` fed `multipart/x-mixed-replace` keeps the
    last frame it received FOREVER once the stream ends — no error, no retry,
    no sign. One dead tile among seven read as live and was eight minutes old;
    every api restart froze the whole grid until someone reloaded the page.
    A tile that re-asks every second cannot fail that way: a request that dies
    is replaced by the next one, and the client knows the difference because it
    sees each request succeed or fail.

    The stream also spent most of its work on nothing. It paced at 5 fps while
    the ring — decimated to the adaptive detection rate — delivers 1-4, so
    roughly two thirds of the JPEG encoding re-sent a byte-identical frame.
    Asking once per second encodes once per second.

    `boxes=false` serves the clean frame (the UI's global overlay toggle).
    """
    row = await request.app.state.pool.fetchrow("SELECT slug FROM cameras WHERE id = $1", camera_id)
    if row is None:
        raise HTTPException(404, "camera not found")
    cache: TrackCache = request.app.state.track_cache
    jpg = await asyncio.to_thread(_encode_latest, row["slug"], cache if boxes else None)
    if jpg is None:
        # No frame in the ring: the ingestor is down or still connecting. 503
        # rather than a placeholder image — the tile keeps its last good frame
        # and marks itself stale, which is the truth.
        raise HTTPException(503, "no frame available for this camera")
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"cache-control": "no-store"},
    )


@live_router.get("/live/parked")
async def live_parked(
    request: Request,
    _user: AuthUser = Depends(current_user),
) -> list[dict]:
    """Which named vehicle stands in which place, one row per camera viewing
    it. The join itself lives in baba_core.parked, shared with the DIDA state
    snapshot so the Live badge and the mirror never disagree."""
    out = []
    for pv in await parked_assignments(request.app.state.pool):
        for cam_id in pv.camera_ids:
            out.append(
                {
                    "camera_id": cam_id,
                    "place": pv.place,
                    "name": pv.name,
                    "since": pv.since.isoformat(),
                }
            )
    return out


@live_router.get("/live/status")
async def live_status(
    request: Request,
    _user: AuthUser = Depends(current_user),
) -> dict[str, dict]:
    """Current per-camera live status `{slug: {fps, detections}}` — one cheap
    poll drives every tile's measured-fps label AND its detection badges,
    sourced from the same TrackCache the frame overlay uses."""
    cache: TrackCache = request.app.state.track_cache
    return cache.status()
