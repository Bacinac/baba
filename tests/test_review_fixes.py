import asyncio
import dataclasses
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import numpy as np
import pytest
from baba_api import routes_ai, routes_auth, routes_recordings, rules_dispatcher
from baba_api import tracks_retention_sweeper as retention
from baba_api.models import SegmentAtPointIn
from baba_api.routes_rules import RuleFilter, RulePatch
from baba_core.face_settings import acknowledge_face_selection, read_face_selection
from baba_core.inference import run_inference
from baba_core.pg_listen import ResilientListener
from baba_core.stats import StatsCollector
from baba_event_manager.__main__ import EventManager
from baba_event_manager._state import TrackState
from baba_event_manager.config import EventManagerConfig
from baba_recorder.worker import CameraRecorder
from baba_state_evaluator import __main__ as scene
from pydantic import ValidationError


def test_expired_and_parked_tracks_survive_a_failed_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("BABA_GO2RTC_API_PASSWORD", "test")
    async def main():
        config = dataclasses.replace(EventManagerConfig.from_env(), media_path=str(tmp_path))
        em = EventManager(config)
        now = time.time_ns()
        def record(at):
            return TrackState(at, at, at, 0, "person", .9, 10, (0, 0, 50, 100), 0, 50, 0, 100)
        expired = record(now - 60_000_000_000)
        parked = record(now)
        parked.park_finalize_due = True
        em._state = {"yard": {1: expired, 2: parked}}
        attempts = []
        async def commit(camera, local, rec):
            attempts.append((local, rec.db_track_id))
            if len(attempts) <= 2:
                raise RuntimeError("database unavailable")
        em._finalize = commit
        try:
            await em._sweep_once()
            assert len(em._pending_finalizations) == 2
            assert 1 not in em._state["yard"]
            assert not parked.parked_finalized
            await em._sweep_once()
            assert not em._pending_finalizations
            assert parked.parked_finalized
            assert attempts[:2] == attempts[2:]
        finally:
            await em._thumbs.close()
    asyncio.run(main())


def test_scene_transition_retries_without_another_classification_change(monkeypatch):
    async def main():
        rid = uuid4()
        region = scene.RegionConfig(id=rid, camera_id=uuid4(), camera_slug="yard", name="Gate",
                                    polygon=[[0, 0], [1, 0], [1, 1]], states=["closed", "open"],
                                    sample_interval_s=1, hysteresis_n=1, unknown_margin=.1, enabled=True)
        rt = scene.RegionRuntime(current_state="closed")
        svc = object.__new__(scene.StateEvaluator)
        svc._lock = asyncio.Lock()
        svc._regions, svc._runtime = {rid: region}, {rid: rt}
        svc._protos = {rid: [("closed", np.ones(3)), ("open", np.ones(3))]}
        svc._stats = StatsCollector("test")
        sequence = iter(range(1, 10))
        svc._reader_for = lambda _: SimpleNamespace(get_latest=lambda: SimpleNamespace(sequence=next(sequence)))
        svc._embed_region = AsyncMock(return_value=scene._Frame(np.full((10, 10, 3), 50, np.uint8), np.ones(3), 20))
        svc._status_eval_tick = AsyncMock()
        svc._commit_transition = AsyncMock(side_effect=[RuntimeError("database unavailable"), None])
        monkeypatch.setattr(scene, "read_region", lambda *args: ("open", .01, {}))
        with pytest.raises(RuntimeError):
            await svc._tick_region(rid)
        assert rt.current_state == "closed"
        await svc._tick_region(rid)
        assert rt.current_state == "open"
        assert svc._commit_transition.await_count == 2
    asyncio.run(main())


def test_listener_closes_every_failed_reconcile_connection(monkeypatch):
    async def main():
        connections = []
        async def connect(_):
            c = SimpleNamespace(add_termination_listener=lambda cb: None,
                                add_listener=AsyncMock(), close=AsyncMock())
            connections.append(c)
            return c
        monkeypatch.setattr(asyncpg, "connect", connect)
        reconcile = AsyncMock(side_effect=[RuntimeError("first"), RuntimeError("second"), None])
        listener = ResilientListener("test", ["changed"], lambda *a: None, reconcile, reconnect_min_s=.001)
        await listener._reconnect()
        assert len(connections) == 3
        assert all(c.close.await_count == 1 for c in connections[:2])
        assert connections[-1].close.await_count == 0
        await listener.stop()
        assert connections[-1].close.await_count == 1
    asyncio.run(main())


def test_sam2_keeps_each_image_and_prediction_together():
    entered, release = threading.Event(), threading.Event()
    seen = []
    class Predictor:
        def set_image(self, rgb):
            self.image = int(rgb[0, 0, 0])
            if self.image == 1:
                entered.set()
                assert release.wait(2)
        def predict_from_clicks(self, **kwargs):
            seen.append((kwargs["positive_norm"][0][0], self.image))
            return None
    async def main():
        p = Predictor()
        a = asyncio.create_task(asyncio.to_thread(routes_ai._segment_at_point_sync, p,
            np.ones((4, 4, 3), np.uint8), SegmentAtPointIn(positive=[[.1, .1]])))
        assert await asyncio.to_thread(entered.wait, 2)
        b = asyncio.create_task(asyncio.to_thread(routes_ai._segment_at_point_sync, p,
            np.full((4, 4, 3), 2, np.uint8), SegmentAtPointIn(positive=[[.2, .2]])))
        await asyncio.sleep(.01)
        release.set()
        await asyncio.gather(a, b)
        assert seen == [(.1, 1), (.2, 2)]
    asyncio.run(main())


@pytest.mark.parametrize("raw", [{"class_ids": ["x"]}, {"class_ids": [True]}, {"class_ids": [80]},
                                  {"camera_ids": ["x"]}, {"unknown": []}])
def test_rule_filters_reject_invalid_values(raw):
    with pytest.raises(ValidationError):
        RuleFilter.model_validate(raw)


def test_rule_patch_rejects_null_filter():
    with pytest.raises(ValidationError):
        RulePatch.model_validate({"filter": None})


def test_bad_stored_rule_does_not_block_the_next_rule():
    async def main():
        bad, good, channel = uuid4(), uuid4(), uuid4()
        pool = SimpleNamespace(fetch=AsyncMock(side_effect=[[
            {"id": bad, "filter": '{"class_ids":["bad"]}', "channel_ids": [channel]},
            {"id": good, "filter": '{"class_ids":[0]}', "channel_ids": [channel]},
        ], []]), execute=AsyncMock())
        event = {"kind": "zone_enter", "camera_id": uuid4(), "track_class_id": 0}
        await rules_dispatcher._dispatch_event(pool, "test", event)
        assert pool.fetch.await_count == 2
        assert pool.execute.await_args_list[0].args[1] == bad
        assert pool.execute.await_args_list[1].args[-1] == good
    asyncio.run(main())


def test_clip_caps_database_query_before_work_and_rejects_nonfinite_values():
    async def main():
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
        for end in (float("nan"), float("inf")):
            with pytest.raises(routes_recordings.HTTPException) as exc:
                await routes_recordings.get_camera_clip(uuid4(), req, start=1700000000, end=end, range_header=None)
            assert exc.value.status_code == 400
        pool.fetch.assert_not_awaited()
        with pytest.raises(routes_recordings.HTTPException) as exc:
            await routes_recordings.get_camera_clip(uuid4(), req, start=1700000000,
                                                    end=1700000000 + 30 * 86400, range_header=None)
        assert exc.value.status_code == 404
        args = pool.fetch.await_args.args
        assert (args[3] - args[2]).total_seconds() == 1200
    asyncio.run(main())


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
def test_ffprobe_is_killed_and_reaped_on_timeout_and_cancellation(monkeypatch, failure):
    async def main():
        proc = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=failure),
                               kill=lambda: None, wait=AsyncMock())
        killed = []
        proc.kill = lambda: killed.append(True)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
        recorder = SimpleNamespace(_probed_codec=None, _spec=SimpleNamespace(slug="yard"))
        if failure is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await CameraRecorder._probe_codec(recorder, Path("/tmp/segment.mp4"))
        else:
            assert await CameraRecorder._probe_codec(recorder, Path("/tmp/segment.mp4")) is None
        assert killed == [True]
        proc.wait.assert_awaited_once()
    asyncio.run(main())


@pytest.mark.parametrize("cancelled", [False, True])
def test_stuck_native_inference_exits_the_process(cancelled):
    code = "import asyncio,time; from baba_core.inference import run_inference; from home_core.tasks import spawn\n"
    code += "async def main():\n t=spawn(run_inference(time.sleep,60,timeout_s=.05))\n"
    if cancelled:
        code += " await asyncio.sleep(.01)\n t.cancel()\n t.cancel()\n"
    code += " await t\nasyncio.run(main())"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
    assert result.returncode == 1
    assert b"exiting for restart" in result.stderr


def test_cancelled_inference_keeps_ownership_until_the_thread_finishes():
    from home_core.tasks import spawn

    async def main():
        entered, release = threading.Event(), threading.Event()
        def native():
            entered.set()
            assert release.wait(2)
        task = spawn(run_inference(native, timeout_s=3))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(main())


def test_native_timeout_error_is_not_a_watchdog_expiry():
    def native():
        raise TimeoutError("native operation failed")
    async def main():
        with pytest.raises(TimeoutError, match="native operation failed"):
            await run_inference(native)
    asyncio.run(main())


def test_face_activation_requires_both_services_and_rejects_stale_acknowledgements(pg):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        try:
            old = await read_face_selection(pool)
            await acknowledge_face_selection(pool, "api", old)
            assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") == 0
            await acknowledge_face_selection(pool, "embedder", old, "cannot load")
            assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") == 0
            await acknowledge_face_selection(pool, "embedder", old)
            assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") == old.revision
            await pool.execute("UPDATE face_recognition_settings SET revision=revision+1, model_key='topofr', "
                               "api_revision=NULL, embedder_revision=NULL, api_error=NULL, embedder_error=NULL")
            new = await read_face_selection(pool)
            assert not await acknowledge_face_selection(pool, "api", old)
            await asyncio.gather(acknowledge_face_selection(pool, "api", new),
                                 acknowledge_face_selection(pool, "embedder", new))
            row = await pool.fetchrow("SELECT active_model_key, active_revision FROM face_recognition_settings")
            assert dict(row) == {"active_model_key": "topofr", "active_revision": new.revision}
        finally:
            await pool.close()
    asyncio.run(main())


def test_only_one_concurrent_login_can_consume_a_recovery_code(pg, monkeypatch):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        try:
            user = await pool.fetchval("INSERT INTO users(username,password_hash,role) VALUES('test','test','admin') RETURNING id")
            await pool.execute("INSERT INTO user_recovery_codes(user_id,code_hash) VALUES($1,'test')", user)
            arrived, ready = 0, asyncio.Event()
            async def verify(*args):
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
                return True
            monkeypatch.setattr(routes_auth, "verify_password", verify)
            results = await asyncio.gather(*(routes_auth._consume_recovery_code(pool, user, "abcd") for _ in range(2)))
            assert sorted(results) == [False, True]
        finally:
            await pool.close()
    asyncio.run(main())


def test_live_zone_event_has_class_before_the_track_is_finalized(pg):
    async def main():
        pool = await asyncpg.create_pool(pg)
        try:
            cam = await pool.fetchval("INSERT INTO cameras(name,slug,stream_url) VALUES('yard','yard','rtsp://x') RETURNING id")
            event = await pool.fetchval("INSERT INTO events(camera_id,track_id,kind,at,payload) "
                "VALUES($1,$2,'zone_enter',now(),$3::jsonb) RETURNING id", cam, uuid4(),
                json.dumps({"class_name": "person", "class_id": 0}))
            found = await rules_dispatcher._fetch_event(pool, str(event))
            assert found["track_class_id"] == 0
            assert rules_dispatcher._matches_filter({"class_ids": [0]}, found)
        finally:
            await pool.close()
    asyncio.run(main())


def test_retention_skips_a_track_whose_deadline_is_being_extended(pg, tmp_path):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=3)
        try:
            cam = await pool.fetchval("INSERT INTO cameras(name,slug,stream_url) VALUES('yard','yard','rtsp://x') RETURNING id")
            tid = await pool.fetchval("INSERT INTO tracks_all(camera_id,local_track_id,class_id,class_name,started_at,ended_at,"
                "n_observations,retain_until,crop_path) VALUES($1,1,0,'person',now(),now(),1,now()-interval '1 hour','crop.jpg') RETURNING id", cam)
            (tmp_path / "crop.jpg").write_bytes(b"crop")
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("UPDATE tracks_all SET retain_until=now()+interval '90 days' WHERE id=$1", tid)
                assert await retention._prune_one_batch(pool, tmp_path, 10) == 0
            assert await retention._prune_one_batch(pool, tmp_path, 10) == 0
            assert (tmp_path / "crop.jpg").exists()
            assert await pool.fetchval("SELECT EXISTS(SELECT 1 FROM tracks_all WHERE id=$1)", tid)
        finally:
            await pool.close()
    asyncio.run(main())




def test_embedder_reloads_after_inflight_processing_and_reports_loaded_pair(pg, tmp_path, monkeypatch):
    from baba_embedder import __main__ as producer

    async def main():
        pool = await asyncpg.create_pool(pg)
        svc = producer.Embedder(pg, "nats://unused", None, tmp_path)
        svc._pool = pool
        fake_stack = object()
        monkeypatch.setenv("BABA_FACE_DETECTOR_MODEL", "/models/yunet.onnx")
        monkeypatch.setattr(producer, "make_face_stack_for_model", lambda **kw: (fake_stack, kw["detector_key"], kw["model_key"]))
        svc.native_face_reader = lambda: SimpleNamespace(run=lambda stop: stop.wait())
        try:
            await svc._processing_lock.acquire()
            task = asyncio.create_task(svc._reload_face_settings())
            await asyncio.sleep(.01)
            assert not task.done()
            assert await pool.fetchval("SELECT embedder_revision FROM face_recognition_settings") is None
            svc._processing_lock.release()
            await task
            selected = await read_face_selection(pool)
            assert svc._face is fake_stack
            assert svc._face_model_key == selected.model_key
            assert svc._face_loaded_pair == (selected.detector_key, selected.model_key)
            assert await pool.fetchval("SELECT embedder_revision FROM face_recognition_settings") == selected.revision
        finally:
            if svc._processing_lock.locked():
                svc._processing_lock.release()
            await svc._stop_native_reader()
            await pool.close()
    asyncio.run(main())


def test_reference_enrolment_rejects_vectors_from_a_previous_model():
    from baba_api.routes_identities._reference_photos import _persist_reference_photos

    async def main():
        state = SimpleNamespace(pool=None, face_stack=object(), face_stack_error=None, face_model_key="new")
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        with pytest.raises(routes_recordings.HTTPException) as exc:
            await _persist_reference_photos(request, uuid4(), None, [np.ones((10, 10, 3))], [], "from-face-samples",
                                           pre_face_embeddings=[np.ones(512)], pre_face_model_key="old", face_only=True)
        assert exc.value.status_code == 409
    asyncio.run(main())


@pytest.mark.parametrize("codecs", [["h264", "hevc"], ["hevc", "h264"]])
def test_clip_rechecks_codec_after_the_encoding_window_is_capped(tmp_path, monkeypatch, codecs):
    from datetime import UTC, datetime

    async def main():
        start = 1700000000
        records = []
        for offset, codec in zip([0, 300], codecs, strict=True):
            path = f"{offset}.mp4"
            (tmp_path / path).write_bytes(b"media")
            records.append({"path": path, "codec": codec,
                            "started_at": datetime.fromtimestamp(start + offset, UTC)})
        pool = SimpleNamespace(fetch=AsyncMock(return_value=records))
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool, config=SimpleNamespace(media_path=tmp_path))))
        cuts = []
        async def run(cut, camera, pairs):
            cuts.append((cut.dur, cut.reencode, len(pairs)))
            cut.out.write_bytes(b"media")
        monkeypatch.setattr(routes_recordings._ClipCut, "run", run)
        monkeypatch.setattr(routes_recordings, "_serve_with_range", lambda *args: None)
        await routes_recordings.get_camera_clip(uuid4(), req, start=start, end=start + 1000, range_header=None)
        assert cuts == [(240, codecs[0] == "hevc", 1)]
    asyncio.run(main())
