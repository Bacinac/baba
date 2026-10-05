import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
from baba_api import routes_events, rules_dispatcher


def run(pg, body):
    async def main():
        pool = await asyncpg.create_pool(pg)
        try:
            camera = await pool.fetchval(
                "INSERT INTO cameras(name,slug,stream_url) VALUES('yard','yard','rtsp://go2rtc/x') RETURNING id"
            )
            track_id = uuid4()
            event = await pool.fetchval(
                "INSERT INTO events(camera_id,track_id,kind,at,payload) "
                "VALUES($1,$2,'zone_enter',now(),$3::jsonb) RETURNING id",
                camera, track_id, json.dumps({"class_id": 2, "class_name": "car", "track_id": "1"}),
            )
            await body(pool, camera, track_id, event)
        finally:
            await pool.close()
    asyncio.run(main())


async def finalize_as_truck(pool, camera, track_id):
    await pool.execute(
        "INSERT INTO tracks_all(id,camera_id,local_track_id,class_id,class_name,started_at,ended_at,n_observations) "
        "VALUES($1,$2,1,7,'truck',now()-interval '10 seconds',now(),10)",
        track_id, camera,
    )


def test_alarm_filter_and_title_keep_the_observed_class_after_finalization(pg):
    async def body(pool, camera, track_id, event_id):
        before = await rules_dispatcher._fetch_event(pool, str(event_id))
        await finalize_as_truck(pool, camera, track_id)
        after = await rules_dispatcher._fetch_event(pool, str(event_id))
        for event in (before, after):
            assert rules_dispatcher._matches_filter({"class_ids": [2]}, event)
            assert not rules_dispatcher._matches_filter({"class_ids": [7]}, event)
            assert rules_dispatcher._build_payload(event)["class"] == "car"
        await pool.execute("DELETE FROM tracks_all WHERE id=$1", track_id)
        retained = await rules_dispatcher._fetch_event(pool, str(event_id))
        assert rules_dispatcher._matches_filter({"class_ids": [2]}, retained)

    run(pg, body)


def test_event_list_has_the_same_subject_before_and_after_track_persistence(pg):
    async def body(pool, camera, track_id, event_id):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))

        async def listing():
            rows = await routes_events.list_events(
                request, camera_id=camera, kind="zone_enter", since=None, until=None, limit=10
            )
            assert len(rows) == 1 and rows[0]["id"] == str(event_id)
            return rows[0]

        before = await listing()
        await finalize_as_truck(pool, camera, track_id)
        after = await listing()
        for event in (before, after):
            assert event["track"]["id"] == str(track_id)
            assert event["track"]["class_id"] == 2 and event["track"]["class_name"] == "car"
            assert event["payload"]["class_id"] == 2
        assert before["track"]["duration_s"] is None
        assert after["track"]["duration_s"] == 10

    run(pg, body)
