"""What the recorder repeats from ffmpeg, and how loudly.

Stamping frames with the server's clock gives the frames of one TCP read the
same tick, and the muxer moves each one past the last and says so — 135 times
in half an hour on production, burying the line that does matter: a timestamp
running backwards. The next frame of the burst is then behind the tick the
muxer made up, which is the same burst, not a clock going back.
"""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from baba_recorder.config import CameraSpec, RecorderConfig
from baba_recorder.worker import CameraRecorder


def _dts(previous: int, current: int) -> str:
    return (f"[segment @ 0x5e0] Non-monotonic DTS in output stream 0:0; previous: {previous}, "
            f"current: {current}; changing to {previous + 1}. This may result in incorrect timestamps")


SAME_TICK = _dts(931687, 931687)
BURST = [_dts(931688, 931687), _dts(931689, 931688), _dts(931690, 931690)]
STEP_BACK = _dts(931691, 900000)
BACKWARDS = _dts(931800, 931600)
PROBE = ("[rtsp @ 0x5e0] DTS discontinuity in stream 0: packet 3 with DTS 161123158764567, "
         "packet 4 with DTS 161123158771001")
UNSET = "[segment @ 0x5e0] Timestamps are unset in a packet for stream 0."
OTHER = "[hevc @ 0x5e0] Error constructing the frame RPS."


def _levels(caplog, *lines: str) -> dict[str, int]:
    async def main():
        stderr = asyncio.StreamReader()
        stderr.feed_data("".join(f"{line}\n" for line in lines).encode())
        stderr.feed_eof()
        worker = CameraRecorder(
            CameraSpec(id="0", slug="west", stream_url="rtsp://go2rtc/west"),
            RecorderConfig(dsn="", media_path=Path("/nonexistent"), segment_seconds=60,
                           rtsp_timeout_us=1, poll_seconds=1, retention_check_seconds=1,
                           go2rtc_rtsp_base="rtsp://go2rtc:8554"),
            pool=None,
        )
        await worker._forward_stderr(SimpleNamespace(stderr=stderr))

    with caplog.at_level(logging.DEBUG, logger="baba_recorder.worker"):
        asyncio.run(main())
    return {r.getMessage(): r.levelno for r in caplog.records}


def test_a_burst_the_muxer_spreads_is_debug_and_everything_else_stays_a_warning(caplog):
    levels = _levels(caplog, PROBE, SAME_TICK, *BURST, STEP_BACK, BACKWARDS, UNSET, OTHER)
    assert levels == {
        f"[ffmpeg west] {PROBE}": logging.DEBUG,
        f"[ffmpeg west] {SAME_TICK}": logging.DEBUG,
        **{f"[ffmpeg west] {line}": logging.DEBUG for line in BURST},
        f"[ffmpeg west] {STEP_BACK}": logging.WARNING,
        f"[ffmpeg west] {BACKWARDS}": logging.WARNING,
        f"[ffmpeg west] {UNSET}": logging.WARNING,
        f"[ffmpeg west] {OTHER}": logging.WARNING,
    }
