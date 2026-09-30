"""go2rtc carries exactly the streams a camera exposes, one source per name.

A name with a second source is a second stream wearing the first one's name:
go2rtc falls back to it when the first stalls and stays there, which is how
west recorded nine hours of its substream on 04.09. And a source swapped under
a name never reaches whoever is already reading it — go2rtc's PUT leaves them
on the old stream object — so the readers are told, after the swap.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from baba_api.go2rtc_sync import Go2RtcSync
from baba_api.models import CameraIn
from pydantic import ValidationError

MAIN = "rtsp://admin:pw@192.168.10.13:554/Preview_01_main"
SUB = "rtsp://admin:pw@192.168.10.13:554/Preview_01_sub"
NEW = "rtsp://admin:rotated@192.168.10.13:554/Preview_01_main"


class _Response:
    def __init__(self, body=None) -> None:
        self._body = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._body


class _Go2rtc:
    def __init__(self, streams: dict[str, list[str]], *, masked: bool = False) -> None:
        self.streams = streams
        self.masked = masked
        self.log: list[tuple[str, str]] = []

    async def get(self, url, **_):
        return _Response({
            name: {"producers": [{"url": "rtsp://***" if self.masked else s} for s in srcs]}
            for name, srcs in self.streams.items()
        })

    async def put(self, url, params):
        name = dict(params)["name"]
        self.streams[name] = [v for k, v in params if k == "src"]
        self.log.append(("put", name))
        return _Response()

    async def delete(self, url, params):
        self.streams.pop(params["src"])
        return _Response()


class _Pool:
    def __init__(self, go2rtc: _Go2rtc) -> None:
        self.cams: list[dict] = []
        self._go2rtc = go2rtc

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, sql, *args):
        return self.cams

    async def execute(self, sql, *args):
        assert "go2rtc_source_changed" in sql
        self._go2rtc.log.append(("notify", args[0]))


def _sync(go2rtc: _Go2rtc) -> tuple[Go2RtcSync, _Pool]:
    sync = Go2RtcSync("postgresql://unused", "http://go2rtc:1984", ("baba", "test"))
    sync._client = go2rtc
    sync._pool = pool = _Pool(go2rtc)
    sync._rtsp_auth_ok = True
    return sync, pool


def test_a_camera_is_exactly_the_streams_it_exposes():
    go2rtc = _Go2rtc({})
    sync, pool = _sync(go2rtc)

    pool.cams = [{"slug": "west", "stream_url": MAIN, "substream_url": None}]
    asyncio.run(sync._reconcile())
    assert go2rtc.streams == {"west": [MAIN]}

    pool.cams = [{"slug": "west", "stream_url": MAIN, "substream_url": SUB}]
    asyncio.run(sync._reconcile())
    assert go2rtc.streams == {"west": [MAIN], "west_sub": [SUB]}

    pool.cams = [{"slug": "west", "stream_url": MAIN, "substream_url": None}]
    asyncio.run(sync._reconcile())
    assert go2rtc.streams == {"west": [MAIN]}


@pytest.mark.parametrize("masked", [False, True])
def test_a_swapped_source_tells_its_readers_after_the_swap(masked):
    go2rtc = _Go2rtc({}, masked=masked)
    sync, pool = _sync(go2rtc)
    pool.cams = [{"slug": "west", "stream_url": MAIN, "substream_url": None}]
    asyncio.run(sync._reconcile())
    asyncio.run(sync._reconcile())
    assert ("notify", "west") not in go2rtc.log, "nothing moved"

    go2rtc.log.clear()
    pool.cams = [{"slug": "west", "stream_url": NEW, "substream_url": SUB}]
    asyncio.run(sync._reconcile())
    assert go2rtc.log.index(("put", "west")) < go2rtc.log.index(("notify", "west"))
    assert ("notify", "west_sub") not in go2rtc.log, "a new name has no reader yet"


def test_the_substream_is_analysed_only_when_there_is_one():
    with pytest.raises(ValidationError, match="needs a substream_url"):
        CameraIn(slug="west", name="West", stream_url=MAIN, analysis_stream="sub")
    assert CameraIn(slug="west", name="West", stream_url=MAIN, substream_url=SUB,
                    analysis_stream="sub").analysis_stream == "sub"


@pytest.mark.parametrize(
    ("fields", "err"),
    [
        ({"idle_fps": 8}, "idle_fps must be below target_fps"),
        ({"target_fps": 2}, "idle_fps must be below target_fps"),
        ({"analysis_stream": "sub"}, None),
        ({"substream_url": None}, "analysis_stream 'sub' needs a substream_url"),
        ({"name": "West"}, None),
    ],
)
def test_a_patch_is_checked_on_the_pair_it_leaves_behind(fields, err):
    """A patch that sends one half of a guarded pair is judged together with
    the half the camera already has."""
    from baba_api.models import adaptive_rate_error, analysis_stream_error
    from baba_api.routes import _reject_pair
    from fastapi import HTTPException

    current = {"idle_fps": 2, "target_fps": 8, "analysis_stream": "sub", "substream_url": SUB}
    try:
        _reject_pair(fields, current, "idle_fps", "target_fps", adaptive_rate_error)
        _reject_pair(fields, current, "analysis_stream", "substream_url", analysis_stream_error)
    except HTTPException as e:
        assert (e.status_code, e.detail) == (422, err)
    else:
        assert err is None
