import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import numpy as np
import pytest
from baba_api import routes_face_recognition as fr
from baba_api.face_recompute_queue import claim_recompute, recover_recomputes, run_recompute_queue
from baba_core.face_settings import acknowledge_face_selection, read_face_selection
from home_core.tasks import spawn


def run(pg, body):
    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        try:
            await body(pool)
        finally:
            await pool.close()
    asyncio.run(main())


@pytest.fixture
def save(monkeypatch):
    monkeypatch.setattr(fr, "get_face_model", lambda _: SimpleNamespace(bundled_with_baba=True))
    monkeypatch.setattr(fr, "get_face_detector", lambda _: SimpleNamespace(bundled_with_baba=True))
    async def update(pool, model="model-b", threshold=.5):
        return await fr.put_face_recognition_settings(
            fr.FaceRecognitionSettingsIn(model_key=model, detector_key="yunet", match_threshold=threshold),
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool))),
            SimpleNamespace(id=None),
        )
    return update


async def activate(pool):
    selection = await read_face_selection(pool)
    await acknowledge_face_selection(pool, "api", selection)
    await acknowledge_face_selection(pool, "embedder", selection)


def test_saved_recompute_survives_navigation_and_waits_for_both_acknowledgements(pg, save):
    async def body(pool):
        settings = await save(pool)
        job_id = UUID(settings.recompute_job_id)
        assert await pool.fetchval("SELECT status FROM face_recompute_jobs WHERE id=$1", job_id) == "pending"
        assert await claim_recompute(pool) is None
        selection = await read_face_selection(pool)
        await acknowledge_face_selection(pool, "api", selection)
        assert await claim_recompute(pool) is None
        await acknowledge_face_selection(pool, "embedder", selection, "cannot load")
        assert await claim_recompute(pool) is None
        await acknowledge_face_selection(pool, "embedder", selection)
        assert (await fr._load_settings(pool)).recompute_job_id == str(job_id)
        stop = asyncio.Event()
        calls = []
        async def execute(p, jid, model, detector):
            calls.append((jid, model, detector))
            assert await p.fetchval("SELECT status FROM face_recompute_jobs WHERE id=$1", jid) == "running"
            await p.execute("UPDATE face_recompute_jobs SET status='done' WHERE id=$1", jid)
            stop.set()
        await run_recompute_queue(pool, stop, execute)
        assert calls == [(job_id, "model-b", "yunet")]
    run(pg, body)


def test_new_selections_supersede_pending_jobs_and_only_one_worker_claims(pg, save):
    async def body(pool):
        first = await save(pool, "model-b")
        second = await save(pool, "model-c")
        assert first.recompute_job_id != second.recompute_job_id
        assert await pool.fetchval("SELECT status FROM face_recompute_jobs WHERE id=$1", UUID(first.recompute_job_id)) == "cancelled"
        await activate(pool)
        threshold = await save(pool, "model-c", .7)
        assert threshold.recompute_job_id == second.recompute_job_id
        assert await claim_recompute(pool) is None
        await activate(pool)
        claims = await asyncio.gather(claim_recompute(pool), claim_recompute(pool))
        assert sum(c is not None for c in claims) == 1
        assert await pool.fetchval("SELECT count(*) FROM face_recompute_jobs WHERE status='running'") == 1
    run(pg, body)


def test_restart_resumes_current_job_but_preserves_a_newer_pending_selection(pg, save):
    async def body(pool):
        await save(pool, "model-b")
        await activate(pool)
        first = await claim_recompute(pool)
        await pool.execute("UPDATE face_recompute_jobs SET processed=3 WHERE id=$1", first["id"])
        await recover_recomputes(pool)
        resumed = await claim_recompute(pool)
        assert resumed["id"] == first["id"] and resumed["processed"] == 0
        second = await save(pool, "model-c")
        await recover_recomputes(pool)
        assert await pool.fetchval("SELECT status FROM face_recompute_jobs WHERE id=$1", first["id"]) == "failed"
        await activate(pool)
        assert (await claim_recompute(pool))["id"] == UUID(second.recompute_job_id)
    run(pg, body)


def test_failed_activation_can_retry_its_durable_pending_job(pg, save):
    async def body(pool):
        first = await save(pool)
        await acknowledge_face_selection(pool, "api", await read_face_selection(pool), "load failed")
        assert await claim_recompute(pool) is None
        second = await save(pool)
        assert second.recompute_job_id != first.recompute_job_id
        await activate(pool)
        assert (await claim_recompute(pool))["id"] == UUID(second.recompute_job_id)
    run(pg, body)


def test_worker_reconciles_a_job_when_its_completion_write_was_lost(pg, save):
    async def body(pool):
        settings = await save(pool)
        await activate(pool)
        stop = asyncio.Event()
        calls = []
        async def execute(p, job_id, model, detector):
            calls.append(job_id)
            if len(calls) == 2:
                await p.execute("UPDATE face_recompute_jobs SET status='done' WHERE id=$1", job_id)
                stop.set()
        await run_recompute_queue(pool, stop, execute)
        assert calls == [UUID(settings.recompute_job_id)] * 2
    run(pg, body)


def test_manual_recompute_is_atomically_queued_once(pg):
    async def body(pool):
        await activate(pool)
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
        results = await asyncio.gather(fr.start_recompute(req, SimpleNamespace(id=None)),
                                       fr.start_recompute(req, SimpleNamespace(id=None)), return_exceptions=True)
        assert sum(isinstance(r, fr.RecomputeStartOut) for r in results) == 1
        assert sum(isinstance(r, fr.HTTPException) and r.status_code == 409 for r in results) == 1
        assert await pool.fetchval("SELECT count(*) FROM face_recompute_jobs WHERE status='pending'") == 1
    run(pg, body)


def test_superseded_native_result_cannot_overwrite_references(pg, tmp_path, monkeypatch, save):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setenv("BABA_MEDIA_PATH", str(tmp_path))
    (tmp_path / "reference.jpg").write_bytes(b"test")
    monkeypatch.setattr(fr, "make_face_stack_for_model", lambda **_: (object(), "yunet", "model-b"))
    def reembed(*_):
        entered.set()
        assert release.wait(3)
        return np.ones(512, np.float32), None, (112, 112)
    monkeypatch.setattr(fr, "_reembed", reembed)
    async def body(pool):
        gid = uuid4()
        await pool.execute("INSERT INTO identity_labels(global_id, name) VALUES($1,'Test')", gid)
        rid = await pool.fetchval(
            "INSERT INTO identity_reference_photos(global_id, photo_path, source, face_embedding_model) "
            "VALUES($1,'reference.jpg','upload','original') RETURNING id", gid,
        )
        await save(pool)
        await activate(pool)
        job = await claim_recompute(pool)
        task = spawn(fr._run_recompute(pool, job["id"], "model-b", "yunet"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await save(pool, "model-c")
        finally:
            release.set()
            await task
        assert await pool.fetchval("SELECT face_embedding_model FROM identity_reference_photos WHERE id=$1", rid) == "original"
        assert await pool.fetchval("SELECT status FROM face_recompute_jobs WHERE id=$1", job["id"]) == "cancelled"
    run(pg, body)
