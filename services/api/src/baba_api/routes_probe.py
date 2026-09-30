"""Camera probe + quick-add — the "give me user/pass, I'll figure out the
rest" path on top of LAN discovery.

probe(ip, user, pass):
  1. Try ONVIF — if the camera speaks it, we get vendor/model + the exact
     RTSP URI(s) that camera advertises. Most reliable.
  2. Fallback: brute-force a list of vendor templates with the provided
     creds; for each, validate via ffprobe.
  3. For every working candidate, capture a snapshot via ffmpeg so the UI
     can show what the user is about to add.

quickadd(stream_url, ip): create a camera row with sane defaults — auto
slug from IP, default fps / max_edge / retention. No form filling.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.models import Camera
from baba_api.onvif import OnvifClient
from baba_api.routes import _CAMERA_FIELDS
from baba_api.stream_presets import STREAM_PRESETS, fill
from baba_api.stream_url import FFMPEG_PROTOCOL_WHITELIST, validate_stream_url

log = logging.getLogger(__name__)

probe_router = APIRouter()


class StreamPresetOut(BaseModel):
    id: str
    label: str
    main: str
    sub: str | None


@probe_router.get("/stream-presets", response_model=list[StreamPresetOut])
async def stream_presets() -> list[StreamPresetOut]:
    return [StreamPresetOut(id=p.id, label=p.label, main=p.main, sub=p.sub) for p in STREAM_PRESETS]


# ------------- ffprobe + snapshot helpers -------------


async def _run_capture(cmd: list[str], deadline_s: float) -> tuple[int, bytes] | None:
    """Run `cmd` and return (returncode, stdout); None when it cannot start or
    outlives `deadline_s`, in which case it is killed rather than left behind."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=deadline_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    return proc.returncode, stdout


async def _ffprobe(url: str, timeout_s: float = 8.0) -> dict | None:
    """Returns the first video stream info if ffprobe succeeds, else None."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        FFMPEG_PROTOCOL_WHITELIST,
        "-rtsp_transport",
        "tcp",
        "-timeout",
        str(int(timeout_s * 1_000_000)),
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        url,
    ]
    ran = await _run_capture(cmd, timeout_s + 2)
    if ran is None or ran[0] != 0:
        return None
    try:
        data = json.loads(ran[1])
    except ValueError:
        return None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def _fps_from(s: dict) -> float | None:
    def _eval(r: str | None) -> float | None:
        if not r or r == "0/0":
            return None
        try:
            n, d = r.split("/")
            n = float(n)
            d = float(d)
        except (ValueError, AttributeError):
            return None
        return n / d if d else None

    return _eval(s.get("avg_frame_rate")) or _eval(s.get("r_frame_rate"))


async def _snapshot_b64(url: str, max_width: int = 480, timeout_s: float = 8.0) -> str | None:
    """Pull one frame, downscale, return as base64-encoded JPEG (data URL)."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        FFMPEG_PROTOCOL_WHITELIST,
        "-rtsp_transport",
        "tcp",
        "-timeout",
        str(int(timeout_s * 1_000_000)),
        "-i",
        url,
        "-frames:v",
        "1",
        "-vf",
        f"scale={max_width}:-2",
        "-q:v",
        "5",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    ran = await _run_capture(cmd, timeout_s + 2)
    if ran is None or ran[0] != 0 or not ran[1]:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(ran[1]).decode()


# ------------- probe pipeline -------------


class ProbeIn(BaseModel):
    ip: str = Field(..., min_length=7, max_length=64)
    username: str = Field("", max_length=64)
    password: str = Field("", max_length=256)


class StreamCandidate(BaseModel):
    template_id: str
    label: str
    stream_url: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    snapshot_b64: str | None = None


class ProbeOut(BaseModel):
    ip: str
    vendor: str | None = None
    model: str | None = None
    duration_ms: int
    candidates: list[StreamCandidate]


async def _verify_url(template_id: str, label: str, url: str) -> StreamCandidate | None:
    info = await _ffprobe(url, timeout_s=6.0)
    if info is None:
        return None
    snap = await _snapshot_b64(url, timeout_s=6.0)
    return StreamCandidate(
        template_id=template_id,
        label=label,
        stream_url=url,
        codec=info.get("codec_name"),
        width=info.get("width"),
        height=info.get("height"),
        fps=_fps_from(info),
        snapshot_b64=snap,
    )


@probe_router.post("/cameras/probe", response_model=ProbeOut)
async def probe(payload: ProbeIn) -> ProbeOut:
    start = time.monotonic()
    candidates: list[StreamCandidate] = []
    vendor: str | None = None
    model: str | None = None
    seen_urls: set[str] = set()

    # --- 1. ONVIF path ---
    if payload.username and payload.password:
        try:
            async with OnvifClient(payload.ip, payload.username, payload.password) as c:
                info = await c.get_device_information()
                if info:
                    vendor, model = info.vendor, info.model
                streams = await c.get_streams()
        except Exception:
            log.exception("onvif probe failed for %s", payload.ip)
            streams = []

        # ONVIF gives us URIs without credentials; inject them so ffprobe can auth.
        for s in streams:
            from urllib.parse import quote, urlparse, urlunparse

            try:
                p = urlparse(s.rtsp_uri)
                netloc = f"{quote(payload.username, safe='')}:{quote(payload.password, safe='')}@{p.hostname or payload.ip}"
                if p.port:
                    netloc += f":{p.port}"
                url = urlunparse(p._replace(netloc=netloc))
            except ValueError:
                url = s.rtsp_uri
            if url in seen_urls:
                continue
            seen_urls.add(url)
            cand = await _verify_url(
                f"onvif:{s.profile_token}",
                f"ONVIF — {s.profile_name}",
                url,
            )
            if cand:
                candidates.append(cand)

    # --- 2. Template brute-force, in parallel ---
    if not candidates:
        urls = []
        for preset in STREAM_PRESETS:
            for role, tpl in (("main", preset.main), ("sub", preset.sub)):
                if tpl is None or "{path}" in tpl or tpl.startswith("onvif://"):
                    continue
                url = fill(tpl, ip=payload.ip, user=payload.username, password=payload.password)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                urls.append((f"{preset.id}:{role}", preset.label, url))
        # Limit parallelism so a slow camera doesn't get hammered.
        sem = asyncio.Semaphore(3)

        async def _wrap(tid, lbl, u):
            async with sem:
                return await _verify_url(tid, lbl, u)

        results = await asyncio.gather(*[_wrap(t, lbl, u) for t, lbl, u in urls])
        candidates.extend(c for c in results if c is not None)

    return ProbeOut(
        ip=payload.ip,
        vendor=vendor,
        model=model,
        duration_ms=int((time.monotonic() - start) * 1000),
        candidates=candidates,
    )


# ------------- quickadd -------------


class QuickaddIn(BaseModel):
    stream_url: str = Field(..., min_length=1)
    substream_url: str | None = Field(None, min_length=1)
    ip: str | None = None
    name: str | None = None


def _slug_from_ip(ip: str) -> str:
    last = ip.rsplit(".", 1)[-1] if "." in ip else ip
    # Strip anything non-alnum.
    last = re.sub(r"[^a-z0-9]", "", last.lower()) or "cam"
    return f"cam{last}"


def _slug_from_name(name: str) -> str | None:
    """Best-effort slugify of a human-typed name. Returns None if the
    name has no slug-safe characters (e.g. emoji-only), so the caller
    knows to fall back to the IP-derived form."""
    # Lowercase, swap whitespace and dashes for underscores, drop the
    # rest. Collapse consecutive underscores.
    s = re.sub(r"[\s\-]+", "_", name.strip().lower())
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return None
    # Slugs must start with [a-z0-9]. After the steps above the only
    # way that can fail is a leading underscore, which strip("_")
    # already handled, but stay defensive.
    if not s[0].isalnum():
        s = f"cam_{s}"
    return s[:64]


@probe_router.post("/cameras/quickadd", response_model=Camera, status_code=201)
async def quickadd(payload: QuickaddIn, request: Request) -> Camera:
    # Reject non-stream schemes (file://, …) before persisting a URL that the
    # ingestor will later hand to ffmpeg — same SSRF/file-read guard as the
    # probe endpoints.
    validate_stream_url(payload.stream_url)
    if payload.substream_url:
        validate_stream_url(payload.substream_url)
    pool = request.app.state.pool
    name = payload.name or (f"Camera @ {payload.ip}" if payload.ip else "Camera")

    # Prefer a slug derived from the user-typed name (so operators see
    # `backyard` instead of `cam16` in media paths, NATS subjects, and
    # filenames). Fall back to the IP-derived form when the user did
    # not provide a name — e.g. zero-config LAN discovery flows. On
    # collision, append _2, _3, …
    base = None
    if payload.name:
        base = _slug_from_name(payload.name)
    if not base:
        base = _slug_from_ip(payload.ip or "cam")
    slug = base
    n = 1
    while True:
        existing = await pool.fetchval("SELECT 1 FROM cameras WHERE slug = $1", slug)
        if not existing:
            break
        n += 1
        slug = f"{base}_{n}"
        if n > 99:
            raise HTTPException(409, "could not pick a unique slug — try editing manually")

    row = await pool.fetchrow(
        f"""
        INSERT INTO cameras (slug, name, stream_url, substream_url, enabled, target_fps,
                             downscale_max_edge,
                             recording_enabled)
        VALUES ($1, $2, $3, $4, true, 5, 1280, true)
        RETURNING {_CAMERA_FIELDS}
        """,  # noqa: S608
        slug,
        name,
        payload.stream_url,
        payload.substream_url,
    )
    return Camera(**dict(row))
