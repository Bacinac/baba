import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from baba_core.native import run_native
from baba_core.task_owner import TaskOwner, finish_on_cancel


def test_cancelled_native_call_retains_lock_and_dedicated_executor():
    entered, release = threading.Event(), threading.Event()
    context = contextvars.ContextVar("request")

    async def main():
        context.set("camera-update")
        lock = asyncio.Lock()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-osnet") as executor:
            def native():
                entered.set()
                assert release.wait(3)
                return context.get(), threading.current_thread().name

            result = await run_native(lambda: (context.get(), threading.current_thread().name), executor=executor)
            assert result[0] == "camera-update"
            assert result[1].startswith("camera-osnet")

            async def process():
                async with lock:
                    await run_native(native, executor=executor)

            task = asyncio.create_task(process())
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
                assert lock.locked()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not lock.locked()

    asyncio.run(main())


def test_task_owner_waits_for_native_work_and_rejects_late_callbacks():
    entered, release = threading.Event(), threading.Event()

    async def main():
        owner = TaskOwner("test-service", logging.getLogger(__name__))
        finished = asyncio.Event()

        def native():
            entered.set()
            assert release.wait(3)

        async def refresh():
            try:
                await run_native(native)
            finally:
                finished.set()

        owner.spawn(refresh())
        assert await asyncio.to_thread(entered.wait, 2)
        stop = asyncio.create_task(owner.stop())
        try:
            await asyncio.sleep(.01)
            assert not stop.done()
            late = refresh()
            owner.spawn(late)
            assert late.cr_frame is None
        finally:
            release.set()
            await stop
        assert finished.is_set()
        assert not owner._tasks

    asyncio.run(main())


def test_task_owner_reports_failed_updates_and_releases_completed_tasks(caplog):
    async def main():
        owner = TaskOwner("test-config", logging.getLogger(__name__))

        async def refresh():
            raise RuntimeError("refresh failed")

        owner.spawn(refresh())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not owner._tasks
        await owner.stop()

    asyncio.run(main())
    assert "refresh failed" in caplog.text
    assert "test-config" in caplog.text


def test_cancellation_is_reported_after_the_mutation_commits():
    async def main():
        ready, release = asyncio.Event(), asyncio.Event()
        committed = []

        async def mutation():
            ready.set()
            await release.wait()
            committed.append("metadata")

        task = asyncio.create_task(finish_on_cancel(mutation(), name="retention", log=logging.getLogger(__name__)))
        await ready.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed == ["metadata"]

    asyncio.run(main())


def test_graceful_owner_shutdown_waits_for_accepted_jobs():
    async def main():
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []
        owner = TaskOwner("accepted-job", logging.getLogger(__name__))

        async def job():
            entered.set()
            await release.wait()
            completed.append("done")

        owner.spawn(job())
        await entered.wait()
        stop = asyncio.create_task(owner.stop(cancel=False))
        await asyncio.sleep(0)
        assert not stop.done()
        late = job()
        owner.spawn(late)
        assert late.cr_frame is None
        release.set()
        await stop
        assert completed == ["done"]

    asyncio.run(main())
