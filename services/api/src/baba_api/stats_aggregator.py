"""Aggregates per-service runtime stats published on NATS `baba.stats.*`.

Each pipeline service publishes a `StatsSnapshot` (baba_core.stats) every few
seconds. This component holds one NATS subscription for the whole API, keeps
the latest snapshot per service plus a short in-memory rolling window for
sparklines, and exposes a view for `GET /system/metrics` + the Settings →
System page. Deliberately in-memory and ephemeral — operational liveness, not
durable history (that's what analytics/heatmap cover). No Prometheus, no extra
container, no new dependency.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import deque

import msgspec
import nats.errors
from baba_core import StatsSnapshot
from baba_core.nats_conn import connect as nats_connect
from baba_core.nats_conn import drain_quietly

log = logging.getLogger("baba.api.stats")

# ~30 min of history at the default 5s publish cadence.
_WINDOW = 360
# A snapshot older than this many seconds means the service stopped publishing
# (crashed, wedged, or NATS partitioned) — render its card as stale.
_STALE_AFTER_S = 20.0


class StatsAggregator:
    def __init__(
        self,
        nats_url: str,
        *,
        window: int = _WINDOW,
        stale_after_s: float = _STALE_AFTER_S,
    ) -> None:
        self._nats_url = nats_url
        self._window = window
        self._stale_after_s = stale_after_s
        self._decoder = msgspec.msgpack.Decoder(StatsSnapshot)
        self._latest: dict[str, StatsSnapshot] = {}
        self._history: dict[str, deque[dict]] = {}
        self._nc = None
        self._sub = None

    async def start(self) -> None:
        self._nc = await nats_connect(self._nats_url, name="api-stats")
        self._sub = await self._nc.subscribe("baba.stats.*", cb=self._on_msg)
        log.info("stats aggregator subscribed to baba.stats.*")

    async def _on_msg(self, msg) -> None:
        try:
            snap = self._decoder.decode(msg.data)
        except msgspec.DecodeError:
            log.debug("stats: undecodable snapshot on %s", msg.subject)
            return
        self._latest[snap.service] = snap
        hist = self._history.get(snap.service)
        if hist is None:
            hist = deque(maxlen=self._window)
            self._history[snap.service] = hist
        hist.append(
            {
                "ts_ns": snap.ts_ns,
                "rates": snap.rates,
                "gauges": snap.gauges,
                "timings_p95": {k: v.p95 for k, v in snap.timings.items()},
            }
        )


    def labels_for(self, service: str) -> dict[str, str]:
        """Non-numeric facts the given service reported about itself (see
        StatsSnapshot.labels). Empty until its first snapshot lands."""
        snap = self._latest.get(service)
        return dict(snap.labels) if snap is not None else {}

    def view(self) -> dict:
        """Snapshot of all services for the API response."""
        now_ns = time.time_ns()
        services = []
        for name in sorted(self._latest):
            snap = self._latest[name]
            age_s = max((now_ns - snap.ts_ns) / 1e9, 0.0)
            last_error_age_s = (now_ns - snap.last_error_ns) / 1e9 if snap.last_error_ns else None
            services.append(
                {
                    "service": snap.service,
                    "instance": snap.instance,
                    "uptime_s": snap.uptime_s,
                    "age_s": age_s,
                    "stale": age_s > self._stale_after_s,
                    "rates": snap.rates,
                    "gauges": snap.gauges,
                    "counters": snap.counters,
                    "timings": {
                        k: {"count": v.count, "p50": v.p50, "p95": v.p95, "p99": v.p99}
                        for k, v in snap.timings.items()
                    },
                    "last_error": snap.last_error,
                    "last_error_age_s": last_error_age_s,
                    "history": list(self._history.get(name, ())),
                }
            )
        return {"generated_ts_ns": now_ns, "services": services}

    async def stop(self) -> None:
        if self._sub is not None:
            with contextlib.suppress(nats.errors.Error):
                await self._sub.unsubscribe()
        await drain_quietly(self._nc)
