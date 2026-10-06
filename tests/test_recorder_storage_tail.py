import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from baba_recorder.config import CameraSpec, RecorderConfig
from baba_recorder.worker import CameraRecorder


class _Probe:
    def __init__(self, result):
        self.result = result
        self.returncode = None
        self.killed = False

    async def communicate(self):
        if self.result == "timeout":
            raise TimeoutError
        self.returncode = 1
        return b"", self.result.encode()

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = -9


def _worker(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / now.strftime("%Y-%m-%d_%H-%M-%S.mp4")
    path.write_bytes(b"possible-valid-data")
    deleted = []

    async def execute(_sql, relative):
        deleted.append(relative)

    worker = CameraRecorder(
        CameraSpec(id="0", slug="fault", stream_url="rtsp://unused"),
        RecorderConfig(dsn="", media_path=tmp_path, segment_seconds=2, rtsp_timeout_us=1,
                       poll_seconds=1, retention_check_seconds=1, go2rtc_rtsp_base="rtsp://unused"),
        SimpleNamespace(execute=execute),
    )
    worker._session_started_at = now
    return worker, path, deleted


@pytest.mark.parametrize("failure", ["missing", "timeout", "Unknown option"])
def test_validation_failure_preserves_potentially_valid_media(tmp_path, monkeypatch, failure):
    worker, path, deleted = _worker(tmp_path)
    probe = _Probe(failure)

    async def start(*_args, **_kwargs):
        if failure == "missing":
            raise FileNotFoundError("ffprobe")
        return probe

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    error = FileNotFoundError if failure == "missing" else TimeoutError if failure == "timeout" else RuntimeError
    with pytest.raises(error):
        asyncio.run(worker._discard_failed_tail(tmp_path))
    assert path.read_bytes() == b"possible-valid-data"
    assert not deleted
    assert probe.killed == (failure == "timeout")


def test_confirmed_unreadable_tail_removes_the_file_and_its_index(tmp_path, monkeypatch):
    worker, path, deleted = _worker(tmp_path)

    async def start(*_args, **_kwargs):
        return _Probe("moov atom not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    asyncio.run(worker._discard_failed_tail(tmp_path))
    assert not path.exists()
    assert deleted == [f"segments/fault/{path.name}"]
