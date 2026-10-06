import asyncio
import threading

import asyncpg
import pytest
from baba_api import face_activation as activation
from baba_core.face_settings import acknowledge_face_selection, read_face_selection
from fastapi import FastAPI


async def select(pool, *, model=None, threshold=None):
    await pool.execute(
        "UPDATE face_recognition_settings SET revision=revision+1, "
        "model_key=COALESCE($1,model_key), match_threshold=COALESCE($2,match_threshold), "
        "api_revision=NULL, embedder_revision=NULL, api_error=NULL, embedder_error=NULL",
        model, threshold,
    )
    return await read_face_selection(pool)


async def wait_for_ack(pool, revision, *, failed=False):
    async with asyncio.timeout(3):
        while True:
            row = await pool.fetchrow(
                "SELECT revision, api_revision, api_error FROM face_recognition_settings"
            )
            if row["revision"] == revision and (
                row["api_error"] if failed else row["api_revision"] == revision
            ):
                return row
            await asyncio.sleep(.005)


def run(pg, monkeypatch, body, factory=None):
    monkeypatch.setenv("BABA_FACE_DETECTOR_MODEL", "/models/yunet.onnx")
    monkeypatch.setattr(activation, "make_face_stack_for_model", factory or (
        lambda **kw: (object(), kw["detector_key"], kw["model_key"])
    ))

    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        app = FastAPI()
        worker = activation.ApiFaceActivation(app, pool, pg)
        try:
            await worker.start()
            await body(worker, app.state, pool)
        finally:
            await worker.stop()
            await pool.close()

    asyncio.run(main())


def test_threshold_revision_reuses_loaded_models_and_waits_for_embedder(pg, monkeypatch):
    loads = []

    def factory(**kw):
        loads.append(kw["model_key"])
        return object(), kw["detector_key"], kw["model_key"]

    async def body(worker, state, pool):
        original = state.face_stack
        await acknowledge_face_selection(pool, "embedder", await read_face_selection(pool))
        selected = await select(pool, threshold=.25)
        await wait_for_ack(pool, selected.revision)
        assert len(loads) == 1 and state.face_stack is original
        assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") < selected.revision
        await acknowledge_face_selection(pool, "embedder", selected)
        row = await pool.fetchrow(
            "SELECT active_revision, active_match_threshold FROM face_recognition_settings"
        )
        assert row["active_revision"] == selected.revision
        assert row["active_match_threshold"] == pytest.approx(.25)

    run(pg, monkeypatch, body, factory)


def test_models_arriving_after_startup_activate_without_a_notification(pg, monkeypatch):
    available = threading.Event()
    monkeypatch.setattr(activation.ApiFaceActivation, "_RETRY_INTERVAL_S", .02)

    def factory(**kw):
        return (object() if available.is_set() else None), kw["detector_key"], kw["model_key"]

    async def body(worker, state, pool):
        selected = await read_face_selection(pool)
        await wait_for_ack(pool, selected.revision, failed=True)
        assert state.face_stack is None
        await acknowledge_face_selection(pool, "embedder", selected)
        available.set()
        await wait_for_ack(pool, selected.revision)
        assert state.face_stack is not None and state.face_stack_error is None
        assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") == selected.revision

    run(pg, monkeypatch, body, factory)


def test_notifications_coalesce_and_superseded_loads_cannot_activate(pg, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    loads = []

    def factory(**kw):
        model = kw["model_key"]
        loads.append(model)
        if model == "model-b":
            entered.set()
            assert release.wait(3)
        return object(), kw["detector_key"], model

    async def body(worker, state, pool):
        try:
            await select(pool, model="model-b")
            assert await asyncio.to_thread(entered.wait, 2)
            task = worker._task
            selected = await select(pool, model="model-c")
            for _ in range(100):
                worker.request()
            assert worker._task is task
            release.set()
            await wait_for_ack(pool, selected.revision)
            assert loads[1:] == ["model-b", "model-c"]
            assert state.face_loaded_pair == (selected.detector_key, "model-c")
            assert await pool.fetchval("SELECT active_revision FROM face_recognition_settings") == 0
            await acknowledge_face_selection(pool, "embedder", selected)
            assert await pool.fetchval("SELECT active_model_key FROM face_recognition_settings") == "model-c"
        finally:
            release.set()

    run(pg, monkeypatch, body, factory)


def test_stop_retains_native_load_ownership_and_prevents_late_database_writes(pg, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    loads = []

    def factory(**kw):
        model = kw["model_key"]
        loads.append(model)
        if model == "model-b":
            entered.set()
            assert release.wait(3)
        return object(), kw["detector_key"], model

    async def body(worker, state, pool):
        try:
            initial_pair = state.face_loaded_pair
            selected = await select(pool, model="model-b")
            assert await asyncio.to_thread(entered.wait, 2)
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(.01)
            assert worker._stopping and not stopping.done()
            worker.request()
            release.set()
            await asyncio.wait_for(stopping, timeout=1)
            assert state.face_loaded_pair == initial_pair
            assert await pool.fetchval("SELECT api_revision FROM face_recognition_settings") is None
            await pool.close()
            worker.request()
            await worker.refresh()
            assert loads[-1] == "model-b" and len(loads) == 2
            assert worker._task is None
            assert selected.model_key == "model-b"
        finally:
            release.set()

    run(pg, monkeypatch, body, factory)


@pytest.mark.parametrize("failure", ["empty", "wrong-pair", "exception"])
def test_failed_load_is_visible_and_same_revision_can_be_retried(pg, monkeypatch, failure):
    async def body(worker, state, pool):
        good = activation.make_face_stack_for_model

        def broken(**kw):
            if failure == "exception":
                raise RuntimeError("accelerator unavailable")
            return (None if failure == "empty" else object()), "wrong-detector", kw["model_key"]

        monkeypatch.setattr(activation, "make_face_stack_for_model", broken)
        selected = await select(pool, model="model-b")
        row = await wait_for_ack(pool, selected.revision, failed=True)
        assert row["api_error"] == state.face_stack_error
        assert state.face_stack is None and state.face_model_key is None
        assert state.face_loaded_pair is None and row["api_revision"] is None
        monkeypatch.setattr(activation, "make_face_stack_for_model", good)
        worker.request()
        await wait_for_ack(pool, selected.revision)
        assert state.face_model_key == "model-b" and state.face_stack_error is None

    run(pg, monkeypatch, body)


def test_missing_detector_clears_all_loaded_state_and_recovers(pg, monkeypatch):
    async def body(worker, state, pool):
        original = state.face_stack
        monkeypatch.delenv("BABA_FACE_DETECTOR_MODEL")
        selected = await select(pool, threshold=.25)
        row = await wait_for_ack(pool, selected.revision, failed=True)
        assert row["api_error"] == state.face_stack_error == "Face detector is not configured"
        assert state.face_stack is None and state.face_model_key is None
        assert state.face_loaded_pair is None
        monkeypatch.setenv("BABA_FACE_DETECTOR_MODEL", "/models/yunet.onnx")
        worker.request()
        await wait_for_ack(pool, selected.revision)
        assert state.face_stack is not None and state.face_stack is not original
        assert state.face_stack_error is None

    run(pg, monkeypatch, body)


def test_transient_settings_read_failure_is_retried_without_another_notification(pg, monkeypatch):
    async def body(worker, state, pool):
        read = activation.read_face_selection
        attempts = 0

        async def flaky(pool):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database unavailable")
            return await read(pool)

        monkeypatch.setattr(activation, "read_face_selection", flaky)
        selected = await select(pool, model="model-b")
        worker.request()
        await wait_for_ack(pool, selected.revision)
        assert attempts >= 2 and state.face_model_key == "model-b"

    run(pg, monkeypatch, body)
