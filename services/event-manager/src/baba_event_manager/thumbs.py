"""Thumbnail capture.

Three strategies, tried in order:
1. Serve the JPEG SnapshotBaker already produced — the detection's own SHM
   frame with its box baked in, captured live (see snapshot.py). Cheapest and
   most accurate: no decoder work, and frame + box are one array from one
   instant. Absent only when no active detection ever cleared the bake gate.
2. Extract a frame from the recording segment that covers the track's
   midpoint. Works for closed segments — same frame the user sees on
   Play. Falls through when only an open segment covers the moment.
3. Fall back to a live go2rtc snapshot — current scene at finalize time.
   By the time we hit this branch the subject has often already left the
   frame, producing empty-scene thumbnails. Last resort.

All end up as `media/thumbnails/<track_id>.jpg`. Capture is best-effort —
the track row exists either way, UI gracefully handles a missing thumb.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import httpx
from baba_core.paths import THUMBNAILS, MediaLayout
from PIL import Image

log = logging.getLogger(__name__)


class ThumbnailCapture:
    def __init__(
        self, media_path: Path, go2rtc_url: str, max_width: int,
        go2rtc_auth: tuple[str, str],
    ) -> None:
        self._media_root = media_path
        self._dir = MediaLayout(media_path).thumbnails
        self._dir.mkdir(parents=True, exist_ok=True)
        self._go2rtc_url = go2rtc_url.rstrip("/")
        self._max_w = max_width
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2, read=5, write=2, pool=5), auth=go2rtc_auth
        )

    async def close(self) -> None:
        await self._client.aclose()

    # --- primary: an already-encoded JPEG (box baked in by SnapshotBaker) ---

    def save_jpeg(self, target_name: str, data: bytes) -> str | None:
        """Write a ready JPEG to media/thumbnails/<name>.jpg. Small (~tens of
        KB) so the write is inline; returns the relative path or None."""
        try:
            out_path = self._dir / f"{target_name}.jpg"
            out_path.write_bytes(data)
            return MediaLayout.rel(THUMBNAILS, f"{target_name}.jpg")
        except Exception:
            log.exception("save_jpeg failed for %s", target_name)
            return None

    # --- fallback: extract from recording segment ---

    async def from_recording(
        self, target_name: str, segment_rel_path: str, offset_seconds: float
    ) -> str | None:
        """Use ffmpeg to extract a single frame from a closed segment at the
        given offset. Returns relative path or None on failure."""
        seg_path = self._media_root / segment_rel_path
        if not seg_path.is_file():
            return None
        out_path = self._dir / f"{target_name}.jpg"
        # -ss before -i is fast seek using container indexes; for closed MP4
        # with faststart it's accurate enough and avoids decoding from start.
        # scale=W:-2 keeps even-height, preserves aspect.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, offset_seconds):.3f}",
            "-i",
            str(seg_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={self._max_w}:-2",
            "-q:v",
            "3",
            "-y",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("ffmpeg thumb extract timed out: %s", segment_rel_path)
            return None
        if proc.returncode != 0:
            log.warning(
                "ffmpeg thumb extract failed (%s): %s",
                segment_rel_path,
                (stderr or b"")[-300:].decode(errors="replace"),
            )
            return None
        if not out_path.is_file() or out_path.stat().st_size == 0:
            return None
        return MediaLayout.rel(THUMBNAILS, f"{target_name}.jpg")

    # --- last resort: live snapshot from go2rtc ---

    async def from_live_snapshot(self, camera_slug: str, target_name: str) -> str | None:
        # go2rtc's /api/frame.jpeg waits for the next I-frame from the live
        # producer and returns 500 when that wait times out. High-bitrate
        # H.264 streams with long GOPs (Hikvision Main profile L5.1) miss
        # the window often. Retry twice with backoff before giving up so
        # events on Patio don't end up with empty thumbnails.
        url = f"{self._go2rtc_url}/api/frame.jpeg?src={camera_slug}"
        raw: bytes | None = None
        last_err: str | None = None
        for attempt in range(3):
            try:
                r = await self._client.get(url)
                r.raise_for_status()
                raw = r.content
                break
            except httpx.HTTPError as e:
                last_err = str(e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        if raw is None:
            log.warning("snapshot fetch failed for %s after 3 attempts: %s", camera_slug, last_err)
            return None
        try:
            return await asyncio.to_thread(self._encode_and_save, raw, target_name)
        except Exception:
            log.exception("encode/save failed for %s", camera_slug)
            return None

    def _encode_and_save(self, raw: bytes, target_name: str) -> str:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > self._max_w:
                new_h = max(1, round(h * self._max_w / w))
                im = im.resize((self._max_w, new_h), Image.LANCZOS)
            out_path = self._dir / f"{target_name}.jpg"
            im.save(out_path, "JPEG", quality=82, optimize=True)
        return MediaLayout.rel(THUMBNAILS, f"{target_name}.jpg")
