"""Recorder health: which cameras count as no longer recording."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import asyncpg
from baba_recorder.config import RecorderConfig
from baba_recorder.supervisor import RecorderSupervisor


def _health(pg: str, tmp_path, *, segment_ago: timedelta, up_s: float):
    async def main():
        sup = RecorderSupervisor(RecorderConfig(
            dsn=pg, media_path=tmp_path, segment_seconds=60, rtsp_timeout_us=1,
            poll_seconds=1, retention_check_seconds=1, go2rtc_rtsp_base="rtsp://go2rtc:8554",
        ))
        sup._started -= up_s
        sup._pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            cam = await sup._pool.fetchval(
                "INSERT INTO cameras (name, slug, stream_url) VALUES ('yard', 'yard', 'rtsp://go2rtc/x') RETURNING id"
            )
            await sup._pool.execute(
                "INSERT INTO recordings (camera_id, started_at, ended_at, path)"
                " VALUES ($1, now() - $2::interval, now() - $2::interval + interval '1 minute', 'segments/yard/a.mp4')",
                cam, segment_ago,
            )
            return await sup.recording_health()
        finally:
            await sup._pool.close()

    return asyncio.run(main())


def test_a_restart_longer_than_the_window_is_not_a_dead_camera(pg, tmp_path):
    assert _health(pg, tmp_path, segment_ago=timedelta(minutes=4), up_s=30) == (True, [])


def test_a_camera_silent_for_the_whole_window_since_start_is_stale(pg, tmp_path):
    assert _health(pg, tmp_path, segment_ago=timedelta(minutes=4), up_s=200) == (False, ["yard"])


def test_a_camera_with_a_fresh_segment_is_healthy(pg, tmp_path):
    assert _health(pg, tmp_path, segment_ago=timedelta(seconds=30), up_s=200) == (True, [])
