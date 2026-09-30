"""Read a face again from the recording, at the size it was actually filmed.

Faces are embedded off the SHM ring, and the ring is downscaled: patio records
3040x1368 and carries 1280, so 2.38x of the picture is gone before anything
looks at it. Measured on 31.08, the operator standing on the patio looking
straight into the camera for fifteen seconds — the best face the ring could
offer was 57.9 px, under the floor, and the system said nothing. The same
moment cut from the segment is 124 x 146 px and matches his enrolled
photographs at 0.242, with the next person 0.614 behind.

That is not a threshold problem and no threshold fixes it. It is the same
lesson plate reading already learned: the answer is in the recording, and the
ring is a preview of it.

So a person track whose ring face was too small to identify anybody has its
best face read again from the segment, minutes after the fact, with nothing
waiting on it. What that produces goes back onto the sample and the track, and
the re-ID resweep — which names anonymous tracks from curated evidence alone —
picks it up on its next pass. Nothing here decides who anybody is.

Cheap by construction: only tracks that have a face at all AND could not use
it, only their best few samples, and each track only once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from baba_core.face import FRONTALITY_MAX, align_face, frontality_residual, set_canonical_face
from baba_core.paths import FACE_CROPS, MediaLayout
from baba_core.recordings import covers_until_sql

log = logging.getLogger(__name__)

# The floor the identity decision answers to, in pixels of the face's shorter
# side. Kept here as a plain number rather than imported across service
# boundaries: this module only decides what is WORTH re-reading, and re-reading
# a face that already clears the floor buys nothing.
_FACE_ID_MIN_PX = 60.0

# Nothing is waiting on this, so it runs rarely and takes a small bite.
_INTERVAL_S = 120.0
_TRACKS_PER_PASS = 12

# Per track, the best few samples by the detector's own score. One frame is
# usually enough, but the top-scoring crop is not always the one that embeds
# best — the same reason finalize takes a MIN over several.
_SAMPLES_PER_TRACK = 3

# How much room around the person box the crop takes. The detector needs the
# head, and a box drawn on a downscaled frame is not exact.
_PAD = 0.15

# One frame is a coin toss. Measured 2026-09-09 over 40 samples: at the exact
# instant a sample claims, the detector finds a face in 8 of them; allowed to
# look around that instant it finds one in 34. A head turns, blinks and dips,
# and the sample's own moment is only the one the ring happened to publish.
# The window stays short because the box is held still while the person is
# not: a second of walking already carries a face out of a box drawn a second
# ago. Frames are read forward from the start of the window rather than sought
# one by one — seeking costs about three times a linear read.
_WINDOW_S = 1.0
_WINDOW_STRIDE = 3

# Why a sample yielded nothing. The pass is one-shot per track — it stamps
# face_native_at whether it rescued anything or not — so a track that came out
# no more identifiable than it went in has to say WHICH of these it was.
# Without that the three cases that need three different answers all look the
# same from outside: the face genuinely was not there (optics, and no code
# fixes it), the footage had already rotated off the disk (retention), or the
# read itself failed (a defect). Measured 2026-09-09: patio re-read 114 person
# tracks in seven days and made five of them identifiable, and the other 109
# said nothing at all.
_NO_FILE = "segment file is gone"
_NO_OPEN = "segment would not open"
_NO_TIMEBASE = "segment reports no usable fps or size"
_NO_FRAME = "the frame would not read at that offset"
_NO_CROP = "the person box lands outside the frame"
_NO_FACE = "no face anywhere in the window around the sample"
_NO_GAIN = "the recording held no more of the face than the ring did"
_NO_SEGMENT = "no recording covers the sample"


def _scale_of(width: float, height: float, cap_edge: int | None) -> float:
    """How much bigger the segment is than the frame the boxes were drawn on."""
    long_edge = max(width, height)
    if not cap_edge or cap_edge <= 0 or long_edge <= cap_edge:
        return 1.0
    return long_edge / float(cap_edge)


def _crop_person(
    frame: np.ndarray, bbox: Any, scale: float
) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (float(v) * scale for v in bbox)
    pw, ph = (x2 - x1) * _PAD, (y2 - y1) * _PAD
    cx1, cy1 = max(0, int(x1 - pw)), max(0, int(y1 - ph))
    cx2, cy2 = min(w, int(x2 + pw)), min(h, int(y2 + ph))
    if cx2 - cx1 < 16 or cy2 - cy1 < 16:
        return None
    return frame[cy1:cy2, cx1:cx2]


class NativeFaceReader:
    """Re-reads under-sized faces from the recordings that hold them."""

    def __init__(self, pool: Any, media_root: Path, face_stack: Any,
                 face_model_key: str, face_crops_dir: Path) -> None:
        self._pool = pool
        self._media_root = media_root
        self._face = face_stack
        self._model_key = face_model_key
        self._crops_dir = face_crops_dir

    async def _pending(self) -> list[Any]:
        """Person tracks that had a face and could not use it.

        No time bound: one whose footage is gone finds nothing, is marked as
        asked, and is never looked at again — so a window here would only be a
        second way of saying what the recordings already say. Newest first, so
        the backlog never delays what just happened.
        """
        return await self._pool.fetch(
            f"""
            SELECT t.id, c.slug, c.downscale_max_edge, t.face_px
            FROM tracks t JOIN cameras c ON c.id = t.camera_id
            WHERE t.class_id = 0
              AND t.face_native_at IS NULL
              AND t.face_embedding IS NOT NULL
              AND t.face_px < $1
            ORDER BY t.ended_at DESC
            LIMIT {_TRACKS_PER_PASS}
            """,  # noqa: S608
            _FACE_ID_MIN_PX,
        )

    async def _samples(self, track_id: Any) -> list[Any]:
        return await self._pool.fetch(
            f"""
            SELECT s.id, s.captured_at, s.bbox, s.face_px,
                   r.path, r.started_at
            FROM track_embedding_samples s
            JOIN recordings r ON r.camera_id = s.camera_id
                             AND r.started_at <= s.captured_at
                             AND {covers_until_sql("r.started_at", "r.ended_at")} > s.captured_at
            WHERE s.track_id = $1 AND s.face_embedding IS NOT NULL
            ORDER BY CASE WHEN s.face_realigned THEN 0
                          WHEN s.face_realigned IS NULL THEN 1
                          ELSE 2 END ASC,
                     COALESCE(s.face_frontality, 0) > $3 ASC,
                     s.face_score DESC NULLS LAST
            LIMIT $2
            """,  # noqa: S608
            track_id, _SAMPLES_PER_TRACK, FRONTALITY_MAX,
        )

    def _read_face(
        self, path: Path, seg_start: datetime, at: datetime, bbox: Any,
        cap_edge: int | None,
    ) -> tuple[np.ndarray, np.ndarray, float, float, float | None, bool] | str:
        """The best face in a short window around the sample's own instant —
        (vector, aligned crop, face px, detector score, frontality, whether it
        survives its own alignment) — or the reason there is none."""
        if not path.exists():
            return _NO_FILE
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return _NO_OPEN
        best: tuple[np.ndarray, float, Any] | None = None
        saw_crop = read_any = False
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0
            height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0
            if fps <= 0 or width <= 0:
                return _NO_TIMEBASE
            centre = round((at - seg_start).total_seconds() * fps)
            if centre < 0:
                return _NO_FRAME
            span = int(_WINDOW_S * fps)
            first = max(0, centre - span)
            cap.set(cv2.CAP_PROP_POS_FRAMES, first)
            scale = _scale_of(width, height, cap_edge)
            for n in range(first, centre + span + 1):
                ok, frame = cap.read()
                if not ok:
                    break
                read_any = True
                if (n - first) % _WINDOW_STRIDE:
                    continue
                crop = _crop_person(frame, bbox, scale)
                if crop is None:
                    continue
                saw_crop = True
                rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                det = self._face.detector.detect(rgb)
                if det is None:
                    continue
                x1, y1, x2, y2 = det.bbox
                px = float(min(x2 - x1, y2 - y1))
                if best is None or px > best[1]:
                    best = (rgb, px, det)
        finally:
            cap.release()
        if not read_any:
            return _NO_FRAME
        if best is None:
            return _NO_FACE if saw_crop else _NO_CROP
        rgb, px, det = best
        aligned = align_face(rgb, det.landmarks)
        return (self._face.embedder.embed(aligned), aligned, px, float(det.score),
                frontality_residual(det.landmarks),
                self._face.detector.detect(aligned) is not None)

    async def _reread(self, track: Any) -> tuple[float | None, list[str]]:
        """Best face px this track can produce from its recordings, and — when
        that is nothing — what stood in the way of each sample."""
        best_px = None
        why: list[str] = []
        samples = await self._samples(track["id"])
        if not samples:
            why.append(_NO_SEGMENT)
        for s in samples:
            got = await asyncio.to_thread(
                self._read_face,
                self._media_root / s["path"], s["started_at"], s["captured_at"],
                s["bbox"], track["downscale_max_edge"],
            )
            if isinstance(got, str):
                why.append(got)
                continue
            vec, aligned, px, score, frontality, realigned = got
            if px <= (s["face_px"] or 0.0):
                why.append(_NO_GAIN)
                continue
            rel = MediaLayout.rel(FACE_CROPS, f"{s['id']}.jpg")
            with contextlib.suppress(Exception):
                cv2.imwrite(
                    str(self._crops_dir / f"{s['id']}.jpg"),
                    cv2.cvtColor(aligned, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 90],
                )
            await self._pool.execute(
                """
                UPDATE track_embedding_samples
                   SET face_embedding = $2::vector, face_px = $3,
                       face_score = $4, face_crop_path = $5,
                       face_embedding_model = $6, face_frontality = $7,
                       face_realigned = $8
                 WHERE id = $1
                """,
                s["id"], _vec(vec), px, score, rel, self._model_key, frontality,
                realigned,
            )
            best_px = px if best_px is None else max(best_px, px)
        return best_px, why

    async def _tick(self) -> None:
        for track in await self._pending():
            try:
                best_px, why = await self._reread(track)
            except Exception:
                log.exception("native face re-read failed on track %s", track["id"])
                continue
            # Marked either way: a track whose recording is gone, or whose face
            # is no bigger there, must not be tried on every pass forever.
            await self._pool.execute(
                "UPDATE tracks SET face_native_at = now() WHERE id = $1",
                track["id"],
            )
            if best_px is None:
                # The one line that separates optics from retention from a
                # defect. It is also the only trace this track leaves: it is
                # now stamped, and will never be asked again.
                log.info(
                    "%s: face re-read rescued nothing — %s; the ring had %.0f px",
                    track["slug"], "; ".join(dict.fromkeys(why)),
                    track["face_px"] or 0.0,
                )
                continue
            # The canonical face follows the best sample, so the identity
            # decision sees what the recording actually held.
            await set_canonical_face(self._pool, track["id"], self._model_key)
            log.info(
                "%s: face re-read from the recording — %.0f px against %.0f "
                "from the ring%s", track["slug"], best_px, track["face_px"] or 0.0,
                ", now identifiable" if best_px >= _FACE_ID_MIN_PX else "",
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("native face pass failed — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_INTERVAL_S)


def _vec(v: np.ndarray) -> str:
    from baba_core.pgvector import vector_literal

    return vector_literal(v)
