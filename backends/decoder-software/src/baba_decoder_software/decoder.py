"""Software (CPU) video decoder backend.

PyAV-driven; the baseline that ships everywhere and the fallback when no
hardware-accelerated decoder is present or none can handle the source codec.

Decode path:
  RTSP/HTTP source → libav demuxer → libav* SW decoder → numpy RGB frame
  → optional CPU downscale (cv2 INTER_AREA).

Frame rate is gated in Python (we still decode every frame from the wire,
then keep only one per `1/target_fps` second). Hardware backends should
prefer pipeline-level rate limiting where the silicon supports it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import cv2
import numpy as np
from baba_core.video import DecoderCapabilities, DecoderConfig, VideoDecoder

log = logging.getLogger(__name__)


def _downscale(image: np.ndarray, max_edge: int) -> np.ndarray:
    h, w = image.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return image
    scale = max_edge / long_edge
    return cv2.resize(
        image,
        (round(w * scale), round(h * scale)),
        interpolation=cv2.INTER_AREA,
    )


class SoftwareDecoder(VideoDecoder):
    capabilities = DecoderCapabilities(
        name="software",
        hardware_accelerated=False,
    )

    @classmethod
    def is_available(cls) -> bool:
        try:
            import av  # noqa: F401
        except ImportError:
            return False
        return True

    def __init__(self) -> None:
        self._config: DecoderConfig | None = None
        self._container = None

    async def open(self, config: DecoderConfig) -> None:
        import av

        self._config = config
        # Default RTSP knobs — caller may override via config.options.
        options: dict[str, str] = {
            "rtsp_transport": "tcp",
            "stimeout": "5000000",
        }
        options.update(config.options or {})
        self._container = await asyncio.to_thread(av.open, config.url, options=options)

    async def frames(self, stop: asyncio.Event) -> AsyncIterator[tuple[np.ndarray, int]]:
        if self._container is None or self._config is None:
            raise RuntimeError("open() must be called before frames()")
        container = self._container
        target_fps = self._config.target_fps
        max_edge = self._config.max_edge

        frame_interval_ns = int(1e9 / target_fps) if target_fps > 0 else 0
        last_emit_ns = 0

        def _next_frame():
            for frame in container.decode(video=0):
                return frame
            return None

        while not stop.is_set():
            frame = await asyncio.to_thread(_next_frame)
            if frame is None:
                break
            now_ns = int(frame.time * 1e9) if frame.time else 0
            if frame_interval_ns and now_ns - last_emit_ns < frame_interval_ns:
                continue
            last_emit_ns = now_ns
            array = frame.to_ndarray(format="rgb24")
            if max_edge > 0:
                array = _downscale(array, max_edge)
            yield array, now_ns

    async def close(self) -> None:
        if self._container is not None:
            container = self._container
            self._container = None
            await asyncio.to_thread(container.close)
