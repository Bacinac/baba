from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from baba_core import dsn_from_env


@dataclass(slots=True, frozen=True)
class RecorderConfig:
    dsn: str
    media_path: Path
    # Segment length in seconds. 300s (5 min) keeps the number of files/DB
    # rows manageable (~288/day/cam) while still bounding worst-case data
    # loss on ffmpeg crash and keeping retention granular enough.
    # ffmpeg cuts at the next keyframe after this many seconds AND aligned
    # to clock boundaries (segment_atclocktime), so segments land on
    # predictable wall-clock minutes (00, 05, 10, ...).
    segment_seconds: int
    # Socket I/O timeout (µs) handed to ffmpeg's -timeout (the rtsp demuxer's
    # own option; -rw_timeout is rejected for an rtsp input). A camera whose
    # stream stalls (network drop, go2rtc hiccup) would otherwise leave ffmpeg
    # blocked on a read forever — no error, no segments, no reconnect. With the
    # timeout ffmpeg exits, the run loop reconnects, and the gap shows in the
    # log instead of a silently frozen recorder.
    rtsp_timeout_us: int
    # How often to scan each camera's output directory for new closed segments.
    poll_seconds: float
    # How often to run the retention sweeper.
    retention_check_seconds: float
    # All camera streams are fronted by go2rtc so we get a single uniform
    # RTSP source regardless of what the camera natively speaks (RTSP, FLV,
    # ONVIF, ...). Recording the raw camera URL would mean ffmpeg has to
    # cope with vendor-specific protocols and reconnect quirks; routing
    # through go2rtc moves that complexity to one place.
    go2rtc_rtsp_base: str

    @classmethod
    def from_env(cls) -> RecorderConfig:
        return cls(
            dsn=dsn_from_env(),
            media_path=Path(os.environ.get("BABA_MEDIA_PATH", "/media")),
            segment_seconds=int(os.environ.get("BABA_RECORDER_SEGMENT_SECONDS", "300")),
            rtsp_timeout_us=int(
                float(os.environ.get("BABA_RECORDER_RTSP_TIMEOUT_S", "15")) * 1_000_000
            ),
            poll_seconds=float(os.environ.get("BABA_RECORDER_POLL_SECONDS", "5")),
            retention_check_seconds=float(
                os.environ.get("BABA_RECORDER_RETENTION_CHECK_SECONDS", "300")
            ),
            go2rtc_rtsp_base=os.environ.get("BABA_GO2RTC_RTSP", "rtsp://go2rtc:8554"),
        )


@dataclass(slots=True, frozen=True)
class CameraSpec:
    id: str  # uuid as string
    slug: str
    stream_url: str
