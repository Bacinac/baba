"""The detector's batcher: the newest frame of each camera, many cameras per GPU pass."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np
from baba_detector.batcher import FrameBatcher, PendingFrame


class Stats:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def incr(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


def batcher(size: int, wait_ms: int, stats: Stats | None = None) -> FrameBatcher:
    return FrameBatcher(SimpleNamespace(max_batch_size=size, max_batch_wait_ms=wait_ms), stats=stats)


def frame(cam: str, seq: int) -> PendingFrame:
    return PendingFrame(
        camera_id=cam,
        sequence=seq,
        timestamp_ns=seq,
        pixels=np.zeros((2, 2, 3), np.uint8),
        enqueued_ns=time.time_ns(),
    )


def ids(batch: list[PendingFrame]) -> list[tuple[str, int]]:
    return [(f.camera_id, f.sequence) for f in batch]


def test_a_camera_that_outpaces_the_gpu_keeps_only_its_newest_frame():
    async def main():
        stats = Stats()
        b = batcher(4, 0, stats)
        for f in (frame("porch", 1), frame("porch", 2), frame("yard", 1)):
            await b.submit(f)
        return ids(await b.next_batch()), stats.counts

    batch, counts = asyncio.run(main())
    assert batch == [("porch", 2), ("yard", 1)]
    assert counts == {"frames_coalesced": 1}


def test_a_full_batch_goes_without_waiting_out_the_window():
    async def main():
        b = batcher(2, 5000)
        pending = asyncio.create_task(b.next_batch())
        await b.submit(frame("porch", 1))
        await asyncio.sleep(0.01)
        await b.submit(frame("yard", 1))
        return ids(await asyncio.wait_for(pending, 1.0))

    assert asyncio.run(main()) == [("porch", 1), ("yard", 1)]


def test_the_window_runs_from_the_oldest_waiting_frame():
    async def main():
        b = batcher(4, 200)
        await b.submit(frame("porch", 1))
        t0 = time.monotonic()

        async def late():
            await asyncio.sleep(0.15)
            await b.submit(frame("yard", 1))

        latecomer = asyncio.create_task(late())
        batch = await b.next_batch()
        await latecomer
        return ids(batch), time.monotonic() - t0

    batch, elapsed = asyncio.run(main())
    assert batch == [("porch", 1), ("yard", 1)]
    assert 0.18 <= elapsed < 0.3


def test_more_cameras_than_a_batch_go_oldest_first_and_the_rest_follow_at_once():
    async def main():
        b = batcher(2, 0)
        for cam in ("gate", "porch", "yard"):
            await b.submit(frame(cam, 1))
        first = ids(await b.next_batch())
        second = ids(await asyncio.wait_for(b.next_batch(), 1.0))
        return first, second, b.pending

    assert asyncio.run(main()) == ([("gate", 1), ("porch", 1)], [("yard", 1)], 0)


def test_close_releases_a_waiting_consumer_and_refuses_new_frames():
    async def main():
        b = batcher(4, 0)
        waiting = asyncio.create_task(b.next_batch())
        await asyncio.sleep(0.01)
        b.close()
        released = await asyncio.wait_for(waiting, 1.0)
        await b.submit(frame("porch", 1))
        return released, b.pending, await b.next_batch()

    assert asyncio.run(main()) == ([], 0, [])
