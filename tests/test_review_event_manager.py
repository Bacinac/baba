import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_departure_opens_a_new_visit_while_parked_arrival_is_waiting_for_commit(pg, tmp_path):
    from test_observe import CAR, run, track

    async def body(w):
        await w.tick(track(1, 300, cls=CAR))
        await w.tick(track(1, 320, cls=CAR, motion="parked"))
        arrival = w.rec(1)
        w.em._queue_finalization("gate", 1, arrival, parked=True)
        arrival.park_finalize_due = False
        finalize = w.em._finalize
        w.em._finalize = AsyncMock(side_effect=RuntimeError("database unavailable"))
        await w.em._drain_finalizations()
        assert not arrival.parked_finalized
        await w.tick(track(1, 450, cls=CAR))
        departure = w.rec(1)
        assert departure is not arrival and departure.db_track_id != arrival.db_track_id
        assert w.em._pending_finalizations[arrival.db_track_id].record.last_seen_ns == arrival.last_seen_ns
        w.em._finalize = finalize
        await w.em._drain_finalizations()
        assert arrival.parked_finalized and not departure.parked_finalized
        assert w.rec(1) is departure

    run(pg, tmp_path, body)


def test_finalization_retries_the_entire_transaction_without_duplicate_events(pg, tmp_path):
    from test_finalize import _run

    async def body(w):
        rec = w.rec()
        rec.inside_zones = {w.zone["drive"]: rec.first_seen_ns}
        rec.enter_emitted = {w.zone["drive"]}
        original = w.em._file_naming
        w.em._file_naming = AsyncMock(side_effect=RuntimeError("database transaction failed"))
        with pytest.raises(RuntimeError):
            await w.em._finalize("yard", 1, rec)
        assert await w.count("tracks_all") == 0
        assert await w.count("events") == 1
        w.em._file_naming = original
        await w.em._finalize("yard", 1, rec)
        await w.em._finalize("yard", 1, rec)
        assert await w.count("tracks_all") == 1
        assert await w.count("events") == 2

    _run(pg, tmp_path, body)


def test_reconnect_reconciles_tunables_and_active_face_settings(pg, tmp_path):
    from baba_core.tunables import REID_KEY
    from test_finalize import _run

    async def body(w):
        await w.pool.execute(
            "INSERT INTO app_settings(key,value) VALUES($1,$2::jsonb) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            REID_KEY, json.dumps({"reid_body_cluster_threshold": .19}),
        )
        await w.pool.execute(
            "UPDATE face_recognition_settings SET model_key='pending', active_model_key='loaded', "
            "match_threshold=.8, active_match_threshold=.35"
        )
        await w.em._reconcile()
        assert w.em._config.face_embedding_model == "loaded"
        assert w.em._config.reid_face_cosine_threshold == pytest.approx(.35)
        assert w.em._config.reid_body_cluster_threshold == pytest.approx(.19)
        w.em._pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=RuntimeError("database unavailable")))
        with pytest.raises(RuntimeError, match="database unavailable"):
            await w.em._refresh_face_threshold()

    _run(pg, tmp_path, body)
