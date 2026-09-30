from __future__ import annotations

import asyncio
import logging
import time

import asyncpg
import msgspec
import nats
from baba_core import DECODERS, RING_PIXEL_FORMAT, StatsCollector, VideoDecoder
from baba_core.nats_conn import connect as nats_connect
from baba_core.nats_conn import drain_quietly
from baba_core.pg_listen import ResilientListener
from baba_core.wire import SUBJECT_TELEMETRY_TEMPLATE, ActivityMessage
from home_core.tasks import spawn

from baba_ingestor.config import CameraSpec, SupervisorConfig
from baba_ingestor.worker import CameraWorker

log = logging.getLogger(__name__)


async def _load_cameras(conn: asyncpg.Connection, go2rtc_rtsp_base: str) -> list[CameraSpec]:
    rows = await conn.fetch(
        """
        SELECT id::text AS id, slug, target_fps, idle_fps, downscale_max_edge, analysis_stream
        FROM cameras
        WHERE enabled
        """
    )
    return [
        CameraSpec(
            id=r["id"],
            slug=r["slug"],
            stream_url=(
                f"{go2rtc_rtsp_base}/{r['slug']}_sub"
                if r["analysis_stream"] == "sub"
                else f"{go2rtc_rtsp_base}/{r['slug']}"
            ),
            target_fps=r["target_fps"],
            downscale_max_edge=r["downscale_max_edge"],
            idle_fps=r["idle_fps"],
        )
        for r in rows
    ]


class IngestorSupervisor:
    """Owns one CameraWorker per enabled row in `cameras`. Reacts to NOTIFY
    messages on `cameras_changed` to reconcile workers without restart."""

    def __init__(self, config: SupervisorConfig, *, stats: StatsCollector | None = None) -> None:
        self._config = config
        self._stats = stats
        self._workers: dict[str, CameraWorker] = {}  # keyed by camera slug
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._reconcile_event = asyncio.Event()
        # go2rtc names whose source was swapped under a reader already on them.
        self._source_changed: set[str] = set()
        self._reconcile_lock = asyncio.Lock()
        self._decoder_cls: type[VideoDecoder] | None = None
        # One shared NATS subscription for the tracker's per-camera activity
        # verdicts (baba.activity.*) — routed to the matching worker, which
        # uses them to switch between target_fps and idle_fps.
        self._activity_nc: nats.NATS | None = None
        self._activity_decoder = msgspec.msgpack.Decoder(ActivityMessage)

    async def start(self) -> None:
        # Bind the decoder once at startup. The same class is used for every
        # camera worker — switching backends mid-flight is not a goal, and
        # would require restarting workers anyway.
        registry = DECODERS
        self._decoder_cls = registry.select([self._config.decoder_backend])
        # The ring format is the variant's contract with the detector (NV12 on
        # the GPU variants, colour-converted on the GPU; RGB on cpu). A decoder
        # writing the other one would have every frame dropped downstream.
        emits = self._decoder_cls.capabilities.output_pixel_format
        ring_format = RING_PIXEL_FORMAT[self._config.variant]
        if emits != ring_format:
            raise RuntimeError(
                f"decoder {self._config.decoder_backend!r} writes {emits}, but the "
                f"{self._config.variant} frame ring carries {ring_format}. "
                f"Set BABA_DECODER_BACKEND to one of: "
                f"{','.join(registry.names(only_available=True))}."
            )
        log.info("video decoder: %s (writes %s)", self._config.decoder_backend, emits)

        self._pool = await asyncpg.create_pool(self._config.dsn, min_size=1, max_size=4)
        # Resilient LISTEN: runs the initial reconcile, then reconnects +
        # re-reconciles after any DB drop so cameras added while the listener
        # was down still get a worker (the recurring "added but ignored" bug).
        self._listener = ResilientListener(
            self._config.dsn,
            ["cameras_changed", "go2rtc_source_changed"],
            on_notify=self._on_notify,
            on_connect=self._reconcile,
            name="ingestor-listen",
        )
        await self._listener.start()
        spawn(self._reconcile_loop(), name="ingestor-reconcile-loop")

        # Adaptive-fps feedback loop: the tracker publishes a per-tick scene
        # activity verdict per camera; workers with idle_fps configured use it
        # to decimate their output rate. nats-py reconnects internally, and a
        # worker that stops hearing verdicts fails toward FULL rate (see
        # CameraWorker), so a dropped subscription degrades cost, not coverage.
        self._activity_nc = await nats_connect(self._config.nats_url, name="ingestor-activity")
        await self._activity_nc.subscribe("baba.activity.*", cb=self._on_activity)
        spawn(self._telemetry_loop(), name="ingestor-telemetry")

        log.info(
            "supervisor started: %d worker(s) running",
            len(self._workers),
        )

    async def _telemetry_loop(self) -> None:
        """Drain each worker's adaptive-rate window counters once a minute onto
        the telemetry bus — duty cycle (active vs idle seconds), transition
        count and frame counters per camera."""
        encoder = msgspec.msgpack.Encoder()
        last = time.monotonic()
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            window = now - last
            last = now
            if self._activity_nc is None:
                continue
            for slug, worker in list(self._workers.items()):
                try:
                    payload = worker.drain_telemetry(window)
                    await self._activity_nc.publish(
                        SUBJECT_TELEMETRY_TEMPLATE.format(source="ingestor", camera_id=slug),
                        encoder.encode(payload),
                    )
                except Exception:
                    log.exception("telemetry flush failed for %s", slug)

    async def _on_activity(self, msg) -> None:
        try:
            wire = self._activity_decoder.decode(msg.data)
        except Exception:
            log.exception("undecodable activity message on %s", msg.subject)
            return
        worker = self._workers.get(wire.camera_id)
        if worker is not None:
            worker.note_activity(wire.active)

    async def stop(self) -> None:
        log.info("supervisor stopping: %d worker(s)", len(self._workers))
        if self._activity_nc is not None:
            await drain_quietly(self._activity_nc)
        if self._listener is not None:
            await self._listener.stop()
        await asyncio.gather(*(w.stop() for w in self._workers.values()))
        self._workers.clear()
        if self._pool is not None:
            await self._pool.close()

    def _on_notify(self, channel: str, payload: str) -> None:
        log.info("%s: %s", channel, payload)
        if channel == "go2rtc_source_changed":
            self._source_changed.add(payload)
        self._reconcile_event.set()

    async def _reconcile_loop(self) -> None:
        while True:
            await self._reconcile_event.wait()
            self._reconcile_event.clear()
            # Coalesce bursts of NOTIFY into a single reconcile pass.
            await asyncio.sleep(0.2)
            try:
                await self._reconcile()
            except Exception:
                log.exception("reconcile failed; will retry on next NOTIFY")

    async def _reconcile(self) -> None:
        async with self._reconcile_lock:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                desired = await _load_cameras(conn, self._config.go2rtc_rtsp_base)
            desired_by_slug = {c.slug: c for c in desired}
            moved, self._source_changed = self._source_changed, set()

            # Stop workers for cameras that disappeared or got disabled.
            stale = [slug for slug in self._workers if slug not in desired_by_slug]
            for slug in stale:
                log.info("removing worker for %s (no longer enabled)", slug)
                worker = self._workers.pop(slug)
                await worker.stop()

            # Restart workers whose config changed; start new workers.
            for slug, spec in desired_by_slug.items():
                existing = self._workers.get(slug)
                if existing is None:
                    log.info("starting worker for %s", slug)
                    w = CameraWorker(
                        spec,
                        self._config.nats_url,
                        self._decoder_cls,
                        stats=self._stats,
                    )
                    w.start()
                    self._workers[slug] = w
                elif existing.spec != spec or spec.stream_url.rsplit("/", 1)[-1] in moved:
                    log.info("config or source changed for %s; restarting worker", slug)
                    await existing.stop()
                    w = CameraWorker(
                        spec,
                        self._config.nats_url,
                        self._decoder_cls,
                        stats=self._stats,
                    )
                    w.start()
                    self._workers[slug] = w

            if self._stats is not None:
                self._stats.set_gauge("cameras", len(self._workers))
