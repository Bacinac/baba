from __future__ import annotations

import os
from dataclasses import dataclass

from baba_core import build_variant, dsn_from_env

# The decoder each variant runs unless the host names another in
# BABA_DECODER_BACKEND. That override exists for Intel, where the right decoder
# is a per-host fact rather than a per-variant one: the N305 iGPU's VAAPI path
# dies every ~60 s (vaSyncSurface) while QSV on the same driver is stable, and
# the Arc runs VAAPI cleanly for days.
_DEFAULT_DECODER = {"nvidia": "ffmpeg-nvdec-nv12", "intel": "vaapi-nv12", "cpu": "software"}


@dataclass(slots=True, frozen=True)
class SupervisorConfig:
    """Process-wide config for the ingestor supervisor.

    Per-camera knobs (rtsp_url, target_fps, etc.) live in Postgres now; the
    supervisor reads them from the `cameras` table at startup and on every
    `cameras_changed` NOTIFY.
    """

    nats_url: str
    dsn: str
    go2rtc_rtsp_base: str
    variant: str
    # The one VideoDecoder plugin every camera worker runs. Unavailable means
    # the ingestor does not start: the only thing to fall back to is decoding
    # on the CPU, which pegs the host and collapses the stack (2026-06-04).
    decoder_backend: str

    @classmethod
    def from_env(cls) -> SupervisorConfig:
        variant = build_variant()
        return cls(
            nats_url=os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
            dsn=dsn_from_env(),
            go2rtc_rtsp_base=os.environ.get("BABA_GO2RTC_RTSP", "rtsp://go2rtc:8554"),
            variant=variant,
            decoder_backend=os.environ.get("BABA_DECODER_BACKEND", "").strip()
            or _DEFAULT_DECODER[variant],
        )


@dataclass(slots=True, frozen=True)
class CameraSpec:
    """A camera as the supervisor needs to know it. Mirrors a `cameras` row.

    `stream_url` is the go2rtc path this worker decodes: `{base}/{slug}_sub`
    when the camera's `analysis_stream` is its substream, `{base}/{slug}`
    otherwise. One decode feeds detector, tracker and (via the SHM ring) the
    embedder. Recording is not this worker's: the recorder muxes `{slug}`.
    """

    id: str  # uuid as string
    slug: str
    stream_url: str
    target_fps: int
    downscale_max_edge: int
    # Adaptive detection rate: when set (1..target_fps-1), the worker decays
    # this camera's published frame rate to idle_fps while the tracker reports
    # no scene activity, and snaps back to target_fps on the first fresh
    # detection. The decoder keeps running at target_fps either way — only
    # SHM writes + NATS frame notifications are decimated, so the expensive
    # consumers (detector inference, ReID, embedder) see fewer frames while
    # full-frame detection continues at the idle cadence. None = off.
    idle_fps: int | None
