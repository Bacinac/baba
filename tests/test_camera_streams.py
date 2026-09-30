"""Which stream is analysed, and what the recorder writes down about the one it
records.

A camera exposes its own stream, its substream, or both. The ingestor reads the
one `analysis_stream` names; the recorder records `stream_url` and notes the
size it actually carries — smaller included, since a camera that exposes only
its substream records that, and the plate reader maps zones onto recordings by
this size.
"""

import asyncio
from pathlib import Path

import asyncpg
import pytest
from baba_ingestor.supervisor import _load_cameras
from baba_recorder.config import CameraSpec, RecorderConfig
from baba_recorder.worker import CameraRecorder

BASE = "rtsp://go2rtc:8554"


def _run(pg: str, body) -> None:
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            await body(pool)
        finally:
            await pool.close()

    asyncio.run(main())


async def _camera(conn, slug: str, *, sub: str | None = None, analysis: str = "main"):
    return await conn.fetchval(
        """
        INSERT INTO cameras (name, slug, stream_url, substream_url, analysis_stream)
        VALUES ($1, $1, 'rtsp://cam/main', $2, $3) RETURNING id::text
        """,
        slug, sub, analysis,
    )


def test_the_ingestor_reads_the_stream_the_camera_is_analysed_on(pg):
    async def body(pool):
        async with pool.acquire() as conn:
            await _camera(conn, "west", sub="rtsp://cam/sub", analysis="sub")
            await _camera(conn, "door", sub="rtsp://cam/sub")
            await _camera(conn, "patio")
            specs = {s.slug: s.stream_url for s in await _load_cameras(conn, BASE)}
        assert specs == {"west": f"{BASE}/west_sub", "door": f"{BASE}/door", "patio": f"{BASE}/patio"}

    _run(pg, body)


def test_no_substream_nothing_to_analyse_on_it(pg):
    async def body(pool):
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _camera(conn, "west", analysis="sub")
            cam = await _camera(conn, "door", sub="rtsp://cam/sub", analysis="sub")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE cameras SET substream_url = NULL WHERE id = $1::uuid", cam)

    _run(pg, body)


def test_the_recorder_writes_down_a_smaller_stream_instead_of_refusing_it(pg):
    async def body(pool):
        async with pool.acquire() as conn:
            cam = await _camera(conn, "west")
            await conn.execute(
                "UPDATE cameras SET stream_width = 4096, stream_height = 1152 WHERE id = $1::uuid", cam
            )
        recorder = CameraRecorder(
            CameraSpec(id=cam, slug="west", stream_url=f"{BASE}/west"),
            RecorderConfig(dsn="", media_path=Path("/nonexistent"), segment_seconds=60,
                           rtsp_timeout_us=1, poll_seconds=1, retention_check_seconds=1,
                           go2rtc_rtsp_base=BASE),
            pool=pool,
        )
        recorder._probed_size = (1536, 432)
        await recorder._note_size()
        row = await pool.fetchrow("SELECT stream_width, stream_height FROM cameras WHERE id = $1::uuid", cam)
        assert (row["stream_width"], row["stream_height"]) == (1536, 432)

    _run(pg, body)
