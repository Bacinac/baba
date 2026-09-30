"""An expired track takes its own media with it, and only that.

A plate read outlives the track it came from: the read is the evidence the
place registry was built on, so the sweeper leaves its row and its crop. It
goes on its own identity's tier once nothing cites it any more, and so do
closed presence episodes and parking places; what is still open is kept.
"""

import asyncio
from datetime import timedelta
from pathlib import Path

import asyncpg
from baba_api.tracks_retention_sweeper import _prune_one_batch, prune_expired

D = timedelta(days=1)


class _Media:
    def __init__(self, root: Path) -> None:
        self.root = root

    def file(self, rel: str) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return rel

    def has(self, rel: str) -> bool:
        return (self.root / rel).exists()


async def _camera(conn):
    return await conn.fetchval(
        "INSERT INTO cameras (name, slug, stream_url) VALUES ('yard', 'yard', 'rtsp://go2rtc/x') RETURNING id"
    )


async def _track(conn, media: _Media, cam, name: str, keep: timedelta):
    return await conn.fetchval(
        """
        INSERT INTO tracks_all (camera_id, local_track_id, class_id, class_name, started_at, ended_at,
                                n_observations, retain_until, crop_path, thumbnail_path,
                                face_crop_path, plate_crop_path)
        VALUES ($1, 1, 0, 'person', now() - interval '31 days', now() - interval '31 days', 5,
                now() + $2::interval, $3, $4, $5, $6)
        RETURNING id
        """,
        cam, keep,
        media.file(f"crops/{name}.jpg"), media.file(f"thumbnails/{name}.jpg"),
        media.file(f"face_crops/{name}.jpg"), media.file(f"plate_crops/{name}.jpg"),
    )


def _run(pg: str, body) -> None:
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=2)
        try:
            await body(pool)
        finally:
            await pool.close()

    asyncio.run(main())


def test_an_expired_track_goes_with_its_media_and_leaves_its_plate_read(pg, tmp_path):
    media = _Media(tmp_path)

    async def body(pool):
        async with pool.acquire() as conn:
            cam = await _camera(conn)
            gone = await _track(conn, media, cam, "gone", keep=-D)
            kept = await _track(conn, media, cam, "kept", keep=D)
            read = await conn.fetchval(
                """
                INSERT INTO plate_reads (camera_id, track_id, read_at, plate_text, readings, crop_path)
                VALUES ($1, $2, now(), 'ZG1234AB', 2, 'plate_crops/gone.jpg') RETURNING id
                """,
                cam, gone,
            )

        assert await _prune_one_batch(pool, tmp_path, 100) == 1

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM tracks_all WHERE id = $1", gone) == 0
            for rel in ("crops/gone.jpg", "thumbnails/gone.jpg", "face_crops/gone.jpg"):
                assert not media.has(rel), rel
            assert await conn.fetchval("SELECT track_id FROM plate_reads WHERE id = $1", read) is None
            assert media.has("plate_crops/gone.jpg")

            assert await conn.fetchval("SELECT count(*) FROM tracks_all WHERE id = $1", kept) == 1
            assert media.has("crops/kept.jpg")

    _run(pg, body)


def test_a_track_whose_media_will_not_delete_keeps_its_row(pg, tmp_path):
    media = _Media(tmp_path)

    async def body(pool):
        async with pool.acquire() as conn:
            cam = await _camera(conn)
            stuck = await _track(conn, media, cam, "stuck", keep=-D)
        blocker = tmp_path / "crops" / "stuck.jpg"
        blocker.unlink()
        blocker.mkdir()
        (blocker / "x").write_bytes(b"x")

        assert await _prune_one_batch(pool, tmp_path, 100) == 0

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM tracks_all WHERE id = $1", stuck) == 1

    _run(pg, body)


async def _read(conn, media: _Media, cam, name: str, age_days: int, gid=None):
    return await conn.fetchval(
        """
        INSERT INTO plate_reads (camera_id, read_at, plate_text, readings, global_id, crop_path)
        VALUES ($1, now() - make_interval(days => $2), 'ZG1234AB', 2, $3, $4) RETURNING id
        """,
        cam, age_days, gid, media.file(f"plate_crops/{name}.jpg"),
    )


async def _count(conn, table: str, row_id) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM {table} WHERE id = $1", row_id)  # noqa: S608


def test_a_plate_read_goes_on_its_tier_once_nothing_cites_it(pg, tmp_path):
    media = _Media(tmp_path)

    async def body(pool):
        async with pool.acquire() as conn:
            cam = await _camera(conn)
            resident = await conn.fetchval(
                "INSERT INTO identity_labels (global_id, name, kind) "
                "VALUES (gen_random_uuid(), 'Mazda', 'vehicle') RETURNING global_id"
            )
            guest = await _read(conn, media, cam, "guest", 31)
            fresh = await _read(conn, media, cam, "fresh", 29)
            named = await _read(conn, media, cam, "named", 31, resident)
            parked = await _read(conn, media, cam, "parked", 31)
            await conn.execute(
                "INSERT INTO place_occupancy (place, evidence, plate_read_id, occupied_since) "
                "VALUES ('P1', 'plate', $1, now() - interval '31 days')",
                parked,
            )
            cited = await _read(conn, media, cam, "cited", 31)
            track = await _track(conn, media, cam, "car", keep=D)
            await conn.execute("UPDATE plate_reads SET track_id = $1 WHERE id = $2", track, cited)

        assert await prune_expired(pool, tmp_path, 100) == {"plate reads": 1}

        async with pool.acquire() as conn:
            assert await _count(conn, "plate_reads", guest) == 0
            assert not media.has("plate_crops/guest.jpg")
            for kept, name in ((fresh, "fresh"), (named, "named"), (parked, "parked"), (cited, "cited")):
                assert await _count(conn, "plate_reads", kept) == 1, name
                assert media.has(f"plate_crops/{name}.jpg"), name

    _run(pg, body)


def test_presence_and_parking_go_once_closed_past_their_tier(pg, tmp_path):
    async def body(pool):
        async with pool.acquire() as conn:
            cam = await _camera(conn)

            async def episode(closed_days_ago):
                return await conn.fetchval(
                    """
                    INSERT INTO presence_episodes (global_id, camera_id, evidence, present_since,
                                                   last_confirmed_at, departed_at, closed_by)
                    VALUES (gen_random_uuid(), $1, 'face', now() - interval '40 days',
                            now() - interval '40 days', now() - make_interval(days => $2),
                            CASE WHEN $2 IS NULL THEN NULL ELSE 'absence' END)
                    RETURNING id
                    """,
                    cam, closed_days_ago,
                )

            async def place(name, released_days_ago):
                return await conn.fetchval(
                    """
                    INSERT INTO place_occupancy (place, evidence, occupied_since, released_at)
                    VALUES ($1, 'unknown', now() - interval '40 days', now() - make_interval(days => $2))
                    RETURNING id
                    """,
                    name, released_days_ago,
                )

            old_visit, recent_visit, still_here = await episode(31), await episode(29), await episode(None)
            old_place, recent_place, taken = await place("P1", 31), await place("P2", 29), await place("P3", None)

        assert await prune_expired(pool, tmp_path, 100) == {"parking places": 1, "presence episodes": 1}

        async with pool.acquire() as conn:
            assert await _count(conn, "presence_episodes", old_visit) == 0
            assert await _count(conn, "place_occupancy", old_place) == 0
            for table, kept in (("presence_episodes", recent_visit), ("presence_episodes", still_here),
                                ("place_occupancy", recent_place), ("place_occupancy", taken)):
                assert await _count(conn, table, kept) == 1, (table, kept)

    _run(pg, body)
