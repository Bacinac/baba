from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from baba_core.stats import StatsCollector

from baba_event_manager._state import TrackState

log = logging.getLogger("baba.event-manager")
Finalizer = Callable[[str, int, TrackState], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class _PendingTrack:
    camera: str
    local_id: int
    record: TrackState
    source: TrackState
    parked: bool


class FinalizationQueue:
    def __init__(self, stats: StatsCollector) -> None:
        self._stats = stats
        self._pending: dict[UUID, _PendingTrack] = {}
        self._lock = asyncio.Lock()
        self._update_gauge()

    def __len__(self) -> int:
        return len(self._pending)

    def parked_visit_closed(self, rec: TrackState | None) -> bool:
        if rec is None:
            return False
        pending = self._pending.get(rec.db_track_id)
        return rec.parked_finalized or (pending is not None and pending.parked)

    def enqueue(
        self, camera: str, local_id: int, rec: TrackState, *, parked: bool = False
    ) -> UUID:
        if rec.db_track_id is None:
            rec.db_track_id = uuid4()
        if rec.db_track_id not in self._pending:
            self._pending[rec.db_track_id] = _PendingTrack(
                camera, local_id, copy.deepcopy(rec), rec, parked
            )
            self._update_gauge()
        return rec.db_track_id

    async def drain(self, finalize: Finalizer) -> None:
        async with self._lock:
            for track_id, item in list(self._pending.items()):
                try:
                    await finalize(item.camera, item.local_id, item.record)
                except Exception:
                    log.exception(
                        "finalization pending cam=%s tid=%s", item.camera, item.local_id
                    )
                    continue
                if item.parked:
                    item.source.parked_finalized = True
                    item.source.inside_zones.clear()
                    item.source.enter_emitted.clear()
                    item.source.dwell_emitted.clear()
                self._pending.pop(track_id)
                self._update_gauge()

    async def finish(
        self, finalize: Finalizer, *, timeout_s: float = 10.0, retry_s: float = 1.0
    ) -> None:
        try:
            async with asyncio.timeout(timeout_s):
                while self._pending:
                    await self.drain(finalize)
                    if self._pending:
                        await asyncio.sleep(retry_s)
        except TimeoutError as exc:
            raise RuntimeError(
                f"shutdown has {len(self._pending)} uncommitted tracks"
            ) from exc

    def _update_gauge(self) -> None:
        self._stats.set_gauge("finalization_pending", len(self._pending))
