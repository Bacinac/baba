"""In-process runtime stats for pipeline services, shipped over NATS.

BABA deliberately does not run Prometheus/Grafana — at single-box, ~8-camera
scale that's a parallel observability stack with its own UI. Instead each
service accumulates a few cheap counters / gauges / timers in-process and
publishes a compact snapshot to NATS `baba.stats.<service>` every few seconds.
The API subscribes to `baba.stats.*`, keeps the latest snapshot plus a short
in-memory rolling window per service, and the web UI (Settings → System)
renders it. No new container, no scrape endpoint, no extra dependency.

Usage (mirrors HealthMarker — one object, touched from the main loop):

    from baba_core import StatsCollector

    stats = StatsCollector(service="detector")
    await stats.start(nc)                 # spawns the periodic publisher
    ...
    stats.incr("frames_in")
    stats.set_gauge("queue_depth", batcher.pending)
    with stats.timer("infer_ms"):
        output = backend.infer(tensor)
    stats.observe("batch_size", len(batch))
    ...
    await stats.stop()                    # on shutdown

All mutators are cheap and thread-safe (a single uncontended lock), so a
service may also `observe()` from a worker thread (e.g. inference run via
`asyncio.to_thread`). Counters are cumulative; the snapshot also carries the
per-second rate computed over the just-elapsed publish interval so the UI can
show "fps in" etc. without diffing successive snapshots itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Iterator

import msgspec
import nats.errors
from home_core.tasks import spawn

log = logging.getLogger(__name__)

SUBJECT_PREFIX = "baba.stats"

# How many recent samples to keep per timing metric for percentile calc.
_TIMING_WINDOW = 512


class TimingSummary(msgspec.Struct, frozen=True):
    """Percentile summary of a timing/observation metric over the window."""

    count: int
    p50: float
    p95: float
    p99: float


class StatsSnapshot(msgspec.Struct, frozen=True):
    """One service's runtime stats at a point in time (the NATS wire message).

    Generic dicts on purpose: core doesn't own the per-service metric names,
    so adding a metric in one service needs no change here or in the API —
    the UI renders whatever keys arrive (with a small label map for the known
    ones). Decoded by the API with this same struct → single source of truth.
    """

    service: str
    instance: str
    ts_ns: int
    uptime_s: float
    interval_s: float
    counters: dict[str, int]
    rates: dict[str, float]
    gauges: dict[str, float]
    timings: dict[str, TimingSummary]
    # Non-numeric facts a service knows about itself and nothing else can
    # answer authoritatively — e.g. the detector's ACTUALLY-loaded model file.
    # Reading such things from another process's environment guesses; the
    # owner reports them. Same generic-dict contract as the metric maps.
    labels: dict[str, str] = {}
    last_error: str | None = None
    last_error_ns: int = 0


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile (q in [0,1]) on an already-sorted list."""
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    idx = round(q * (len(sorted_samples) - 1))
    return sorted_samples[idx]


class _LastErrorHandler(logging.Handler):
    """Feeds the collector whatever the service last failed at.

    Settings → System draws a red warning per service off `last_error`, and
    nothing ever set it: every service reported "no error, ever", which reads
    as all-clear and is the one thing a health panel must not do. Wiring it
    here rather than at each `log.exception` keeps one definition of what the
    last error was and covers every service through the one constructor they
    all call.

    The record is formatted to its message alone. Holding the record would
    hold its traceback, and with it every frame's locals, until the next error
    replaced it.
    """

    def __init__(self, collector: StatsCollector) -> None:
        super().__init__(level=logging.ERROR)
        self._collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self._collector.set_error(f"{record.name}: {record.getMessage()}")


class StatsCollector:
    """Accumulates counters/gauges/timers and publishes periodic snapshots."""

    def __init__(
        self,
        service: str,
        *,
        instance: str | None = None,
        timing_window: int = _TIMING_WINDOW,
    ) -> None:
        self.service = service
        # Single process per service today, but tag the instance so a future
        # sharded/queue-grouped detector fleet shows up as distinct cards.
        self.instance = instance or f"{socket.gethostname()}:{os.getpid()}"
        self._timing_window = timing_window

        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, deque[float]] = {}
        self._labels: dict[str, str] = {}
        self._last_error: str | None = None
        self._last_error_ns: int = 0

        self._started_ns = time.monotonic_ns()
        self._prev_counters: dict[str, int] = {}
        self._prev_snapshot_ns = self._started_ns

        self._encoder = msgspec.msgpack.Encoder()
        self._task: asyncio.Task | None = None
        logging.getLogger().addHandler(_LastErrorHandler(self))

    # --- mutators (cheap, thread-safe) -------------------------------------

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def set_label(self, name: str, value: str) -> None:
        """Publish a non-numeric fact about this service (see StatsSnapshot)."""
        with self._lock:
            self._labels[name] = value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        """Record a sample (latency in ms, batch size, …) for percentiles."""
        with self._lock:
            buf = self._timings.get(name)
            if buf is None:
                buf = deque(maxlen=self._timing_window)
                self._timings[name] = buf
            buf.append(float(value))

    @contextlib.contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Context manager: records elapsed wall time in milliseconds."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000.0)

    def set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._last_error_ns = time.time_ns()

    # --- snapshot / publish ------------------------------------------------

    def snapshot(self) -> StatsSnapshot:
        """Build a snapshot and roll the rate baseline forward.

        Computes per-second rates for every counter over the interval since
        the previous snapshot, then records the current counters as the new
        baseline. Intended to be called once per publish interval.
        """
        now_ns = time.monotonic_ns()
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            labels = dict(self._labels)
            timings: dict[str, TimingSummary] = {}
            for name, buf in self._timings.items():
                samples = sorted(buf)
                timings[name] = TimingSummary(
                    count=len(samples),
                    p50=_percentile(samples, 0.50),
                    p95=_percentile(samples, 0.95),
                    p99=_percentile(samples, 0.99),
                )
            last_error = self._last_error
            last_error_ns = self._last_error_ns
            prev = self._prev_counters
            prev_ns = self._prev_snapshot_ns
            self._prev_counters = counters
            self._prev_snapshot_ns = now_ns

        elapsed_s = max((now_ns - prev_ns) / 1e9, 1e-6)
        rates = {name: (value - prev.get(name, 0)) / elapsed_s for name, value in counters.items()}
        return StatsSnapshot(
            service=self.service,
            instance=self.instance,
            ts_ns=time.time_ns(),
            uptime_s=(now_ns - self._started_ns) / 1e9,
            interval_s=elapsed_s,
            counters=counters,
            rates=rates,
            gauges=gauges,
            timings=timings,
            labels=labels,
            last_error=last_error,
            last_error_ns=last_error_ns,
        )

    @property
    def subject(self) -> str:
        return f"{SUBJECT_PREFIX}.{self.service}"

    async def start(self, nc, *, interval: float = 5.0) -> None:
        """Spawn the periodic publisher task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = spawn(self._run(nc, interval), name=f"stats-{self.service}", log=log)

    async def _run(self, nc, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                payload = self._encoder.encode(self.snapshot())
                await nc.publish(self.subject, payload)
            except nats.errors.Error as e:
                # Never let a stats hiccup (NATS reconnecting, etc.) take down
                # the service — that would be observability causing an outage.
                log.debug("stats: publish failed: %s", e)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
