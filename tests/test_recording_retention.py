"""What the recorder deletes, and above all what it must never delete.

Every rule here is a WHERE clause over recordings, tracks and samples, so the
passes run against a real migrated database; a fake would only agree with
the SQL its author imagined. The media tier is a temp dir and the disk it
sits on is simulated, so a full host disk never reaches the verdict.
"""

import asyncio
import logging
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from baba_recorder import supervisor
from baba_recorder.config import RecorderConfig
from baba_recorder.supervisor import RecorderSupervisor

PERSON, CAR = 0, 2
SEGMENT_BYTES = 10


class _Disk:
    """The media volume as disk_usage reports it: BABA's own files plus
    whatever a co-tenant of the same volume holds."""

    def __init__(self, media: Path, total: int) -> None:
        self.media = media
        self.total = total
        self.other = 0
        self.other_growth = 0

    def __call__(self, _path):
        self.other += self.other_growth
        ours = sum(f.stat().st_size for f in self.media.rglob("*") if f.is_file())
        used = min(self.total, self.other + ours)
        return SimpleNamespace(total=self.total, used=used, free=self.total - used)


@pytest.fixture
def media(tmp_path):
    return tmp_path


@pytest.fixture
def disk(media, monkeypatch):
    d = _Disk(media, total=10**9)
    monkeypatch.setattr(supervisor.shutil, "disk_usage", d)
    return d


class _Scene:
    def __init__(self, conn: asyncpg.Connection, media: Path) -> None:
        self.conn = conn
        self.media = media

    async def settings(self, **kw) -> None:
        cols = ", ".join(f"{k} = ${i}" for i, k in enumerate(kw, 1))
        await self.conn.execute(f"UPDATE recording_settings SET {cols} WHERE id = 1", *kw.values())  # noqa: S608

    async def camera(self, slug: str = "yard"):
        return await self.conn.fetchval(
            "INSERT INTO cameras (name, slug, stream_url) VALUES ($1, $1, 'rtsp://go2rtc/x') RETURNING id",
            slug,
        )

    async def segment(self, cam, ago: timedelta, *, open_: bool = False, as_dir: bool = False):
        rel = f"segments/{cam}/{ago.total_seconds():.0f}.mp4"
        path = self.media / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if as_dir:
            path.mkdir()
            (path / "stuck").write_bytes(b"x")
        else:
            path.write_bytes(b"\0" * SEGMENT_BYTES)
        seg_id = await self.conn.fetchval(
            """
            INSERT INTO recordings (camera_id, started_at, ended_at, path)
            VALUES ($1, now() - $2::interval,
                    CASE WHEN $3 THEN NULL ELSE now() - $2::interval + interval '1 minute' END, $4)
            RETURNING id
            """,
            cam, ago, open_, rel,
        )
        return SimpleNamespace(id=seg_id, path=path)

    async def track(self, cam, class_id: int, ago: timedelta, *, keep: timedelta | None = None,
                    length: timedelta = timedelta(seconds=40)):
        return await self.conn.fetchval(
            """
            INSERT INTO tracks_all (camera_id, local_track_id, class_id, class_name,
                                    started_at, ended_at, n_observations, retain_until)
            VALUES ($1, 1, $2, 'x', now() - $3::interval, now() - $3::interval + $4::interval, 5,
                    now() + $5::interval)
            RETURNING id
            """,
            cam, class_id, ago, length, keep if keep is not None else timedelta(days=30),
        )

    async def sample(self, cam, ago: timedelta) -> None:
        await self.conn.execute(
            """
            INSERT INTO track_embedding_samples (camera_id, local_track_id, pts_ns, sequence,
                                                 confidence, bbox, captured_at)
            VALUES ($1, 1, 0, 0, 0.9, '{0,0,1,1}', now() - $2::interval)
            """,
            cam, ago,
        )

    async def kept(self, seg) -> bool:
        row = await self.conn.fetchval("SELECT count(*) FROM recordings WHERE id = $1", seg.id)
        assert bool(row) == seg.path.exists(), "a row and its file must go together"
        return bool(row)


def _run(pg: str, media: Path, arrange):
    """Arrange the scene, run one retention pass, then hand the scene back
    for the verdict."""

    async def main():
        sup = RecorderSupervisor(RecorderConfig(
            dsn=pg, media_path=media, segment_seconds=60, rtsp_timeout_us=1,
            poll_seconds=1, retention_check_seconds=1, go2rtc_rtsp_base="rtsp://go2rtc:8554",
        ))
        sup._pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            async with sup._pool.acquire() as conn:
                scene = _Scene(conn, media)
                check = await arrange(scene)
            await sup._run_retention()
            async with sup._pool.acquire() as conn:
                scene.conn = conn
                await check(scene)
        finally:
            await sup._pool.close()

    asyncio.run(main())


H = timedelta(hours=1)
D = timedelta(days=1)


def test_cancelled_recording_batch_finishes_its_metadata_delete(pg, media, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    unlink = supervisor._unlink_recording_files

    def blocked_unlink(*args):
        entered.set()
        assert release.wait(3)
        return unlink(*args)

    monkeypatch.setattr(supervisor, "_unlink_recording_files", blocked_unlink)

    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        task = None
        try:
            async with pool.acquire() as conn:
                scene = _Scene(conn, media)
                segment = await scene.segment(await scene.camera(), 8 * D)
                svc = object.__new__(RecorderSupervisor)
                svc._config = SimpleNamespace(media_path=media)
                task = asyncio.create_task(svc._delete_segments(conn, "id=$1", segment.id))
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not await scene.kept(segment)
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await pool.close()

    asyncio.run(main())


def test_footage_past_the_age_cap_goes_file_and_row_together(pg, media, disk):
    async def arrange(s):
        cam = await s.camera()
        old, young = await s.segment(cam, 8 * D), await s.segment(cam, 6 * D)

        async def check(s):
            assert not await s.kept(old)
            assert await s.kept(young)
        return check

    _run(pg, media, arrange)


def test_a_person_on_camera_outlives_the_age_cap_and_a_car_does_not(pg, media, disk):
    async def arrange(s):
        cam = await s.camera()
        with_person, with_car = await s.segment(cam, 9 * D), await s.segment(cam, 8 * D)
        await s.track(cam, PERSON, 9 * D - timedelta(seconds=10), keep=21 * D)
        await s.track(cam, CAR, 8 * D - timedelta(seconds=10))

        async def check(s):
            assert await s.kept(with_person)
            pin = await s.conn.fetchval(
                "SELECT retain_until - now() FROM recordings WHERE id = $1", with_person.id)
            assert pin > 20 * D
            assert not await s.kept(with_car)
        return check

    _run(pg, media, arrange)


def test_a_person_track_just_outside_the_segment_still_pins_it_within_the_roll(pg, media, disk):
    async def arrange(s):
        cam = await s.camera()
        seg = await s.segment(cam, 8 * D)
        await s.track(cam, PERSON, 8 * D - timedelta(seconds=90), keep=D)

        async def check(s):
            assert await s.kept(seg)
        return check

    _run(pg, media, arrange)


def test_an_expired_person_track_no_longer_defends_its_footage(pg, media, disk):
    async def arrange(s):
        cam = await s.camera()
        seg = await s.segment(cam, 8 * D)
        await s.track(cam, PERSON, 8 * D - timedelta(seconds=10), keep=-H)
        pinned_then_lapsed = await s.segment(cam, 10 * D)
        await s.conn.execute(
            "UPDATE recordings SET retain_until = now() - interval '1 hour' WHERE id = $1",
            pinned_then_lapsed.id)

        async def check(s):
            assert not await s.kept(seg)
            assert not await s.kept(pinned_then_lapsed)
        return check

    _run(pg, media, arrange)


def test_a_file_that_will_not_delete_keeps_its_row(pg, media, disk):
    async def arrange(s):
        cam = await s.camera()
        stuck = await s.segment(cam, 8 * D, as_dir=True)

        async def check(s):
            assert await s.conn.fetchval("SELECT count(*) FROM recordings WHERE id = $1", stuck.id) == 1
        return check

    _run(pg, media, arrange)


def test_activity_mode_keeps_what_overlaps_a_track_and_drops_the_empty(pg, media, disk):
    async def arrange(s):
        await s.settings(mode="activity", retention_days=30)
        cam = await s.camera()
        visited = await s.segment(cam, 3 * H)
        in_the_roll = await s.segment(cam, 5 * H)
        empty = await s.segment(cam, 7 * H)
        recording_now = await s.segment(cam, timedelta(minutes=10), open_=True)
        await s.track(cam, CAR, 3 * H - timedelta(seconds=20))
        await s.track(cam, CAR, 5 * H + timedelta(seconds=90))

        async def check(s):
            assert await s.kept(visited)
            assert await s.kept(in_the_roll)
            assert not await s.kept(empty)
            assert await s.kept(recording_now)
        return check

    _run(pg, media, arrange)


def test_activity_mode_leaves_a_camera_with_a_live_subject_alone(pg, media, disk):
    async def arrange(s):
        await s.settings(mode="activity")
        busy, quiet = await s.camera("busy"), await s.camera("quiet")
        busy_empty, quiet_empty = await s.segment(busy, 2 * H), await s.segment(quiet, 2 * H)
        await s.sample(busy, timedelta(minutes=1))
        await s.track(quiet, CAR, timedelta(minutes=10))

        async def check(s):
            assert await s.kept(busy_empty)
            assert not await s.kept(quiet_empty)
        return check

    _run(pg, media, arrange)


@pytest.mark.parametrize("last_finalized", [H, None])
def test_activity_mode_waits_while_the_finalizer_is_stalled(pg, media, disk, last_finalized):
    async def arrange(s):
        await s.settings(mode="activity")
        busy, quiet = await s.camera("busy"), await s.camera("quiet")
        empty = await s.segment(quiet, 2 * H)
        await s.sample(busy, timedelta(minutes=1))
        if last_finalized is not None:
            await s.track(quiet, CAR, last_finalized)

        async def check(s):
            assert await s.kept(empty)
        return check

    _run(pg, media, arrange)


def test_activity_mode_never_prunes_past_the_shortest_track_tier(pg, media, disk):
    async def arrange(s):
        await s.settings(mode="activity", retention_days=60)
        cam = await s.camera()
        beyond, within = await s.segment(cam, 40 * D), await s.segment(cam, 20 * D)

        async def check(s):
            assert await s.kept(beyond)
            assert not await s.kept(within)
        return check

    _run(pg, media, arrange)


async def _many(s, cam, n: int, start: timedelta) -> list:
    return [await s.segment(cam, start - i * timedelta(minutes=1)) for i in range(n)]


def test_a_full_disk_sheds_the_oldest_unpinned_segments_first(pg, media, disk):
    disk.total = 260 * SEGMENT_BYTES

    async def arrange(s):
        cam = await s.camera()
        oldest_person = await s.segment(cam, 3 * D)
        await s.track(cam, PERSON, 3 * D - timedelta(seconds=10))
        segs = await _many(s, cam, 250, 2 * D)
        live = await s.segment(cam, timedelta(seconds=30), open_=True)

        async def check(s):
            assert await s.kept(oldest_person)
            assert await s.kept(live)
            kept = [await s.kept(x) for x in segs]
            assert kept == [False] * 100 + [True] * 150
        return check

    _run(pg, media, arrange)


def test_a_co_tenant_filling_the_volume_stops_the_prune_after_one_batch(pg, media, disk, caplog):
    disk.total = 260 * SEGMENT_BYTES
    disk.other_growth = 1000 * SEGMENT_BYTES

    async def arrange(s):
        cam = await s.camera()
        segs = await _many(s, cam, 250, 2 * D)

        async def check(s):
            assert sum([await s.kept(x) for x in segs]) == 150
        return check

    with caplog.at_level(logging.ERROR):
        _run(pg, media, arrange)
    assert "the pressure is NOT BABA recordings" in caplog.text


def test_a_disk_full_of_pinned_person_footage_alarms_instead_of_eating_it(pg, media, disk, caplog):
    disk.total = 3 * SEGMENT_BYTES

    async def arrange(s):
        cam = await s.camera()
        segs = [await s.segment(cam, i * H) for i in (1, 2, 3)]
        for i in (1, 2, 3):
            await s.track(cam, PERSON, i * H - timedelta(seconds=10))

        async def check(s):
            assert all([await s.kept(x) for x in segs])
        return check

    with caplog.at_level(logging.ERROR):
        _run(pg, media, arrange)
    assert "refusing to delete them" in caplog.text
