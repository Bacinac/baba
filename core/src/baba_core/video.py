"""Video decoder plugin protocol.

Mirrors the InferenceBackend pattern: an abstract base class plus a separate
plugin registry, so swapping CPU PyAV for NVDEC (NVIDIA) or QSV (Intel Arc)
is a packaging concern, not an ingestor code change.

Decoder packages live under `backends/decoder-*/` and register themselves via:

    [project.entry-points."baba.decoders"]
    software = "baba_decoder_software:SoftwareDecoder"

The ingestor asks the registry for exactly one decoder: the one its variant
runs, or the one BABA_DECODER_BACKEND names. The decoder reports its
capabilities (whether it can downscale in the pipeline, which pixel format it
writes, etc.) and the ingestor consults those to decide which knobs to pass.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True, frozen=True)
class DecoderCapabilities:
    """What a video decoder backend can do. The ingestor queries this to know
    what work is still left to it (e.g. CPU downscale if the backend can't
    downscale in-pipeline) and to check the variant's ring format."""

    name: str
    # True if decode runs on dedicated silicon (NVDEC, QSV, VAAPI, AMF),
    # False for pure CPU. Used for scheduling decisions and logging.
    hardware_accelerated: bool
    # Pixel format the decoder emits to the SHM ring / NATS publisher.
    #   "rgb"  — (H, W, 3) uint8 RGB (the cpu variant's software decoder)
    #   "nv12" — (H + H/2, W) uint8 flat NV12 (Y plane then interleaved UV);
    #            shifts the YUV→RGB step off the CPU into either the model
    #            ONNX graph (preferred — see tools/bake_preproc.py
    #            --input-format nv12) or a downstream cv2 conversion on the
    #            consumer side. Consumers query this attribute to pick the
    #            right preprocess path; FrameRing carries it forward to
    #            readers via the slot meta.
    output_pixel_format: str = "rgb"


@dataclass(slots=True, frozen=True)
class DecoderConfig:
    """Per-stream config passed to `open()`. Stable, codec-agnostic."""

    url: str
    target_fps: int
    # If > 0, instruct the decoder to deliver frames whose long edge does
    # not exceed this size. Caller may still post-process if the backend
    max_edge: int = 0
    # Connection-level options the decoder may honour where it makes sense.
    # E.g. {"rtsp_transport": "tcp", "stimeout": "5000000"}.
    options: dict[str, str] = field(default_factory=dict)


class VideoDecoder(ABC):
    """Abstract base for video decoder plugins.

    Lifecycle: `is_available()` (classmethod, no I/O) → instantiate →
    `open(config)` → iterate `frames(stop_event)` → `close()`. Implementations
    must be safe to call `close()` from a different task than the one that
    drives `frames()` — the ingestor stops decoders that way.
    """

    capabilities: DecoderCapabilities

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Probe the host: required libs importable, drivers reachable.
        Must NOT raise on missing hardware; return False instead."""

    @abstractmethod
    async def open(self, config: DecoderConfig) -> None:
        """Open the source URL. May block briefly (RTSP DESCRIBE)."""

    @abstractmethod
    def frames(self, stop: asyncio.Event) -> AsyncIterator[tuple[np.ndarray, int]]:
        """Yield (rgb_frame, pts_ns) tuples until `stop` is set or the source
        ends. The iterator honours target_fps either at the pipeline level
        (if capabilities say so) or by skipping frames in Python.

        pts_ns is the source-side monotonic timestamp (PyAV `frame.time`
        converted to ns). Zero is acceptable when the source provides no PTS
        (rare; some MJPEG-over-HTTP cameras)."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down ffmpeg/decoder handles. Idempotent."""
