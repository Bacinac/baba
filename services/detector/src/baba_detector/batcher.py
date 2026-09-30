from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from baba_core import StatsCollector

    from baba_detector.config import DetectorConfig

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingFrame:
    camera_id: str
    sequence: int
    timestamp_ns: int
    pixels: np.ndarray
    enqueued_ns: int
    pts_ns: int = 0
    # Logical picture dims. For RGB pixels these match pixels.shape[:2]; for
    # NV12 the pixels buffer is taller (H + H/2) so we carry the original
    # H, W explicitly to keep the postprocessor's letterbox-undo math from
    # mistaking the chroma rows for picture rows.
    pixel_format: str = "rgb"
    width: int = 0
    height: int = 0


class FrameBatcher:
    """Coalesces incoming frames from N cameras into batches for inference.

    Keep-LATEST-per-camera, not a FIFO: each camera owns one slot holding its
    newest pending frame; a fresh frame replaces an unconsumed predecessor
    (counted as `frames_coalesced`). On a GPU that keeps up this behaves like
    the old FIFO — slots never collide. On a GPU that can't keep up (weak
    iGPU, contention with a co-tenant NVR) a FIFO builds a deep backlog of
    stale frames: the detector falls seconds behind the ring, publishes in
    bursts, and the gaps starve the tracker's activity verdicts long enough
    to trip the ingestor's stale-feed full-rate fallback — the adaptive rate
    then oscillates forever. Latest-slot semantics degrade to a lower
    effective fps on FRESH frames instead, and the publish cadence (one
    batch per infer cycle) stays continuous.

    Batch assembly still waits up to max_batch_wait_ms from the oldest
    pending frame so multiple cameras coalesce into one GPU pass.
    """

    def __init__(self, config: DetectorConfig, *, stats: StatsCollector | None = None) -> None:
        self._max_batch = config.max_batch_size
        self._max_wait_ns = config.max_batch_wait_ms * 1_000_000
        self._latest: dict[str, PendingFrame] = {}
        self._wakeup = asyncio.Event()
        self._closed = False
        self._stats = stats

    @property
    def pending(self) -> int:
        """Cameras with a frame waiting for the next batch."""
        return len(self._latest)

    async def submit(self, frame: PendingFrame) -> None:
        if self._closed:
            return
        if frame.camera_id in self._latest and self._stats is not None:
            # Predecessor never reached the GPU — superseded by this frame.
            self._stats.incr("frames_coalesced")
        self._latest[frame.camera_id] = frame
        self._wakeup.set()

    async def next_batch(self) -> list[PendingFrame]:
        """Block until at least one camera has a frame, then collect up to
        max_batch_size cameras or until max_wait_ms has elapsed since the
        oldest pending frame. Returns [] only after close()."""
        while not self._closed:
            await self._wakeup.wait()
            if self._closed:
                break
            if not self._latest:
                self._wakeup.clear()
                continue
            deadline_ns = min(pf.enqueued_ns for pf in self._latest.values()) + self._max_wait_ns
            while len(self._latest) < self._max_batch:
                remaining_ns = deadline_ns - time.time_ns()
                if remaining_ns <= 0:
                    break
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=remaining_ns / 1e9)
                except TimeoutError:
                    break
            cams = sorted(self._latest, key=lambda c: self._latest[c].enqueued_ns)
            batch = [self._latest.pop(c) for c in cams[: self._max_batch]]
            if self._latest:
                self._wakeup.set()
            else:
                self._wakeup.clear()
            return batch
        return []

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()
