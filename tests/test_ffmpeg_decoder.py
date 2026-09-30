"""A reader that joins a live stream mid-GOP sees missing-reference errors
until the next keyframe. That is the join, not a fault; the same line after the
first frame is frames being lost, and stays loud."""

import asyncio
import logging

import pytest
from baba_core.ffmpeg_decoder import FFmpegSubprocessDecoder

JOINS = [
    b"[hevc @ 0x1] Error constructing the frame RPS.\n",
    b"[vist#0:0/h264 @ 0x1] [dec:h264 @ 0x2] Decoding error: Invalid argument\n",
]


class _Decoder(FFmpegSubprocessDecoder):
    @classmethod
    def is_available(cls) -> bool:
        return True

    def _decoder_for_codec(self, codec):
        return codec

    def _build_ffmpeg_args(self, **_):
        return []


class _Proc:
    def __init__(self, data: bytes) -> None:
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(data)
        self.stderr.feed_eof()


def _levels(caplog, join: bytes, decoding: bool) -> list[int]:
    async def drain():
        decoder = _Decoder()
        decoder._proc = _Proc(join + b"[rtsp @ 0x2] method DESCRIBE failed: 404 Not Found error\n")
        decoder._decoding = decoding
        await decoder._drain_stderr()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="baba_core.ffmpeg_decoder"):
        asyncio.run(drain())
    return [r.levelno for r in caplog.records]


@pytest.mark.parametrize("join", JOINS)
def test_a_missing_reference_is_the_join_until_the_first_frame(caplog, join):
    assert _levels(caplog, join, decoding=False) == [logging.DEBUG, logging.WARNING]


@pytest.mark.parametrize("join", JOINS)
def test_after_the_first_frame_a_missing_reference_is_frames_lost(caplog, join):
    assert _levels(caplog, join, decoding=True) == [logging.WARNING, logging.WARNING]
