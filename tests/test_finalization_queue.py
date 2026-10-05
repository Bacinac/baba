import asyncio
from uuid import uuid4

import pytest
from baba_core.stats import StatsCollector
from baba_event_manager._state import TrackState
from baba_event_manager.finalization import FinalizationQueue


def record():
    rec = TrackState(1, 10, 10, 2, "car", .9, 5, (0, 0, 50, 100), 0, 50, 0, 100)
    rec.class_counts = {2: 5}
    rec.inside_zones = {uuid4(): 1}
    rec.enter_emitted = set(rec.inside_zones)
    rec.dwell_emitted = set(rec.inside_zones)
    return rec


def test_retry_keeps_the_original_uuid_and_observation_snapshot():
    async def main():
        stats = StatsCollector("test")
        queue = FinalizationQueue(stats)
        source = record()
        track_id = queue.enqueue("yard", 1, source)
        source.last_seen_ns = 20
        source.class_counts[2] = 10
        source.inside_zones.clear()
        assert queue.enqueue("yard", 1, source) == track_id
        attempts = []

        async def finalize(camera, local_id, rec):
            attempts.append((camera, local_id, rec.db_track_id, rec.last_seen_ns,
                             dict(rec.class_counts), dict(rec.inside_zones)))
            if len(attempts) == 1:
                raise RuntimeError("database unavailable")

        await queue.drain(finalize)
        assert len(queue) == stats.snapshot().gauges["finalization_pending"] == 1
        await queue.drain(finalize)
        assert not queue
        assert stats.snapshot().gauges["finalization_pending"] == 0
        assert attempts[0] == attempts[1]
        assert attempts[0][:5] == ("yard", 1, track_id, 10, {2: 5})
        assert len(attempts[0][5]) == 1

    asyncio.run(main())


def test_parked_visit_is_closed_while_pending_and_cleared_only_after_commit():
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        source = record()
        queue.enqueue("yard", 1, source, parked=True)
        assert queue.parked_visit_closed(source)
        assert not queue.parked_visit_closed(None)
        assert not queue.parked_visit_closed(record())

        async def fail(*args):
            raise RuntimeError("database unavailable")

        await queue.drain(fail)
        assert not source.parked_finalized
        assert source.inside_zones and source.enter_emitted and source.dwell_emitted

        async def commit(*args):
            pass

        await queue.drain(commit)
        assert source.parked_finalized and queue.parked_visit_closed(source)
        assert not source.inside_zones and not source.enter_emitted and not source.dwell_emitted

    asyncio.run(main())


def test_cancellation_retains_the_visit_and_releases_the_drain_lock():
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        source = record()
        track_id = queue.enqueue("yard", 1, source, parked=True)
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(queue.drain(blocked))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(queue) == 1 and not source.parked_finalized

        async def commit(camera, local_id, rec):
            assert rec.db_track_id == track_id

        await asyncio.wait_for(queue.drain(commit), timeout=1)
        assert not queue and source.parked_finalized

    asyncio.run(main())


def test_concurrent_drains_finalize_each_visit_once():
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        queue.enqueue("yard", 1, record())
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []

        async def finalize(camera, local_id, rec):
            attempts.append(rec.db_track_id)
            entered.set()
            await release.wait()

        first = asyncio.create_task(queue.drain(finalize))
        await entered.wait()
        second = asyncio.create_task(queue.drain(finalize))
        await asyncio.sleep(0)
        assert len(attempts) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        assert len(attempts) == 1 and not queue

    asyncio.run(main())


def test_enqueue_during_drain_remains_owned_for_the_next_attempt():
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        first_id = queue.enqueue("yard", 1, record())
        second = record()
        committed = []

        async def finalize(camera, local_id, rec):
            committed.append(rec.db_track_id)
            if rec.db_track_id == first_id:
                queue.enqueue("yard", 2, second)

        await queue.drain(finalize)
        assert len(queue) == 1 and committed == [first_id]
        await queue.drain(finalize)
        assert not queue and committed == [first_id, second.db_track_id]

    asyncio.run(main())


def test_shutdown_retries_until_every_owned_visit_commits():
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        source = record()
        track_id = queue.enqueue("yard", 1, source)
        attempts = []

        async def finalize(camera, local_id, rec):
            attempts.append(rec.db_track_id)
            if len(attempts) < 3:
                raise RuntimeError("database unavailable")

        await queue.finish(finalize, timeout_s=1, retry_s=0)
        assert attempts == [track_id] * 3 and not queue

    asyncio.run(main())


@pytest.mark.parametrize("blocked", [False, True])
def test_shutdown_deadline_reports_and_retains_uncommitted_visits(blocked):
    async def main():
        queue = FinalizationQueue(StatsCollector("test"))
        source = record()
        track_id = queue.enqueue("yard", 1, source, parked=True)

        async def finalize(*args):
            if blocked:
                await asyncio.Event().wait()
            raise RuntimeError("database unavailable")

        with pytest.raises(RuntimeError, match="shutdown has 1 uncommitted tracks"):
            await queue.finish(finalize, timeout_s=.02, retry_s=.01)
        assert len(queue) == 1 and not source.parked_finalized

        async def commit(camera, local_id, rec):
            assert rec.db_track_id == track_id

        await queue.finish(commit, timeout_s=1)
        assert not queue and source.parked_finalized

    asyncio.run(main())
