from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from baba_core.observed_record import OBSERVED_HEADLINE_TABLE
from baba_core.paths import MediaLayout
from baba_core.recordings import covers_until_sql
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user, require_admin
from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

recordings_router = APIRouter()



@recordings_router.get("/recordings/coverage")
async def recording_coverage(
    request: Request,
    camera_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> list[dict[str, Any]]:
    """MERGED recording coverage — adjacent segments collapsed into runs.

    The Activity timeline needs "where does footage exist" for every camera
    over a whole day. Raw segments are 60 s each: one camera-day is 1440 rows,
    seven cameras ≈ 10 000 — the capped /recordings/segments fetch (2000)
    covered only the most recent hours in the all-cameras view, which read as
    "recording keeps stopping" when recording is in fact continuous. Coverage
    is the RIGHT shape for drawing: continuous 24/7 recording collapses to ONE
    interval per camera per day. Rows mimic the Recording payload (synthetic
    id, null duration/size) so the client renders them unchanged; clip playback
    is camera+time based and never needs the underlying segment row."""
    pool = request.app.state.pool
    flt = SqlFilter()

    if camera_id is not None:
        flt.add("r.camera_id = ?", camera_id)
    if since is not None:
        flt.add("r.started_at >= ?", since)
    if until is not None:
        flt.add("r.started_at < ?", until)
    where = flt.where()
    rows = await pool.fetch(
        f"""
        SELECT r.camera_id, c.slug AS camera_slug, c.name AS camera_name,
               r.started_at, r.ended_at
        FROM recordings r
        JOIN cameras c ON c.id = r.camera_id
        {where}
        ORDER BY r.camera_id, r.started_at
        """,  # noqa: S608
        *flt.args,
    )
    # Merge runs per camera. 2 s tolerance bridges the segment-roll seam
    # (back-to-back 60 s files whose boundaries don't butt to the millisecond).
    gap = timedelta(seconds=2)
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cur_end: datetime | None = None
    for r in rows:
        start = r["started_at"]
        end = r["ended_at"] or start + timedelta(seconds=60)
        if (
            cur is not None
            and cur_end is not None
            and cur["_cam"] == r["camera_id"]
            and start - cur_end <= gap
        ):
            cur_end = max(cur_end, end)
            cur["ended_at"] = cur_end.isoformat()
            continue
        cur = {
            "_cam": r["camera_id"],
            "id": f"cov-{len(out)}",
            "camera": {
                "id": str(r["camera_id"]),
                "slug": r["camera_slug"],
                "name": r["camera_name"],
            },
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
            "duration_s": None,
            "size_bytes": None,
        }
        cur_end = end
        out.append(cur)
    for o in out:
        del o["_cam"]
    return out


# --- byte-range streaming of MP4 segments ---
#
# The browser <video> element needs HTTP Range support to seek without
# re-downloading the whole file. FastAPI's FileResponse handles single Range
# requests correctly on 0.115+; we still write the loop ourselves so we
# control the chunk size (low memory) and content-disposition.

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        # bytes=-N → last N bytes
        n = int(end_s)
        start = max(total - n, 0)
        end = total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    if start < 0 or end >= total or start > end:
        return None
    return start, end


# Raw-bitstream container per codec, for the salvage remux. Only codecs with
# a raw elementary-stream format can be salvaged this way.
_RAW_FORMATS = {"h264": "h264", "hevc": "hevc"}

# A segment declaring less than this fraction of its DB row's span is treated
# as timeline-broken (the measured failure declares ~0.1% of it; a healthy
# segment declares ~100%). Generous on purpose: a partial-but-honest segment
# (recorder restarted mid-window) must NOT be "salvaged" into a wrong timeline.
_BROKEN_TIMELINE_RATIO = 0.5

# Hard wall-clock caps on the ffprobe/ffmpeg children. A hung child (corrupt
# segment, stalled HEVC decode) must not block forever: the clip caller holds a
# per-camera lock, so one hang would freeze every later clip request for that
# camera. Probes are metadata-only (sub-second normally); the ffmpeg ceiling
# sits above the worst measured re-encode (_CLIP_MAX_ENC_S) with wide margin so
# it only ever trips on a genuine stall, never on a slow-but-progressing cut.
_FFPROBE_TIMEOUT_S = 30.0
_FFMPEG_TIMEOUT_S = 300.0


async def _communicate(proc, *, stdin: bytes | None = None) -> tuple[bytes, bytes]:
    """communicate() that never leaves the child behind: when the caller's
    deadline cancels it, the child is killed before the TimeoutError reaches
    the caller."""
    try:
        return await proc.communicate(stdin)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def _ffprobe_csv(path: Path, *entries: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v",
        *entries, "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async with asyncio.timeout(_FFPROBE_TIMEOUT_S):
        out, _ = await _communicate(proc)
    return out.decode().strip() if proc.returncode == 0 else None


async def _salvage_broken_segment(row, src: Path, cache_dir: Path) -> Path | None:
    """Rebuild a segment whose declared timeline is a fraction of its real
    span (a camera stamping broken RTSP timestamps + `-c copy` recording).
    The frames are all present — only their timing lies — so extract the raw
    bitstream and remux it at the rate MEASURED as frame count over the DB
    row's wall-clock span. The stream's own declared rate is not trusted: the
    measured case advertises 30 fps in the SPS while delivering ~14.7 real
    fps, and remuxing at the advertised rate would halve the timeline again.
    Returns the rebuilt file (cached in `cache_dir`), or None when this
    segment is not the broken-timeline case."""
    started = row["started_at"]
    ended = row["ended_at"]
    if ended is None:
        return None
    span = (ended - started).total_seconds()
    if span <= 0:
        return None
    probed = await _ffprobe_csv(
        src, "-show_entries", "stream=codec_name:format=duration"
    )
    if not probed:
        return None
    lines = probed.splitlines()
    codec = lines[0].strip().rstrip(",")
    try:
        declared = float(lines[-1])
    except (ValueError, IndexError):
        declared = 0.0
    if declared >= span * _BROKEN_TIMELINE_RATIO:
        return None  # honest timeline — not our case
    raw_fmt = _RAW_FORMATS.get(codec)
    if raw_fmt is None:
        return None
    out = cache_dir / f"salvaged_{src.parent.name}_{src.name}"
    if out.is_file():
        return out
    counted = await _ffprobe_csv(
        src, "-count_packets", "-show_entries", "stream=nb_read_packets"
    )
    try:
        n_frames = int((counted or "").strip().rstrip(","))
    except ValueError:
        return None
    if n_frames <= 0:
        return None
    fps = n_frames / span
    raw = out.with_suffix(".raw")
    tmp = out.with_suffix(".tmp.mp4")
    try:
        p1 = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-c", "copy", "-an", "-f", raw_fmt, str(raw),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(_FFMPEG_TIMEOUT_S):
            await _communicate(p1)
        if p1.returncode != 0:
            return None
        p2 = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-r", f"{fps:.4f}", "-f", raw_fmt,
            "-i", str(raw), "-c", "copy", "-movflags", "+faststart", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(_FFMPEG_TIMEOUT_S):
            await _communicate(p2)
        if p2.returncode != 0 or not await _has_video(tmp):
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(out)
        return out
    finally:
        raw.unlink(missing_ok=True)


async def _has_video(path: Path) -> bool:
    """Whether the file contains at least one video stream with real duration.
    The guard the clip chain's success test rests on — see its comment."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v",
        "-show_entries", "stream=codec_type:format=duration", "-of", "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async with asyncio.timeout(_FFPROBE_TIMEOUT_S):
        out, _ = await _communicate(proc)
    if proc.returncode != 0 or b"video" not in out:
        return False
    try:
        dur = float(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return dur > 0.0


async def _stream_file(path: Path, start: int, end: int, chunk: int = 64 * 1024):
    f = await asyncio.to_thread(path.open, "rb")
    try:
        await asyncio.to_thread(f.seek, start)
        remaining = end - start + 1
        while remaining > 0:
            data = await asyncio.to_thread(f.read, min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        f.close()


def _serve_with_range(full: Path, range_header: str | None) -> StreamingResponse:
    """Serve an MP4 honouring a single HTTP Range header (or the whole file)."""
    total = full.stat().st_size
    rng = _parse_range(range_header, total)
    common = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if rng is None:
        return StreamingResponse(
            _stream_file(full, 0, total - 1),
            media_type="video/mp4",
            headers={**common, "Content-Length": str(total)},
        )
    start, end = rng
    return StreamingResponse(
        _stream_file(full, start, end),
        status_code=206,
        media_type="video/mp4",
        headers={
            **common,
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(end - start + 1),
        },
    )



# --- on-demand activity clips ---------------------------------------------
#
# Playing a raw segment means the <video> downloads a big fragmented MP4 up to
# the event offset (no global seek index). Instead we cut a short faststart
# clip around the visit once, cache it, and serve that — first frame is near
# instant regardless of segment size/bitrate.
#
# The cut is addressed by ABSOLUTE wall-clock window (camera + start..end
# epoch) rather than a single segment, because a visit can span several 60s
# segments. We concat exactly the segments that overlap the window and trim to
# the precise window with one re-encode. Re-encoding (vs `-c copy`) is what
# makes the pre-roll land at a FIXED offset (a lossless cut can only start on a
# keyframe, so the lead-in would vary by up to one GOP) and also transcodes
# HEVC → H.264 so non-h264 cameras (e.g. west) play in the webview.

_CLIP_TTL_S = 6 * 3600  # cached clips older than this get pruned
# Caps on a single clip. These are what decide whether a VISIT arrives as one
# file or in pieces — and a visit delivered in pieces gets a progress bar per
# piece, each running 0→100, which reads as the player restarting rather than
# as one visit. The card promises a duration; the bar should measure it.
#
# Sized from a week of real visits (patio p50 41 s, p95 546 s, max 1170 s):
# 20 min covers everything seen so far with room to spare. A copy cut is cheap
# and flat in length — measured 10 min in 1.7 s producing 141 MB, and the
# browser streams it over Range rather than waiting for the whole file.
_CLIP_MAX_COPY_S = 1200.0
# The re-encode path is NOT flat: cost scales with content (west measured
# ~2.9 s for 33 s of HEVC), so an uncapped visit would block the request for
# minutes. 4 min keeps the worst case near 20 s, and HEVC is one camera whose
# visits are passers-by (max 3 s over the same week).
_CLIP_MAX_ENC_S = 240.0
# Floor on the requested window. A window shorter than a frame yields an empty
# file, which used to surface as "clip extraction failed" — a 500 that reads
# like a broken accelerator and would bury a real GPU incident in noise. It is
# a malformed request, so it answers as one.
_CLIP_MIN_S = 0.2
# How much of the window the OUTPUT seek handles (see `trim`). Big enough to
# swallow a keyframe snapback (GOP is ~1 s on these cameras), small enough that
# the packets demuxed and discarded stay negligible.
_SEEK_TAIL_S = 3.0
_H264_CODECS = frozenset({"h264", "avc1", "avc"})
_clip_locks: dict[str, asyncio.Lock] = {}


def _prune_clips(clips_dir: Path) -> None:
    """Best-effort eviction of stale cached clips + leftover concat lists. The
    dir holds a handful of small files, so a full scan is cheap."""
    cutoff = time.time() - _CLIP_TTL_S
    try:
        for f in clips_dir.iterdir():
            if f.suffix not in (".mp4", ".txt"):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _resolve_under(media_root: Path, rel: str) -> Path | None:
    """Resolve a stored relative path and confirm it stays under media_root."""
    full = (media_root / rel).resolve()
    try:
        full.relative_to(media_root)
    except ValueError:
        return None
    return full if full.is_file() else None


_FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def _vaapi_render_node() -> Path | None:
    dri = Path("/dev/dri")
    return next(iter(sorted(dri.glob("renderD*"))), None) if dri.is_dir() else None


@dataclass(frozen=True)
class _ClipCut:
    """One window of a camera's concatenated segments, cut into `out`."""

    out: Path
    global_in: float  # window start within the concatenation
    dur: float
    reencode: bool
    variant: str
    render_node: Path | None

    @property
    def tmp(self) -> Path:
        return self.out.with_suffix(".tmp.mp4")

    def commands(self, list_path: Path) -> list[list[str]]:
        """The ffmpeg attempts for one cut, in the order they are tried."""
        cat_in = ["-f", "concat", "-safe", "0"]
        # Seek in TWO stages: a fast input seek to a little before the
        # window, then an output seek for the rest.
        #
        # Input seek alone (`-ss` before `-i`, `-c copy`) does NOT drop
        # the skipped samples — it writes the whole demuxed span into
        # the file and hides the excess behind an MP4 edit list.
        # Measured on a 41 s patio clip: mvhd said 41.007 s while mdhd
        # (the real media) held 97.006 s, with elst presenting a 41 s
        # window at media_time 56 s. Browsers apply edit lists
        # inconsistently, so the picture started in the right place
        # (hence a correct burned-in OSD) while the progress bar
        # measured against 97 s of media — the operator's "time on the
        # clip is right, the bar is wrong".
        #
        # The output seek makes the trim real: samples before it are
        # discarded, mdhd matches mvhd, and the file no longer needs an
        # edit list to tell the truth. The input seek keeps it cheap —
        # only the last _SEEK_TAIL_S are demuxed and thrown away rather
        # than the whole concatenation.
        pre = min(_SEEK_TAIL_S, self.global_in)
        trim = [
            "-ss",
            f"{self.global_in - pre:.3f}",
            "-i",
            str(list_path),
            "-ss",
            f"{pre:.3f}",
            "-t",
            f"{self.dur:.3f}",
            "-an",
        ]
        # The fallback trim demuxes from the start of the concat and
        # lets the output seek do all the work. Slower (the whole
        # prefix is demuxed and discarded) but immune to the concat
        # demuxer's input seek failing — which it does, quietly, on
        # some cameras' segments: "could not seek to position" +
        # "Output file is empty" + exit code 0, a valid 262-byte MP4
        # with no frames. Measured on cabin cam12; the fast path stays
        # first because on healthy files it is ~instant.
        trim_safe = [
            "-i",
            str(list_path),
            "-ss",
            f"{self.global_in:.3f}",
            "-t",
            f"{self.dur:.3f}",
            "-an",
        ]
        tail = ["-movflags", "+faststart", "-f", "mp4", str(self.tmp)]
        copy_cut = [*_FFMPEG, *cat_in, *trim, "-c", "copy", *tail]
        copy_cut_safe = [*_FFMPEG, *cat_in, *trim_safe, "-c", "copy", *tail]
        encode = [self._transcode_command([*cat_in, *trim], tail)]
        return encode if self.reencode else [copy_cut, copy_cut_safe, *encode]

    def _transcode_command(self, source: list[str], tail: list[str]) -> list[str]:
        # H.264 → copy (instant, lossless). HEVC/unknown → transcode.
        #
        # On a GPU build the transcode MUST run on the GPU: silently
        # dropping to libx264 pegs the CPU on a box that is already
        # running detection + embedding, and hides that the accelerator
        # broke — the exact failure mode BABA refuses everywhere else
        # (see baba_core.variant). So the
        # accelerator for THIS variant is the only transcode attempt;
        # if it fails the request errors and says so. Only a cpu build
        # uses libx264, where it is the intended path rather than a
        # degradation.
        #
        # Downscale long edge to <=1920 (never upscale) on the re-encode
        # path only — copy can't filter, and it preserves full quality.
        # 1920 keeps west's ultrawide (4096x1152 -> 1920x540) legible.
        scale = ["-vf", r"scale=min(1920\,iw):-2"]
        if self.variant == "nvidia":
            return [
                *_FFMPEG,
                "-hwaccel",
                "cuda",
                *source,
                *scale,
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-cq",
                "26",
                *tail,
            ]
        if self.variant == "intel":
            # Intel: decode AND encode on the GPU. An HEVC camera (west)
            # can't be cut losslessly, and software transcode made its
            # clips take 11.3 s against 0.36 s for an H.264 camera — long
            # enough that the player just looks stuck. Frames stay in GPU
            # memory, so the scaler must be the VAAPI one (the CPU `scale`
            # filter cannot touch a hardware frame).
            if self.render_node is None:
                raise HTTPException(
                    500,
                    "clip transcode needs the GPU (BABA_VARIANT=intel) but no "
                    "/dev/dri render node is visible in this container",
                )
            # `format=nv12|vaapi,hwupload` tolerates both frame
            # homes: a stream the GPU could not decode arrives as
            # software frames, and handing those straight to
            # h264_vaapi dies with "a hardware frames reference is
            # required". The -vaapi_device gives hwupload its
            # device context.
            return [
                *_FFMPEG,
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                str(self.render_node),
                "-hwaccel_output_format",
                "vaapi",
                "-vaapi_device",
                str(self.render_node),
                *source,
                "-vf",
                r"format=nv12|vaapi,hwupload,scale_vaapi=w=min(1920\,iw):h=-2",
                "-c:v",
                "h264_vaapi",
                "-rc_mode",
                "CQP",
                "-qp",
                "26",
                *tail,
            ]
        return [
            *_FFMPEG,
            *source,
            *scale,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            *tail,
        ]

    async def attempt(self, cut_files: list[Path]) -> bytes | None:
        """One full cut over `cut_files`; None on success, else the last
        attempt's stderr."""
        # concat demuxer over the overlapping segments (same camera =
        # same codec/res, so a clean concat). A single file still goes
        # through it — one code path.
        listing = "ffconcat version 1.0\n" + "".join(
            f"file '{str(f).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
            for f in cut_files
        )
        list_path = self.out.parent / f"{self.out.name}.txt"
        list_path.write_text(listing)
        try:
            return await self._run(self.commands(list_path))
        finally:
            list_path.unlink(missing_ok=True)

    async def _run(self, attempts: list[list[str]]) -> bytes | None:
        last_err = b""
        for cmd in attempts:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(_FFMPEG_TIMEOUT_S):
                    _, last_err = await _communicate(proc)
            except TimeoutError:
                # A stalled attempt is killed and counted as a
                # failure so the fallback chain still runs rather
                # than the whole request hanging under the lock.
                last_err = b"ffmpeg timed out"
                self.tmp.unlink(missing_ok=True)
                continue
            # Exit code 0 is not success: ffmpeg exits cleanly after
            # "Output file is empty, nothing was encoded" and leaves a
            # valid MP4 with no frames, which a size check waves
            # through (262 bytes > 0). Success is the output actually
            # CONTAINING video — anything else falls to the next
            # attempt, and the chain's end is a loud error, never a
            # silent empty clip.
            if proc.returncode == 0 and self.tmp.is_file() and await _has_video(self.tmp):
                return None
            self.tmp.unlink(missing_ok=True)
        return last_err

    async def run(self, camera_id: UUID, pairs: list[tuple[dict, Path]]) -> None:
        err = await self.attempt([f for _, f in pairs])
        if err is not None:
            # Some cameras stamp their RTSP frames with broken
            # timestamps; a copied segment then DECLARES a fraction of
            # its real timeline (measured on cabin: 65 MB / 5 min of
            # media, moov says 0.36 s) and every seek beyond that
            # produces nothing. The media itself is intact, so rebuild
            # any such segment's timeline — remux the raw bitstream at
            # the rate measured from frame count over the row's real
            # span — and cut again. Salvaged segments are cached
            # alongside clips, so one rebuild serves every window over
            # that segment.
            salvaged = [await _salvage_broken_segment(r, f, self.out.parent) for r, f in pairs]
            if any(s is not None for s in salvaged):
                log.warning(
                    "clip: segment timeline broken (camera writes bad "
                    "timestamps) — cut from salvaged rebuild, cam=%s",
                    camera_id,
                )
                err = await self.attempt(
                    [s or f for s, (_, f) in zip(salvaged, pairs, strict=True)]
                )
        if err is not None:
            # Loud on purpose: on a GPU build this means the
            # accelerator is broken, and that must surface as an
            # incident rather than quietly costing every clip a CPU
            # transcode.
            log.error(
                "clip extract FAILED cam=%s start=%.1f dur=%.1f nseg=%d "
                "variant=%s reencode=%s: %s",
                camera_id,
                self.global_in,
                self.dur,
                len(pairs),
                self.variant,
                self.reencode,
                (err or b"").decode("utf-8", "replace")[:300],
            )
            raise HTTPException(500, "clip extraction failed")
        self.tmp.rename(self.out)


@recordings_router.get("/recordings/cameras/{camera_id}/clip")
async def get_camera_clip(
    camera_id: UUID,
    request: Request,
    start: float = Query(..., description="window start, epoch seconds (UTC)"),
    end: float = Query(..., description="window end, epoch seconds (UTC)"),
    range_header: str | None = Header(default=None, alias="range"),
):
    """Cached, faststart clip for an absolute time window, spanning whatever
    segments overlap [start, end].

    For H.264 cameras the clip is a **lossless `-c copy` cut** — no transcode,
    no GPU, ~0.1-0.4s, full quality. The cut snaps back to the keyframe at or
    before the window start (GOP is ~1s here), so the pre-roll is shown in full
    (never less). Only HEVC cameras (e.g. west, which webviews can't decode) are
    re-encoded to H.264 — NVENC first, libx264 fallback, downscaled to keep the
    encode quick."""
    if not math.isfinite(start) or not math.isfinite(end):
        raise HTTPException(400, "window timestamps must be finite")
    if end - start < _CLIP_MIN_S:
        raise HTTPException(
            400, f"window must span at least {_CLIP_MIN_S}s (got {end - start:.3f}s)"
        )
    pool = request.app.state.pool

    end = min(end, start + _CLIP_MAX_COPY_S)
    try:
        start_dt = datetime.fromtimestamp(start, tz=UTC)
        end_dt = datetime.fromtimestamp(end, tz=UTC)
    except (OverflowError, OSError, ValueError) as e:
        raise HTTPException(400, "window timestamps are outside the supported range") from e
    rows = await pool.fetch(
        f"""
        SELECT path, started_at, ended_at, codec
        FROM recordings
        WHERE camera_id = $1
          AND started_at < $3
          AND {covers_until_sql()} > $2
        ORDER BY started_at ASC
        """,  # noqa: S608
        camera_id,
        start_dt,
        end_dt,
    )
    if not rows:
        raise HTTPException(404, "no recording covers this window")

    # Re-encode only when a segment is NOT confirmed H.264 (HEVC, or unknown →
    # play it safe and produce something the browser can decode).
    encoded_end = datetime.fromtimestamp(min(end, start + _CLIP_MAX_ENC_S), tz=UTC)
    reencode = any((r["codec"] or "").lower() not in _H264_CODECS for r in rows)
    if reencode:
        end = encoded_end.timestamp()
        rows = [r for r in rows if r["started_at"] < encoded_end]
    reencode = any((r["codec"] or "").lower() not in _H264_CODECS for r in rows)

    media_root = await asyncio.to_thread(Path(request.app.state.config.media_path).resolve)
    pairs: list[tuple[dict, Path]] = []
    for r in rows:
        f = _resolve_under(media_root, r["path"])
        if f is not None:
            pairs.append((r, f))
    if not pairs:
        raise HTTPException(404, "recording files missing on disk")

    clips_dir = MediaLayout(media_root).clips
    clips_dir.mkdir(parents=True, exist_ok=True)
    name = f"cam_{camera_id}_{int(start * 1000)}_{int(end * 1000)}.mp4"
    out = clips_dir / name

    # A cached hit is only a hit if it holds frames: before the fail-loud
    # validation below existed, a failed concat seek could cache a valid
    # 262-byte MP4 with no video, and this path would serve it forever. Probe
    # only suspiciously small files so real clips pay nothing.
    if out.is_file() and out.stat().st_size < 4096 and not await _has_video(out):
        out.unlink(missing_ok=True)

    if not out.is_file():
        lock = _clip_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if not out.is_file():  # re-check: another request may have cut it
                # Offset of the window start within the (contiguous)
                # concatenation: the first listed segment is the one the window
                # opens in. `-ss` before `-i` keyframe-seeks here, landing on
                # the keyframe at/before the start.
                cut = _ClipCut(
                    out=out,
                    global_in=max(0.0, start - rows[0]["started_at"].timestamp()),
                    dur=end - start,
                    reencode=reencode,
                    variant=os.environ.get("BABA_VARIANT", "cpu").strip().lower(),
                    render_node=await asyncio.to_thread(_vaapi_render_node),
                )
                await cut.run(camera_id, pairs)
        _clip_locks.pop(name, None)
        _prune_clips(clips_dir)

    return _serve_with_range(out, range_header)


# --- recording policy (global singleton) ----------------------------------
#
# One global policy the operator configures in Settings → Recording. The
# recorder re-reads `recording_settings` every retention pass, so changes
# here take effect within one interval. The disk water-marks are the safety
# net that guarantees the media volume never fills to 100% again.


class RecordingSettingsIn(BaseModel):
    mode: Literal["continuous", "activity"]
    retention_days: int = Field(ge=1, le=365)
    disk_high_water_pct: int = Field(ge=50, le=99)
    disk_low_water_pct: int = Field(ge=40, le=98)
    activity_buffer_minutes: int = Field(ge=0, le=60)


def _separate_volume(path: Path, root: Path) -> bool:
    return path.exists() and path.stat().st_dev != root.stat().st_dev


async def _recording_settings_payload(request: Request) -> dict[str, Any]:
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT mode, retention_days, disk_high_water_pct, disk_low_water_pct, "
        "activity_buffer_minutes, updated_at FROM recording_settings WHERE id = 1"
    )
    if row is None:
        raise HTTPException(500, "recording_settings singleton missing")
    media_root = Path(request.app.state.config.media_path)

    async def _usage(path: Path) -> dict[str, Any] | None:
        try:
            du = await asyncio.to_thread(shutil.disk_usage, path)
        except OSError:
            return None
        return {
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "used_pct": round(du.used / du.total * 100, 1) if du.total else 0.0,
        }

    disk = await _usage(media_root)
    # Second tier: the small-file directories (crops, clip cache, thumbnails,
    # identity/scene photos) can live on a different, faster volume than the
    # bulk segments — reported separately so the operator sees BOTH bars
    # instead of guessing where the fast disk stands. Same device as the bulk
    # tier (no split mounted) → reported as null so the UI shows one bar.
    fast_root = MediaLayout(media_root).crops
    fast: dict[str, Any] | None = None
    try:
        if await asyncio.to_thread(_separate_volume, fast_root, media_root):
            fast = await _usage(fast_root)
    except OSError:
        fast = None
    return {
        "mode": row["mode"],
        "retention_days": row["retention_days"],
        "disk_high_water_pct": row["disk_high_water_pct"],
        "disk_low_water_pct": row["disk_low_water_pct"],
        "activity_buffer_minutes": row["activity_buffer_minutes"],
        "updated_at": row["updated_at"].isoformat(),
        "disk": disk,
        "disk_fast": fast,
    }


@recordings_router.get("/recording-settings")
async def get_recording_settings(request: Request) -> dict[str, Any]:
    return await _recording_settings_payload(request)


@recordings_router.put("/recording-settings")
async def put_recording_settings(
    body: RecordingSettingsIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    if body.disk_low_water_pct >= body.disk_high_water_pct:
        raise HTTPException(400, "disk_low_water_pct must be below disk_high_water_pct")
    pool = request.app.state.pool
    before = await pool.fetchrow(
        "SELECT mode, retention_days, disk_high_water_pct, disk_low_water_pct, "
        "activity_buffer_minutes FROM recording_settings WHERE id = 1"
    )
    # Shortening retention, or narrowing the mode, deletes footage — the
    # recorder's age cap unlinks the FILES on its next pass and only then drops
    # the rows. That is the same destruction as `DELETE /recordings`, which is
    # admin-only and audited and says so; this route sat at operator tier and
    # wrote nothing but a log line. Benign edits — raising retention, moving a
    # watermark, widening the buffer — stay where they were.
    destructive = before is not None and (
        body.retention_days < before["retention_days"]
        or (before["mode"] == "continuous" and body.mode != "continuous")
    )
    if destructive and user.role != "admin":
        raise HTTPException(
            403,
            "shortening retention or narrowing the recording mode deletes "
            "footage — that requires an administrator",
        )
    await pool.execute(
        "UPDATE recording_settings SET mode = $1, retention_days = $2, "
        "disk_high_water_pct = $3, disk_low_water_pct = $4, "
        "activity_buffer_minutes = $5, updated_at = now() WHERE id = 1",
        body.mode,
        body.retention_days,
        body.disk_high_water_pct,
        body.disk_low_water_pct,
        body.activity_buffer_minutes,
    )
    await write_audit(
        pool,
        user=user,
        resource_type="recordings",
        op="settings",
        payload={
            "before": dict(before) if before is not None else None,
            "after": body.model_dump(),
            "destructive": destructive,
        },
    )
    log.info(
        "recording settings updated by %s: mode=%s keep=%dd disk=%d/%d buf=%dmin",
        user.username,
        body.mode,
        body.retention_days,
        body.disk_high_water_pct,
        body.disk_low_water_pct,
        body.activity_buffer_minutes,
    )
    return await _recording_settings_payload(request)


# --- purge all recordings (destructive, admin-only) ------------------------
#
# Wipes every recorded video: the whole `recordings` index and every segment
# file + cached clip on disk. Events, tracks and identities are NOT touched —
# they have their own retention. Admin-only and audited.
#
# The actual deletion is NOT done here. A full archive is tens of thousands of
# files on the slow media tier; deleting them inline blows past Cloudflare's
# ~100s proxy timeout (524) and the request dies before the DB is even cleared.
# It's also the recorder's job: it owns the media volume, deletes off the
# request path already, and re-derives `recordings` rows from the files on disk
# at every bootstrap — so rows only stay gone once the FILES are gone. This
# endpoint records the request (a flag on the recording_settings singleton) and
# nudges the recorder over LISTEN/NOTIFY; the recorder drains it in the
# background: wipe files, drop rows, clear the flag. Restart-safe (the flag
# persists), so a coincident recorder restart still finishes the wipe.


class PurgeResult(BaseModel):
    scheduled: bool
    scope: str
    pending_rows: int
    pending_bytes: int
    pending_events: int


@recordings_router.delete("/recordings", status_code=202, response_model=PurgeResult)
async def purge_all_recordings(
    request: Request,
    scope: Literal["recordings", "record"] = "recordings",
    user: AuthUser = Depends(require_admin),
) -> PurgeResult:
    """Schedule a purge. Returns 202 immediately; the recorder does the wipe in
    the background. Destructive, admin-only, audited.

    `recordings` erases the video and its index and nothing else. `record`
    additionally erases everything BABA has observed — see
    `baba_core.observed_record` for what that is and, just as importantly, what
    it is not: the cameras, zones, regions, identities, users and settings the
    system was configured with all survive either scope.
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # What's about to go — reported to the operator so the toast is
        # meaningful; the recorder does the actual deletion asynchronously.
        row = await conn.fetchrow(
            "SELECT count(*) AS n, COALESCE(sum(size_bytes), 0) AS bytes FROM recordings"
        )
        pending_events = 0
        if scope == "record":
            pending_events = int(
                await conn.fetchval(f"SELECT count(*) FROM {OBSERVED_HEADLINE_TABLE}")  # noqa: S608
            )
        await conn.execute(
            "UPDATE recording_settings"
            "   SET purge_requested_at = now(), purge_scope = $1"
            " WHERE id = 1",
            scope,
        )
        # Wake the recorder now; the flag is the restart-safe backstop.
        await conn.execute("NOTIFY recording_purge")

    pending_rows = int(row["n"])
    pending_bytes = int(row["bytes"])
    await write_audit(
        pool,
        user=user,
        resource_type="recordings",
        op="purge",
        payload={
            "scope": scope,
            "pending_rows": pending_rows,
            "pending_bytes": pending_bytes,
            "pending_events": pending_events,
        },
    )
    log.warning(
        "%s purge scheduled by %s: %d segments (%.2fGB)%s queued for the recorder",
        scope,
        user.username,
        pending_rows,
        pending_bytes / 1e9,
        f", {pending_events} events" if scope == "record" else "",
    )
    return PurgeResult(
        scheduled=True,
        scope=scope,
        pending_rows=pending_rows,
        pending_bytes=pending_bytes,
        pending_events=pending_events,
    )
