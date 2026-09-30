"""Bake the detection box into a thumbnail at capture time.

The activity-thumbnail box used to be reconstructed AFTER the track ended:
pick a sampled bbox, extract a frame from the recording at that timestamp,
and store the box for the browser to overlay. Three decoupled things
(box, frame, overlay maths) that could — and did — disagree, reading as a
"misplaced/mirrored" box on cameras where a track transitions or a phantom
detection coexists with the real subject.

Here the box is drawn onto the EXACT frame it was computed on. The tracker
message carries the `sequence` of the SHM frame the detector ran; we fetch
that same frame from the ring (`get_by_sequence`, the way the embedder seeks
its crop frame), draw the box, and JPEG-encode. Frame, box and coordinate
space are one array from one instant — the box sits on the subject by
construction. No recording seek, no clock matching, no browser letterbox
maths. Captured while the track is still live, so short tracks whose segment
is still open at finalize get a proper boxed thumbnail too.
"""

from __future__ import annotations

import contextlib
import logging

import cv2
from baba_core.frame_ring import PIXEL_FORMAT_NV12, FrameRingReader

log = logging.getLogger(__name__)

# #f59e0b (web --color-baba-accent), as cv2 BGR — the same amber the old
# browser overlay drew, so the feed looks unchanged bar the box now being
# baked in.
_BOX_BGR = (11, 158, 245)


class SnapshotBaker:
    """Produces a box-baked JPEG from the SHM frame a detection ran on.

    One FrameRingReader per camera slug, attached lazily and reused (the ring
    is a shared tmpfs the ingestor writes and every SHM reader attaches to)."""

    def __init__(self, max_width: int) -> None:
        self._max_w = max_width
        self._readers: dict[str, FrameRingReader] = {}

    def _reader(self, slug: str) -> FrameRingReader:
        r = self._readers.get(slug)
        if r is None:
            r = FrameRingReader(slug)
            self._readers[slug] = r
        return r

    def bake(
        self,
        slug: str,
        sequence: int,
        nbox: tuple[float, float, float, float],
    ) -> bytes | None:
        """Fetch the exact frame `sequence`, draw the normalised box on it and
        return a JPEG. None when the frame is no longer in the ring (rolled
        out) or decode/encode fails — the caller falls back to a recording or
        live snapshot."""
        try:
            rf = self._reader(slug).get_by_sequence(sequence)
        except (OSError, ValueError):
            log.debug("snapshot ring fetch failed for %s seq=%s", slug, sequence)
            return None
        if rf is None:
            return None
        try:
            if rf.pixel_format == PIXEL_FORMAT_NV12:
                bgr = cv2.cvtColor(rf.pixels, cv2.COLOR_YUV2BGR_NV12)
            else:
                bgr = cv2.cvtColor(rf.pixels, cv2.COLOR_RGB2BGR)
            h, w = bgr.shape[:2]
            if w > self._max_w:
                nh = max(1, round(h * self._max_w / w))
                bgr = cv2.resize(bgr, (self._max_w, nh), interpolation=cv2.INTER_AREA)
                h, w = bgr.shape[:2]
            # Draw AFTER the downscale so the stroke stays a crisp 2 px.
            x1 = max(0, min(w - 1, round(nbox[0] * w)))
            y1 = max(0, min(h - 1, round(nbox[1] * h)))
            x2 = max(0, min(w - 1, round(nbox[2] * w)))
            y2 = max(0, min(h - 1, round(nbox[3] * h)))
            cv2.rectangle(bgr, (x1, y1), (x2, y2), _BOX_BGR, 2)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                return None
            return buf.tobytes()
        except Exception:
            log.exception("snapshot bake failed for %s seq=%s", slug, sequence)
            return None

    def close(self) -> None:
        for r in self._readers.values():
            with contextlib.suppress(Exception):
                r.detach()
        self._readers.clear()
