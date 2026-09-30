"""Rolling trace of RAW detections, per camera — the evidence an incident
replay is built from.

The detector already holds, on every frame, the full set of detections the
model produced before any threshold is applied. It then throws most of them
away: that is the gate's job. But those discarded detections are exactly what
answers the only question worth asking about a proposed threshold change —
"who would I have caught, and what garbage would I have let in?"

So we keep them, briefly, in memory. A ring per camera covering the last
`window_s` seconds. When the watcher opens an incident, the API asks for the
slice covering it and re-runs the gate over that slice twice: once with the
thresholds in force, once with the proposed ones. No second inference pass, no
decoding the recording again, and — the part that matters — no risk of the
replay disagreeing with production. These are the same detections, from the
same model, on the same pixels.

Deliberately NOT persisted. It is a flight recorder: cheap, bounded, and gone
on restart. An incident whose trace was lost simply reports that, rather than
inventing an approximation from re-decoded video (which would land in a
different colour space and produce different scores — a comparison that looks
authoritative and isn't).
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# How far back the ring reaches. The watcher aggregates a 15-minute window and
# runs every 5, so an incident can be opened up to ~20 min after the behaviour
# that tripped it — the ring has to outlive that or the trace is already gone
# by the time anyone asks.
DEFAULT_WINDOW_S = float(os.environ.get("BABA_DETECTOR_TRACE_WINDOW_S", "1800"))
# Per-frame cap. A camera mid-phantom-storm can emit dozens of low-confidence
# boxes; keeping the strongest N bounds memory without losing the ones any
# plausible threshold would admit.
MAX_DETECTIONS_PER_FRAME = int(os.environ.get("BABA_DETECTOR_TRACE_MAX_DETS", "32"))


@dataclass(slots=True)
class TracedFrame:
    """One frame's raw detections, pre-gate."""

    timestamp_ns: int
    sequence: int
    width: int
    height: int
    # (class_name, confidence, x1, y1, x2, y2) in ORIGINAL frame pixels — the
    # same coordinate space the gate's size check uses, so the replay needs no
    # rescaling to reproduce it.
    detections: list[tuple[str, float, float, float, float, float]]


class RawDetectionTrace:
    """Bounded per-camera ring of pre-gate detections."""

    def __init__(
        self,
        window_s: float = DEFAULT_WINDOW_S,
        max_per_frame: int = MAX_DETECTIONS_PER_FRAME,
    ) -> None:
        self._window_ns = int(window_s * 1_000_000_000)
        self._max_per_frame = max_per_frame
        self._by_camera: dict[str, deque[TracedFrame]] = {}

    def record(
        self,
        camera_slug: str,
        *,
        sequence: int,
        timestamp_ns: int,
        width: int,
        height: int,
        detections: Iterable[Any],
    ) -> None:
        """Bank one frame. Called on the hot path — keep it to a list build and
        a couple of deque ops."""
        rows = [
            (
                d.class_name,
                float(d.confidence),
                d.bbox.x1,
                d.bbox.y1,
                d.bbox.x2,
                d.bbox.y2,
            )
            for d in detections
        ]
        if len(rows) > self._max_per_frame:
            rows.sort(key=lambda r: r[1], reverse=True)
            del rows[self._max_per_frame :]
        ring = self._by_camera.get(camera_slug)
        if ring is None:
            ring = self._by_camera[camera_slug] = deque()
        ring.append(
            TracedFrame(
                timestamp_ns=timestamp_ns,
                sequence=sequence,
                width=width,
                height=height,
                detections=rows,
            )
        )
        cutoff = timestamp_ns - self._window_ns
        while ring and ring[0].timestamp_ns < cutoff:
            ring.popleft()

    def slice(
        self, camera_slug: str, start_ns: int, end_ns: int, max_frames: int
    ) -> list[TracedFrame]:
        """Frames inside [start_ns, end_ns], thinned to at most `max_frames` by
        even decimation so the sample still spans the whole window rather than
        stopping partway through it."""
        ring = self._by_camera.get(camera_slug)
        if not ring:
            return []
        hits = [f for f in ring if start_ns <= f.timestamp_ns <= end_ns]
        if max_frames > 0 and len(hits) > max_frames:
            step = len(hits) / max_frames
            hits = [hits[int(i * step)] for i in range(max_frames)]
        return hits

    def coverage(self, camera_slug: str) -> tuple[int, int] | None:
        """(oldest_ns, newest_ns) currently retained, or None if empty. Lets a
        caller say "the trace does not reach that far back" instead of silently
        returning an empty comparison."""
        ring = self._by_camera.get(camera_slug)
        if not ring:
            return None
        return ring[0].timestamp_ns, ring[-1].timestamp_ns
