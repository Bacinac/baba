import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from baba_core.events import TrackEvent


@pytest.mark.parametrize("commit_arrival", [False, True])
def test_parked_and_departure_events_link_their_separate_visits(pg, tmp_path, commit_arrival):
    from test_observe import CAR, run, track

    async def body(w):
        await w.tick(track(1, 300, cls=CAR))
        await w.tick(track(1, 320, cls=CAR, motion="parked"))
        arrival = w.rec(1)
        w.em._finalizations.enqueue("gate", 1, arrival, parked=True)
        if commit_arrival:
            await w.em._finalizations.drain(w.em._finalize)
            assert arrival.parked_finalized
        await w.tick(track(1, 450, cls=CAR))
        departure = w.rec(1)
        rows = await w.pool.fetch("SELECT kind,track_id,payload FROM events ORDER BY at")
        assert [r["kind"] for r in rows] == ["object_parked", "object_unparked"]
        assert [r["track_id"] for r in rows] == [arrival.db_track_id, departure.db_track_id]
        assert departure.db_track_id != arrival.db_track_id
        assert all(json.loads(row["payload"])["class_id"] == 2 for row in rows)
        await w.tick(track(1, 480, cls=CAR))
        assert await w.pool.fetchval("SELECT count(*) FROM events WHERE kind='object_unparked'") == 1
        await w.em._finalizations.drain(w.em._finalize)
        assert arrival.parked_finalized and w.rec(1) is departure

    run(pg, tmp_path, body)


def test_event_retry_is_idempotent_but_reused_local_ids_keep_separate_visits(pg, tmp_path):
    from test_observe import run

    async def body(w):
        event = TrackEvent("zone_enter", 1, uuid4(), 2, "car", (10, 20, 30, 40), uuid4())
        timestamp = 1_750_000_001_000_000_000
        await w.em._emit_track_event("gate", event, timestamp)
        await w.em._emit_track_event("gate", event, timestamp)
        other_visit = replace(event, track_id=uuid4())
        await w.em._emit_track_event("gate", other_visit, timestamp)
        rows = await w.pool.fetch("SELECT id,track_id,payload FROM events")
        assert len(rows) == 2 and {r["track_id"] for r in rows} == {event.track_id, other_visit.track_id}
        assert all(json.loads(r["payload"])["class_id"] == 2 for r in rows)
        assert w.em._stats.snapshot().counters["events"] == 2

    run(pg, tmp_path, body)


def test_event_snapshot_keeps_class_and_coordinates_and_returns_independent_payloads():
    event = TrackEvent("object_parked", 1, uuid4(), 2, "car", (10, 20, 30, 40))
    payload = event.payload()
    payload["bbox"][0] = 999
    payload["class_id"] = 7
    assert event.payload() == {"bbox": [10, 20, 30, 40], "class_id": 2, "class_name": "car", "track_id": "1"}
    with pytest.raises(FrozenInstanceError):
        event.class_name = "truck"


@pytest.mark.parametrize("change", [
    {"track_id": None}, {"kind": "track_finalized"}, {"kind": "zone_enter"},
])
def test_incomplete_transition_contracts_fail_before_persistence(change):
    args = dict(kind="object_parked", local_track_id=1, track_id=uuid4(), class_id=2,
                class_name="car", bbox=(10, 20, 30, 40))
    args.update(change)
    with pytest.raises(ValueError):
        TrackEvent(**args)
