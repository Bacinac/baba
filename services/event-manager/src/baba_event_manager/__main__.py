"""Track-lifecycle → durable events.

What this service does in v1:
1. Subscribe to `baba.tracks.<slug>` NATS messages from the tracker
2. Keep per-camera, per-local-track-id state (first_seen, last_seen, class,
   max_confidence, n_observations, last_bbox)
3. A background sweeper finalizes tracks that haven't been seen for
   `track_timeout_ms` — meaning the tracker considers them lost and they're
   no longer in published messages
4. Finalize = INSERT into `tracks` table + INSERT into `events` table with
   kind='track_finalized'. Postgres NOTIFY 'events_new' fires automatically
   via existing trigger, so any /sse/events consumer sees it live.

What this service deliberately doesn't do yet:
- Zones / polygon membership (next iteration, needs zones schema first)
- Cross-camera re-ID (needs embedder)
- Recording clip linking (needs recorder)

These each plug in as additional event kinds without changing this loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

import asyncpg
import msgspec
import nats
from baba_core import (
    PET_GROUP,
    VEHICLE_GROUP,
    StatsCollector,
    canonical_class,
    drain_quietly,
    setup_logging,
)
from baba_core.classes import class_id_for_name
from baba_core.event_kinds import EventKind
from baba_core.face import set_canonical_face
from baba_core.nats_conn import connect as nats_connect
from baba_core.parked import parked_assignments
from baba_core.pg_listen import ResilientListener
from baba_core.pipeline_settings import Settings, publish_defaults
from baba_core.retention import ANONYMOUS, ENROLLED, tier_sql
from baba_core.runtime import run_service
from baba_core.tunables import REID_KEY, REID_TUNABLES
from baba_core.wire import SUBJECT_TELEMETRY_WILDCARD, TrackWire
from baba_core.wire import TracksMessage as _TracksMessage
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_event_manager._gating import (
    _ZONE_CLASS_REJECTED,
    _effective_motion_gate,
    _motion_gate_allows,
    _zone_class_rule,
)
from baba_event_manager._state import CameraInfo, CameraResolver, TrackState
from baba_event_manager.config import EventManagerConfig
from baba_event_manager.face_anchor import (
    FACE_ID_MIN_PX,
    anchor_sql,
    chain_enabled,
    chain_params,
)
from baba_event_manager.identity_rules import survives_rivals
from baba_event_manager.live_identity import LiveIdentityMatcher
from baba_event_manager.naming import Naming, name_track, reference_kind_for_class
from baba_event_manager.presence import PresenceRegistry
from baba_event_manager.snapshot import SnapshotBaker
from baba_event_manager.state_snapshot import StateSnapshotPublisher
from baba_event_manager.thumbs import ThumbnailCapture

if TYPE_CHECKING:
    # Runtime import is deferred to start() (line ~318) so shapely isn't
    # pulled at module import; this block only feeds the type annotation.
    from baba_event_manager.zones import ZonesResolver

log = logging.getLogger("baba.event-manager")

SUBJECT_TRACKS_IN = "baba.tracks.*"

# Embedder writes samples with `captured_at = now()` (DB clock), while the
# event-manager derives a track's [started_at, ended_at] from observed
# `timestamp_ns` (wall clock from the tracker). The two clocks agree but
# samples can land just after the observation that triggered them — pad
# the claim window so we don't miss the trailing few.
_CLAIM_WINDOW_SLACK = timedelta(seconds=2)

# How far a track's start may be back-dated to the tracker's first sighting of
# the underlying object (TrackWire.first_seen_ns). Covers a real entrance
# (birth gate + idle-fps ramp-up: a few seconds) with room to spare, while
# refusing to inherit the whole life of an object that lingered unqualified —
# that would invent a visit that never happened.
_FIRST_SEEN_BACKDATE_MAX_NS = 30 * 1_000_000_000

# Only (re)bake the box-thumbnail when an ACTIVE detection beats the best-so-far
# confidence by at least this margin — bounds the decode+encode to a handful of
# times over a track's life (confidence climbs then plateaus) instead of every
# frame that ties a new high.
_THUMB_CONF_MARGIN = 0.03

# A track whose bbox comes within this fraction of a frame edge is treated as
# entering/leaving rather than parked-in-place — suppresses the misleading
# object_parked a car emits while pausing at the gate on its way out.
_EDGE_MARGIN_FRAC = 0.015


def came_to_rest(
    last_state: str, cur_state: str, at_frame_edge: bool, already_emitted: bool
) -> bool:
    """Whether this observation is the moment the object stopped.

    A box on a frame edge is entering or leaving, so the edge suppresses the
    report — but only while the tracker still says `stationary`. Reaching
    `parked` takes sustained stillness, which a car on its way out never
    spends, and without that deferral a car parked with its bumper in the last
    few pixels of frame never files a visit at all.
    """
    if cur_state == "parked" and not already_emitted:
        return True
    return (
        last_state == "active"
        and cur_state in ("stationary", "parked")
        and not at_frame_edge
    )


@dataclass(frozen=True, slots=True)
class _PendingEvent:
    """A transition found under the state lock, filed after it is released."""

    kind: EventKind
    zone_id: Any
    track_id: int
    bbox: tuple[float, float, float, float]
    class_name: str
    db_track_id: UUID | None = None


def _note_observation(rec: TrackState, msg: _TracksMessage, t: TrackWire) -> None:
    # Liveness keeps advancing (finalize timeout); EVIDENCE only when the
    # subject was actually seen — see TrackState.
    rec.last_seen_ns = msg.timestamp_ns
    rec.last_evidence_ns = max(rec.last_evidence_ns, t.last_matched_ns or msg.timestamp_ns)
    rec.n_observations += 1
    rec.max_confidence = max(rec.max_confidence, t.confidence)
    rec.last_bbox = (t.x1, t.y1, t.x2, t.y2)
    cx = (t.x1 + t.x2) / 2.0
    cy = (t.y1 + t.y2) / 2.0
    rec.min_cx = min(rec.min_cx, cx)
    rec.max_cx = max(rec.max_cx, cx)
    rec.min_cy = min(rec.min_cy, cy)
    rec.max_cy = max(rec.max_cy, cy)


def zone_gate_verdict(
    touched_zone: bool, enter_fired: bool, came_to_rest: bool, is_person: bool
) -> str | None:
    """Which zone gate hides this track — "off-zone", "sub-dwell", or neither.

    Zones are how the operator declares what they care about, so a track that
    touched none of them is theirs to keep out of the timeline. `min_dwell_ms`
    is the narrower claim: it filters what PASSED THROUGH a zone, and a vehicle
    that came to rest did not pass through. Without that exception a car cannot
    park within sight of the camera it parks under — the zone rules that carry
    a dwell are `moving_only`, so the clock runs only while the car is still
    driving, and it stops seconds after entering.

    A person passes BOTH gates, for the same reason they already passed the
    motion gate: these thresholds are aimed at vehicles and at traffic clipping
    a corner of the frame, and a person on the property is the event this
    system exists for. Measured over thirty days on the gate camera, which
    watches a gap nobody stands in: of 143 people it followed and never
    recorded, 49 crossed a polygon too briefly for the five-second dwell and 94
    never touched one at all — so gating people on zones recorded one visitor
    in six. Letting both go costs 3.5 rows a day across all seven cameras, and
    the tool for "this part of the frame is not mine" remains the `ignore`
    zone, which the detector applies before any of this.
    """
    if is_person:
        return None
    if not touched_zone:
        return "off-zone"
    if not enter_fired and not came_to_rest:
        return "sub-dwell"
    return None


@dataclass(slots=True, frozen=True)
class _Closed:
    """A track that existed, as classification left it."""

    cam: CameraInfo
    start_ns: int
    end_ns: int
    movement_px: float
    class_votes: dict[str, int]
    # suppressed_reason when a gate hides it, else None
    hidden_by: str | None

    @property
    def lifetime_ms(self) -> int:
        return (self.end_ns - self.start_ns) // 1_000_000

    @property
    def started_at(self) -> datetime:
        return datetime.fromtimestamp(self.start_ns / 1e9, tz=UTC)

    @property
    def ended_at(self) -> datetime:
        return datetime.fromtimestamp(self.end_ns / 1e9, tz=UTC)


@dataclass(slots=True)
class _PendingTrack:
    camera: str
    local_id: int
    record: TrackState
    source: TrackState
    parked: bool


class EventManager:
    def __init__(self, config: EventManagerConfig) -> None:
        self._config = config
        # Operator-tunable thresholds. Env is the deployment default; the
        # `reid_defaults` row overrides it. On change the whole config is
        # REBUILT from the catalogue in one loop, so all ~45 read sites go
        # live at once and a new knob needs no wiring here — the alternative,
        # a setter per threshold, is how one gets forgotten.
        self._tunables = Settings(
            REID_TUNABLES, {n: getattr(config, n) for n in REID_TUNABLES}
        )
        self._stats = StatsCollector(service="event-manager")
        # state[camera_slug][local_track_id] = TrackState
        self._state: dict[str, dict[int, TrackState]] = {}
        self._state_lock = asyncio.Lock()
        self._pending_finalizations: dict[UUID, _PendingTrack] = {}
        self._finalization_lock = asyncio.Lock()
        self._pool: asyncpg.Pool | None = None
        self._resolver = CameraResolver()
        self._zones: ZonesResolver | None = None
        # Lazy-imported to avoid pulling shapely / heatmap modules at
        # import time before EventManagerConfig is built.
        from baba_event_manager.heatmap import HeatmapAccumulator

        self._heatmap = HeatmapAccumulator()
        self._listener: ResilientListener | None = None
        self._nc: nats.NATS | None = None
        # Authoritative per-camera snapshot for off-box consumers (DIDA
        # mirrors it and runs no sweeps of its own). Built once NATS is up.
        self._snapshot: StateSnapshotPublisher | None = None
        # Names people while they are still on screen, instead of at
        # finalize ~27 s later. Face only — see live_identity.py.
        self._live_identity: LiveIdentityMatcher | None = None
        # region_id -> {"slug", "state"} for the scene-status listener (the
        # NOTIFY carries only the id) and the snapshot seed. Who stands in an
        # occupied place is the registry's answer (place_occupancy → the
        # `parked` snapshot map), not anything held in RAM here.
        self._scene_regions: dict[str, dict] = {}
        self._stop = asyncio.Event()
        self._thumbs = ThumbnailCapture(
            Path(config.media_path),
            config.go2rtc_url,
            config.thumbnail_max_width,
            config.go2rtc_auth,
        )
        # Bakes the detection box onto the exact SHM frame it ran on, live
        # during the track — the primary thumbnail source (recording/live
        # snapshot are fallbacks). See snapshot.py.
        self._snapshot_baker = SnapshotBaker(config.thumbnail_max_width)
        # Per-zone cooldown bookkeeping: maps (zone_id, class_name, kind)
        # to the wall-clock ns of the last successfully emitted event.
        # Repeat events of the same shape inside `cooldown_s` are dropped
        # silently — prevents flooding the timeline when multiple tracks
        # enter the same area in quick succession (e.g. group of people
        # crossing a doorway).  Track-scoped state (`dwell_emitted` on
        # TrackState) handles per-track one-shot dwell separately.
        self._zone_event_last_ns: dict[tuple[Any, str, str], int] = {}

    async def run(self) -> None:
        # DB pool + initial camera map + zones
        from baba_event_manager.zones import ZonesResolver

        self._pool = await asyncpg.create_pool(self._config.dsn, min_size=1, max_size=4)
        self._zones = ZonesResolver()

        # Listen for camera + zone changes so add/remove camera or zone edits
        # are picked up live. Resilient: the initial refresh runs via
        # on_connect, and it re-runs after any DB drop so a zone/camera added
        # while the listener was down is reconciled (the recurring "zone
        # added but event-manager still says no-zones" bug).
        self._listener = ResilientListener(
            self._config.dsn,
            # scene_* too: the snapshot carries `scenes`, and the status
            # NOTIFY is the only live signal that a region flipped (config
            # changes fire scene_regions_changed, transitions land in events).
            [
                "cameras_changed",
                "zones_changed",
                "scene_regions_changed",
                "scene_status_changed",
                # Pipeline thresholds moved out of env into this row; without
                # the channel they would be UI-only again.
                "app_settings_changed",
                # The face slider used to be read once, at boot. Saving it
                # changed the row and the UI and nothing else, so the pipeline
                # went on matching at the old number until someone happened to
                # restart this service.
                "face_recognition_changed",
            ],
            on_notify=self._on_notify,
            on_connect=self._reconcile,
            name="event-manager-listen",
        )
        await self._listener.start()
        async with self._pool.acquire() as _c:
            await publish_defaults(_c, self._tunables)
            await self._refresh_tunables()

        # NATS subscription
        self._nc = await nats_connect(self._config.nats_url, name="event-manager")
        self._snapshot = StateSnapshotPublisher(self._nc)
        # Given a PROVIDER, not values: the matcher reads whatever the config
        # currently holds, so an operator edit reaches it without a setter.
        self._live_identity = LiveIdentityMatcher(lambda: self._config)
        self._presence = PresenceRegistry(self._pool)
        # Seed scene state now the publisher exists — the listener's
        # on_connect reconcile ran before NATS was up.
        async with self._pool.acquire() as _c:
            await self._refresh_scene_regions(_c)
            await self._refresh_light_conditions(_c)
        decoder = msgspec.msgpack.Decoder(_TracksMessage)

        async def handler(msg) -> None:
            try:
                wire = decoder.decode(msg.data)
                await self._observe(wire)
            except Exception:
                log.exception("track handler failed")

        await self._nc.subscribe(SUBJECT_TRACKS_IN, cb=handler)
        await self._stats.start(self._nc)
        log.info("subscribed to %s", SUBJECT_TRACKS_IN)

        # Telemetry flight recorder: persist every per-camera minute bucket
        # (detector/tracker/ingestor) and run the deterministic watchers
        # that open/close telemetry_incidents over the recorded window.
        from baba_event_manager.telemetry import TelemetrySink

        telemetry_sink = TelemetrySink(self._pool)
        await self._nc.subscribe(SUBJECT_TELEMETRY_WILDCARD, cb=telemetry_sink.handle)
        telemetry_retention = spawn(
            telemetry_sink.retention_loop(), name="event-manager-telemetry-retention"
        )
        telemetry_watch = spawn(telemetry_sink.watch_loop(), name="event-manager-telemetry-watch")
        log.info("subscribed to %s", SUBJECT_TELEMETRY_WILDCARD)

        # Sweeper finalizes stale tracks
        sweeper = spawn(self._sweep_loop(), name="event-manager-sweeper")
        parked = spawn(self._parked_loop(), name="event-manager-parked")
        presence = spawn(self._presence_loop(), name="event-manager-presence")
        # Cleanup task reaps old per-observation embedding samples so the
        # table doesn't grow without bound. The canonical embedding lives
        # on `tracks.embedding` and is unaffected.
        cleanup = spawn(self._cleanup_loop(), name="event-manager-cleanup")
        # Heatmap flush task — periodic UPSERT of in-memory cell counts
        # into heatmap_daily. Restart loses at most the unflushed window.
        heatmap_flush = spawn(
            self._heatmap_flush_loop(),
            name="event-manager-heatmap",
        )
        # Re-ID resweep — re-links anonymous faces that finalize missed
        # (late face_embedding / reference enrolled after the fact).
        resweep = spawn(
            self._reid_resweep_loop(),
            name="event-manager-reid-resweep",
        )
        # State heartbeat — re-publish every camera's current snapshot on a slow
        # cadence so a mirror that connected/restarted during a quiet stretch
        # self-corrects, instead of latching stale state until the next real
        # change (which on a quiet camera may be hours away).
        heartbeat = spawn(
            self._state_heartbeat_loop(),
            name="event-manager-state-heartbeat",
        )

        # Independent health touch — fires while the asyncio loop is
        # responsive even if no tracks are coming in (legitimate quiet
        # period). Compose healthcheck checks the file mtime is <30s old.
        tick = spawn(
            HealthMarker("baba", "event-manager").run_loop(), name="event-manager-health", log=log
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        await self._stop.wait()

        for task in (
            sweeper,
            parked,
            presence,
            cleanup,
            heatmap_flush,
            resweep,
            heartbeat,
            telemetry_retention,
            telemetry_watch,
            tick,
        ):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # Finalize all in-flight tracks on shutdown so we don't drop them.
        await self._finalize_all_pending()
        await self._stats.stop()
        await drain_quietly(self._nc)
        if self._listener is not None:
            await self._listener.stop()
        await self._thumbs.close()
        self._snapshot_baker.close()
        await self._pool.close()

    async def _reconcile(self) -> None:
        # Full refresh of the camera map + zones. Runs on every (re)connect
        # of the LISTEN, so anything changed while the listener was down is
        # caught. Idempotent + cheap at v1 scale.
        assert self._pool is not None and self._zones is not None
        async with self._pool.acquire() as c:
            await self._resolver.refresh(c)
            await self._zones.refresh(c)
            await self._refresh_scene_regions(c)
            await self._refresh_light_conditions(c)
        await self._refresh_tunables()
        await self._refresh_face_threshold()

    async def _refresh_light_conditions(self, c) -> None:
        """Seed + refresh each camera's illumination band on the snapshot.

        Read from `cameras` rather than pushed from the telemetry sink that
        writes it: the row is the single source, and its NOTIFY already carries
        every write here — including a band switch, which updates that column.
        A hand edit therefore propagates the same way an automatic switch does.
        """
        if self._snapshot is None:
            return
        for row in await c.fetch(
            "SELECT id, name, light_condition FROM cameras WHERE enabled"
        ):
            await self._snapshot.update_light(
                str(row["id"]), row["name"], row["light_condition"]
            )

    async def _refresh_scene_regions(self, c) -> None:
        """Load region_id → {slug, state} for the status NOTIFY (which carries
        only the id) and to seed the snapshot with every region's CURRENT
        state. The seed matters: the snapshot republishes only on change, so
        without it a consumer joining a quiet scene would never learn a
        region's state."""
        rows = await c.fetch(
            """
            SELECT r.id, r.name, c.slug, s.published_state
            FROM scene_regions r
            JOIN cameras c ON c.id = r.camera_id
            LEFT JOIN scene_region_status s ON s.region_id = r.id
            WHERE r.enabled
            """
        )
        regions: dict[str, dict] = {}
        for r in rows:
            regions[str(r["id"])] = {
                "slug": r["slug"],
                "state": r["published_state"],
            }
        self._scene_regions = regions
        if self._snapshot is None:
            return
        for rid, region in regions.items():
            info = await self._resolver.get(region["slug"])
            if info is not None:
                await self._snapshot.update_scene(
                    str(info.id), info.name, rid, region["state"]
                )

    async def _on_scene_status(self, payload: str) -> None:
        if self._snapshot is None:
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        region_id = str(data.get("region_id"))
        region = self._scene_regions.get(region_id)
        if region is None:
            return  # region map not loaded yet / region disabled
        state = data.get("state")
        region["state"] = state
        info = await self._resolver.get(region["slug"])
        if info is not None:
            await self._snapshot.update_scene(str(info.id), info.name, region_id, state)

    def _on_notify(self, channel: str, payload: str) -> None:
        async def go():
            assert self._pool is not None and self._zones is not None
            if channel == "scene_status_changed":
                await self._on_scene_status(payload)
                return
            if channel == "face_recognition_changed":
                await self._refresh_face_threshold()
                return
            if channel == "app_settings_changed":
                await self._refresh_tunables()
                return
            async with self._pool.acquire() as c:
                if channel == "zones_changed":
                    await self._zones.refresh(c)
                elif channel == "scene_regions_changed":
                    await self._refresh_scene_regions(c)
                else:
                    await self._resolver.refresh(c)
                    # …and the illumination band, which lives on the same row and
                    # changes far more often than the row's identity does. Refreshing
                    # only the resolver here left the band on the snapshot frozen at
                    # whatever it held when this service last started: west sat at
                    # "normal" for two nights while _switch_light wrote `ir` into the
                    # very column this reads, and the consumer that arms a floodlight
                    # on darkness never saw a dark.
                    await self._refresh_light_conditions(c)

        spawn(go())

    async def _refresh_tunables(self) -> None:
        """Re-read `reid_defaults` and rebuild the config from it.

        One loop over the catalogue, so every threshold moves together and a
        knob added to `REID_TUNABLES` is live with no change here."""
        from dataclasses import replace

        assert self._pool is not None
        raw = await self._pool.fetchval(
            "SELECT value FROM app_settings WHERE key = $1", REID_KEY
        )
        self._tunables.apply(raw)
        updated = {
            name: (
                self._tunables.i(name)
                if isinstance(getattr(self._config, name), int)
                else self._tunables.f(name)
            )
            for name in REID_TUNABLES
        }
        changed = {k: v for k, v in updated.items() if getattr(self._config, k) != v}
        if not changed:
            return
        log.info("reid tunables changed live: %s", changed)
        self._config = replace(self._config, **updated)

    async def _refresh_face_threshold(self) -> None:
        """Re-read the face match gate. One holder: the finalize/resweep
        paths and the live matcher both read it through `self._config`."""
        from dataclasses import replace

        assert self._pool is not None
        row = await self._pool.fetchrow(
            "SELECT active_match_threshold AS match_threshold, active_model_key AS model_key "
            "FROM face_recognition_settings WHERE id = 1"
        )
        if row is None:
            raise RuntimeError("face_recognition_settings singleton missing")
        value = float(row["match_threshold"])
        model_key = row["model_key"] or self._config.face_embedding_model
        if (
            abs(value - self._config.reid_face_cosine_threshold) < 1e-6
            and model_key == self._config.face_embedding_model
        ):
            return
        log.info(
            "face settings changed live: threshold %.3f → %.3f, model %s → %s",
            self._config.reid_face_cosine_threshold,
            value,
            self._config.face_embedding_model,
            model_key,
        )
        # One holder now: the live matcher reads through a provider, so
        # replacing the config is all it takes.
        self._config = replace(
            self._config, reid_face_cosine_threshold=value, face_embedding_model=model_key
        )

    def _zone_cooldown_ok(
        self,
        zone_id: Any,
        class_name: str,
        kind: str,
        cooldown_s: int,
        now_ns: int,
    ) -> bool:
        """Return True if the (zone, class, kind) event is outside its
        cooldown window AND record `now_ns` as the new last-emit ns.
        `cooldown_s == 0` short-circuits — no cooldown, always pass."""
        if cooldown_s <= 0:
            return True
        key = (zone_id, class_name, kind)
        last = self._zone_event_last_ns.get(key, 0)
        window_ns = cooldown_s * 1_000_000_000
        if now_ns - last < window_ns:
            return False
        self._zone_event_last_ns[key] = now_ns
        return True

    async def _emit_zone_event(
        self, camera_slug: str, kind: EventKind, zone_id: Any, track_id: int,
        bbox: tuple[float, float, float, float], timestamp_ns: int,
        class_name: str | None = None, db_track_id: UUID | None = None,
    ) -> None:
        assert self._pool is not None and self._zones is not None
        cam = await self._resolver.get(camera_slug)
        if cam is None:
            return
        payload: dict[str, Any] = {
            "bbox": list(bbox), "class_name": class_name,
            "class_id": class_id_for_name(class_name), "track_id": str(track_id),
        }
        if zone_id is not None:
            zone = self._zones.zone_meta(camera_slug, zone_id)
            payload.update({
                "zone_id": str(zone_id), "zone_name": zone.name if zone else None,
                "zone_kind": zone.kind if zone else None,
            })
        event_id = uuid5(cam.id, f"{db_track_id or track_id}:{kind}:{zone_id}:{timestamp_ns}")
        status = await self._pool.execute(
            "INSERT INTO events (id, camera_id, track_id, kind, at, payload) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT (id) DO NOTHING",
            event_id, cam.id, db_track_id, kind,
            datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC), json.dumps(payload),
        )
        if status == "INSERT 0 1":
            self._stats.incr("events")

    async def _observe(self, msg: _TracksMessage) -> None:
        # NB: do NOT early-return on an empty tracks message. An empty frame is
        # exactly the "scene just emptied" signal, and the authoritative state
        # snapshot MUST run then to publish person_count=0 / identities={}
        # — otherwise the last non-empty snapshot (someone present, recognised)
        # is the last thing a mirror ever hears, and the consumer latches it
        # forever (observed: a DIDA identity_presence badge stuck on "body" for
        # 20+ min after the person left, because the clearing frame was dropped
        # here). The per-track steps are no-ops on empty, so the only work an
        # empty frame does is the snapshot recompute, which no-ops on the
        # dedupe unless something actually changed.
        self._stats.incr("track_msgs")
        await self._retire_older_generations(msg)

        # Transitions are computed under the state lock and emitted after it
        # is released.
        pending: list[_PendingEvent] = []
        async with self._state_lock:
            cam = self._state.setdefault(msg.camera_id, {})
            # Zero-guard in case an upstream service forgot to forward frame
            # dims: zone evaluation is skipped rather than dividing by zero
            # and calling every track "outside everything".
            has_dims = msg.frame_width > 0 and msg.frame_height > 0
            zones_active = (
                self._zones is not None and has_dims and bool(self._zones.zones_for(msg.camera_id))
            )
            for t in msg.tracks:
                rec = self._continued_record(cam, msg, t)
                # A vehicle whose visit was closed when it parked, moving again:
                # that is a departure, and departures are their own visit. Drop
                # the closed record so a fresh track id and start time are
                # minted instead of reopening a row already written.
                if (
                    rec is not None
                    and rec.parked_finalized
                    and (t.motion_state or "active") == "active"
                ):
                    cam.pop(t.track_id, None)
                    rec = None
                # Where this subject was LAST tick, captured before any update —
                # the reference for the thumbnail teleport guard. None on a
                # brand-new record (no history to jump from).
                prev_bbox = rec.last_bbox if rec is not None else None
                if rec is None:
                    rec = cam[t.track_id] = self._open_record(msg, t)
                else:
                    _note_observation(rec, msg, t)
                if has_dims:
                    self._bake_thumbnail(msg, t, rec, prev_bbox)
                rec.class_counts[t.class_id] = rec.class_counts.get(t.class_id, 0) + 1
                rec.class_names.setdefault(t.class_id, t.class_name)
                self._note_motion(msg, t, rec, pending)
                # Heatmap accumulator: every observation contributes one
                # bottom-centre point. Same anchor as zone evaluation so the
                # heatmap and zone counts line up. Independent of zones; the
                # accumulator is a flat in-memory grid, cost is one add.
                # class_id routes into person / vehicle / animal / other
                # buckets — the dashboard can then filter or sum.
                if has_dims:
                    self._heatmap.add(
                        msg.camera_id,
                        t.class_id,
                        (t.x1 + t.x2) / 2.0 / msg.frame_width,
                        t.y2 / msg.frame_height,
                    )
                # Born-parked tracks (never yet active) skip zone evaluation
                # entirely — see TrackState.ever_active. If one later moves,
                # membership is evaluated from that tick and a legitimate
                # zone_enter fires then.
                if zones_active and rec.ever_active:
                    self._note_zones(msg, t, rec, pending)

        # Sequential await is fine — events are sparse (only on transitions);
        # the rules dispatcher fan-out happens asynchronously via Postgres
        # NOTIFY.
        for e in pending:
            await self._emit_zone_event(
                msg.camera_id,
                e.kind,
                e.zone_id,
                e.track_id,
                e.bbox,
                msg.timestamp_ns,
                e.class_name,
                e.db_track_id,
            )
        if self._snapshot is not None:
            await self._publish_snapshot(msg, cam)

    async def _retire_older_generations(self, msg: _TracksMessage) -> None:
        if not msg.epoch:
            return
        retired = False
        async with self._state_lock:
            cam = self._state.get(msg.camera_id, {})
            for tid, rec in list(cam.items()):
                if rec.epoch and rec.epoch < msg.epoch:
                    if not rec.parked_finalized:
                        self._queue_finalization(msg.camera_id, tid, rec)
                    cam.pop(tid)
                    retired = True
        if retired:
            await self._drain_finalizations()

    def _continued_record(
        self, cam: dict[int, TrackState], msg: _TracksMessage, t: TrackWire
    ) -> TrackState | None:
        """This track's record, with the visit it continues folded in.

        The appearance hold watched this spot and matched the new arrival
        against the subject it was holding: same person, new id. Everything
        that makes a visit continuous rides along — the same db_track_id and
        first_seen_ns (so it stays ONE visit rather than two touching ones),
        the zone enter/dwell state (so a heating zone never sees an exit), the
        observation count and the identity. Without this the join had to be
        rediscovered downstream through global_id re-ID, which is precisely
        what fails on someone sitting with their back to the camera.
        """
        rec = cam.get(t.track_id)
        if not t.continues_track_id:
            return rec
        prev = cam.pop(t.continues_track_id, None)
        if prev is None or prev is rec:
            return rec
        if rec is None:
            how = "one presence, visit carries on"
        else:
            # LATE handover: the successor had to live ~2 s before it could
            # take the baton (mayfly filter), so by the time the continuation
            # arrives it already opened a young record of its own.
            prev.absorb(rec)
            how = "young record folded into the visit"
        cam[t.track_id] = prev
        # The live identity verdict rides the same handover — the badge must
        # not blink anonymous across a seam the tracker has already proven is
        # one subject.
        self._live_identity.transfer(msg.camera_id, t.continues_track_id, t.track_id)
        log.info(
            "track %s continues %s on %s — %s",
            t.track_id,
            t.continues_track_id,
            msg.camera_id,
            how,
        )
        return prev

    def _open_record(self, msg: _TracksMessage, t: TrackWire) -> TrackState:
        first_motion = t.motion_state or "active"
        # A visit starts when the SUBJECT appeared, not when the tracker
        # finally confirmed them: the birth gate needs consecutive confident
        # frames while the camera ramps up from idle fps, so the confirmed
        # moment is seconds into a real entrance. The tracker back-dates it to
        # the earliest detection of this object (wire.first_seen_ns), which is
        # what the Activity clip anchors on — no fixed pre-roll guess. Bounded:
        # an object can sit UNQUALIFIED for a long time (a low-confidence
        # phantom that only later earns a confident hit), and inheriting that
        # whole span would invent a visit that never happened.
        first_ns = msg.timestamp_ns
        if t.first_seen_ns and 0 < msg.timestamp_ns - t.first_seen_ns <= _FIRST_SEEN_BACKDATE_MAX_NS:
            first_ns = t.first_seen_ns
        cx = (t.x1 + t.x2) / 2.0
        cy = (t.y1 + t.y2) / 2.0
        return TrackState(
            first_seen_ns=first_ns,
            last_seen_ns=msg.timestamp_ns,
            last_evidence_ns=t.last_matched_ns or msg.timestamp_ns,
            class_id=t.class_id,
            class_name=t.class_name,
            max_confidence=t.confidence,
            n_observations=1,
            last_bbox=(t.x1, t.y1, t.x2, t.y2),
            min_cx=cx,
            max_cx=cx,
            min_cy=cy,
            max_cy=cy,
            db_track_id=uuid4(),
            # Diff baseline is the first OBSERVED state, not a hardcoded
            # "active" — a track born parked (tracker birth-suppression on
            # static-clutter phantoms) never transitioned, so it must not emit
            # object_parked.
            last_motion_state=first_motion,
            # ever_moved carries provenance through the tracker's ghost chain:
            # a seated person's flicker respawn is born parked yet still
            # zone-eligible, so presence (DIDA radio-on-patio) survives track
            # churn. A phantom respawn has ever_moved=False and stays silent.
            ever_active=first_motion == "active" or t.ever_moved,
            epoch=msg.epoch,
        )

    def _bake_thumbnail(
        self,
        msg: _TracksMessage,
        t: TrackWire,
        rec: TrackState,
        prev_bbox: tuple[float, float, float, float] | None,
    ) -> None:
        """Bake the box-thumbnail from THIS detection's own SHM frame the
        moment it becomes the clearest active view of the track (see
        snapshot.py). Captured live, so it needs no closed recording segment
        and the box sits on the subject by construction.

        A portrait must not TELEPORT. With two people on camera, one tick of
        association slip is enough for a walking visitor at 0.8 to beat the
        seated subject's 0.5 forever — and the visit thumbnail ends up boxing
        someone else entirely (live: the guest crossing the doorway, boxed on
        Marko's seated visit). A candidate whose centre jumped more than a
        quarter of the frame since the previous observation is an id-slip, not
        a clearer view of the same subject.
        """
        fw, fh = float(msg.frame_width), float(msg.frame_height)
        if (t.motion_state or "active") != "active":
            return
        if t.confidence <= rec.best_thumb_conf + _THUMB_CONF_MARGIN:
            return
        if prev_bbox is not None:
            jump = (
                (((t.x1 + t.x2) / 2 - (prev_bbox[0] + prev_bbox[2]) / 2) / fw) ** 2
                + (((t.y1 + t.y2) / 2 - (prev_bbox[1] + prev_bbox[3]) / 2) / fh) ** 2
            ) ** 0.5
            if jump > 0.25:
                return
        jpeg = self._snapshot_baker.bake(
            msg.camera_id, msg.sequence, (t.x1 / fw, t.y1 / fh, t.x2 / fw, t.y2 / fh)
        )
        if jpeg is not None:
            rec.best_thumb_jpeg = jpeg
            rec.best_thumb_conf = t.confidence

    def _note_motion(
        self, msg: _TracksMessage, t: TrackWire, rec: TrackState, pending: list[_PendingEvent]
    ) -> None:
        """Motion-state transition events.

        A single object_parked when the tracker promotes from active to either
        stationary or parked, and a single object_unparked on the inverse
        transition. The operator gets a one-line-per-arrival/departure timeline
        instead of the per-frame zone_enter spam that would otherwise come from
        a parked car (the tracker's centroid jitter eventually trips the zone
        boundary on long-stay cars in any non-trivial zone).
        """
        cur_motion = t.motion_state or "active"
        if cur_motion == "active" or t.ever_moved:
            rec.ever_active = True
        # A track whose bbox touches a frame edge is entering or LEAVING, not
        # parking in place — a car that pauses at the gate on its way out
        # (bbox at the frame edge, shrinking) would otherwise be labelled
        # object_parked right before the track is lost (live: West red car
        # "parked" at [38,…] as it exited the Side Gate). Unpark is NOT gated —
        # a real departure must fire.
        fw, fh = float(msg.frame_width), float(msg.frame_height)
        at_frame_edge = (
            fw > 0
            and fh > 0
            and (
                t.x1 <= fw * _EDGE_MARGIN_FRAC
                or t.x2 >= fw * (1.0 - _EDGE_MARGIN_FRAC)
                or t.y2 >= fh * (1.0 - _EDGE_MARGIN_FRAC)
            )
        )
        # Only something that moved can come to rest. The edge deferral in
        # came_to_rest reports any parked state not yet reported, and a track
        # BORN parked (a static-clutter phantom, an IR re-birth) is one: without
        # this gate each such birth filed an object_parked — 1.1k a week became
        # 33k from 17.08.2026.
        rest = rec.ever_active and came_to_rest(
            rec.last_motion_state, cur_motion, at_frame_edge, rec.parked_emitted
        )
        if cur_motion in ("stationary", "parked"):
            rec.came_to_rest = True
        if cur_motion == "active":
            rec.parked_emitted = False
        if cur_motion == rec.last_motion_state and not rest:
            return
        bbox = (t.x1, t.y1, t.x2, t.y2)
        if rest:
            rec.parked_emitted = True
            pending.append(_PendingEvent("object_parked", None, t.track_id, bbox, t.class_name))
            # A vehicle that has come to rest has finished its arrival, so
            # close the visit here rather than holding the row open for the
            # hours it stands there. That delay was not cosmetic: `tracks` is
            # written only at finalize, so a parked car existed nowhere the
            # rest of the system could see it, and the plate sweep — which
            # reads finished tracks — could not read its plate until it left.
            # Measured 2026-07-29: an arrival at 16:41 whose plate was legible
            # for 26 consecutive frames was still unread hours later.
            #
            # ever_active is what separates an arrival from a re-detection:
            # under IR a standing car is repeatedly lost and re-born, and those
            # respawns never moved. One night produced twenty of them; without
            # this gate each would now file its own "arrival".
            if rec.ever_active and t.class_id in VEHICLE_GROUP:
                rec.park_finalize_due = True
        elif rec.last_motion_state in ("stationary", "parked") and cur_motion == "active":
            pending.append(_PendingEvent("object_unparked", None, t.track_id, bbox, t.class_name))
        rec.last_motion_state = cur_motion

    def _admitting_zones(
        self, msg: _TracksMessage, t: TrackWire
    ) -> tuple[frozenset[Any], dict[Any, int], dict[Any, int]]:
        """The zones this track is inside and each one's rules let fire, with
        the per-zone dwell and cooldown overrides of those that did.

        Point-in-polygon on the bbox *bottom-centre* (y2) instead of the
        geometric centre — for ground-plane zones like "parking" or "doorway"
        the bottom edge is the foot position, which is what the operator
        actually drew against in the snapshot. The midpoint would trigger
        "entered the parking" while the person is just walking past with their
        head crossing the line. Raw membership is then filtered by the
        per-zone class allowlist, min_confidence and min_area_pct (layer 3 of
        the detection-rules tower); zones with no class_rules pass every
        class.
        """
        assert self._zones is not None
        fw, fh = float(msg.frame_width), float(msg.frame_height)
        raw = self._zones.inside(msg.camera_id, (t.x1 + t.x2) / 2.0 / fw, t.y2 / fh)
        bbox_area_pct = ((t.x2 - t.x1) * (t.y2 - t.y1)) / (fw * fh)
        admitted: set[Any] = set()
        dwell_ms: dict[Any, int] = {}
        cooldown_s: dict[Any, int] = {}
        for zid in raw:
            zmeta = self._zones.zone_meta(msg.camera_id, zid)
            rule = _zone_class_rule(zmeta, t.class_name)
            if rule is _ZONE_CLASS_REJECTED:
                continue
            # Motion gate: a Parking/Entry/Interest zone is not supposed to
            # fire events for stationary objects (parked-car bbox jitter is the
            # canonical case). Per-class rule beats the zone-kind default;
            # "moving_only" suppresses while the tracker reports the subject as
            # stationary or parked. Without gating, membership is purely a
            # geometric containment check and a static bbox whose centre
            # wobbles across the polygon edge generates an endless enter/exit
            # stream.
            gate = _effective_motion_gate(zmeta, rule)
            if not _motion_gate_allows(gate, t.motion_state, t.class_name):
                continue
            if rule is not None:
                # Say WHICH gate rejected and by how much. Twice now a whole
                # class of subject has gone missing from Activity and the only
                # clue was "off-zone" — the numbers were reachable but never
                # written down, so both hunts were guesswork against live data.
                if rule.min_confidence is not None and t.confidence < rule.min_confidence:
                    log.debug(
                        "zone reject cam=%s tid=%s class=%s zone=%s "
                        "min_confidence %.3f < %.3f",
                        msg.camera_id, t.track_id, t.class_name, zid,
                        t.confidence, rule.min_confidence,
                    )
                    continue
                if rule.min_area_pct is not None and bbox_area_pct < rule.min_area_pct:
                    log.debug(
                        "zone reject cam=%s tid=%s class=%s zone=%s "
                        "min_area_pct %.5f < %.5f",
                        msg.camera_id, t.track_id, t.class_name, zid,
                        bbox_area_pct, rule.min_area_pct,
                    )
                    continue
                if rule.min_dwell_ms is not None:
                    dwell_ms[zid] = rule.min_dwell_ms
                if rule.cooldown_s is not None:
                    cooldown_s[zid] = rule.cooldown_s
            admitted.add(zid)
        return frozenset(admitted), dwell_ms, cooldown_s

    def _note_zones(
        self, msg: _TracksMessage, t: TrackWire, rec: TrackState, pending: list[_PendingEvent]
    ) -> None:
        current, dwell_ms, cooldown_s = self._admitting_zones(msg, t)

        def fire(kind: EventKind, zid: Any) -> None:
            if self._zone_cooldown_ok(zid, t.class_name, kind, cooldown_s.get(zid, 0), msg.timestamp_ns):
                pending.append(
                    _PendingEvent(kind, zid, t.track_id, rec.last_bbox, t.class_name, rec.db_track_id)
                )

        previous = frozenset(rec.inside_zones)
        # An entry only records entry_ns; `zone_enter` waits for the dwell gate
        # below. An exit fires only when the matching `zone_enter` did: a track
        # that left before its dwell threshold never produced an enter, and
        # suppressing the exit too keeps the pair self-consistent.
        for entered in current - previous:
            rec.inside_zones[entered] = msg.timestamp_ns
            rec.touched_zone = True
        for exited in previous - current:
            rec.inside_zones.pop(exited, None)
            rec.dwell_emitted.discard(exited)
            if exited in rec.enter_emitted:
                rec.enter_emitted.discard(exited)
                fire("zone_exit", exited)

        # Deferred zone_enter: fires once the track has been inside the zone
        # continuously for its `min_dwell_ms`, which filters the typical CCTV
        # pollution of motorcycles / pedestrians zipping through a zone for
        # <1 s. Without a rule the gate is open and entry fires on the same
        # observation.
        for zid, entry_ns in rec.inside_zones.items():
            if zid in rec.enter_emitted:
                continue
            gate_ms = dwell_ms.get(zid, 0)
            if gate_ms > 0 and (msg.timestamp_ns - entry_ns) < gate_ms * 1_000_000:
                continue
            rec.enter_emitted.add(zid)
            rec.ever_enter_fired = True
            fire("zone_enter", zid)

        # Long-stay (zone_dwell): the track has been inside the zone for the
        # GLOBAL dwell threshold (default 30 s), the "loiter" indicator. Not
        # when the zone's own min_dwell_ms already covers it — then zone_enter
        # IS the long stay and a second event is redundant — nor when the
        # global threshold is off.
        global_ms = self._config.dwell_threshold_ms
        for zid, entry_ns in rec.inside_zones.items():
            if zid in rec.dwell_emitted:
                continue
            if global_ms <= 0 or dwell_ms.get(zid, 0) >= global_ms:
                continue
            if msg.timestamp_ns - entry_ns < global_ms * 1_000_000:
                continue
            rec.dwell_emitted.add(zid)
            fire("zone_dwell", zid)

    async def _publish_snapshot(self, msg: _TracksMessage, cam: dict[int, TrackState]) -> None:
        """The camera's authoritative state, built from the SAME picture the
        zone events came from — a consumer that mirrors it needs no sweep, no
        off-delay and no cooldown of its own."""
        info = await self._resolver.get(msg.camera_id)
        if info is None:
            return
        occupied: set[Any] = set()
        real_tracks: list[TrackWire] = []
        live_person_ids: list[int] = []
        for t in msg.tracks:
            rec = cam.get(t.track_id)
            # ever_active gates EVERY vision field, not just zones: a
            # static-clutter phantom must never report "1 person here" or raise
            # motion, and gating one field but not the others would publish a
            # self-contradicting snapshot (person_count=1 with every zone false
            # — observed on patio before this). Same gate the zone events use.
            if rec is None or not rec.ever_active:
                continue
            real_tracks.append(t)
            occupied |= set(rec.inside_zones)
            if canonical_class(t.class_id) == "person":
                live_person_ids.append(t.track_id)
        # Keyed by zone UUID, not name: a renamed zone must keep its key so the
        # consumer's entity/automation survives. Every zone the camera has is
        # reported, disabled ones included and always false. Filtering disabled
        # zones OUT froze them at their last value in a consumer that mirrors
        # occupancy: disable an occupied zone and its key vanished, so the
        # mirror never saw the `false` that clears it and the zone read
        # "occupied" forever.
        zones_open = {
            str(z.id): bool(z.enabled) and z.id in occupied
            for z in (self._zones.zones_for(msg.camera_id) if self._zones else [])
        }
        # Named PET/VEHICLE subjects on screen — a separate map from the
        # people's so a consumer's people-logic never sees a car as a person.
        # The name arrives ON THE WIRE: the tracker stamps non-person identity
        # at the source (identity_stamp.py — same OSNet space, same
        # MIN-over-refs + margin rule), so this is a pure read, no DB kNN here.
        object_identities: dict[str, str] = {}
        object_identity_kinds: dict[str, str] = {}
        for t in real_tracks:
            if not t.identity_gid:
                continue
            okind, _ = reference_kind_for_class(t.class_id, self._config)
            if okind is None:
                continue  # persons never body-named — defense in depth
            object_identities[t.identity_gid] = t.identity_name
            object_identity_kinds[t.identity_gid] = okind
        identities, identity_sources, identity_since = await self._people_on_screen(
            msg.camera_id, info.id, live_person_ids
        )
        await self._snapshot.update_vision(
            str(info.id),
            info.name,
            real_tracks,
            zones_open,
            msg.timestamp_ns,
            identities,
            identity_sources,
            identity_since,
            object_identities,
            object_identity_kinds,
        )

    async def _people_on_screen(
        self, slug: str, camera_id: UUID, track_ids: list[int]
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Who the people still on screen are, how we know, and since when.

        By face, or by body anchored to a recent face confirmation
        (live_identity.py). Finalize would reach the same verdict ~27 s later,
        by which time the person has left and a consumer driving automation
        off the snapshot has missed the moment entirely.
        """
        if self._live_identity is None or not track_ids:
            return {}, {}, {}
        async with self._pool.acquire() as conn:
            matched = await self._live_identity.resolve(conn, slug, camera_id, track_ids)
        # When each of them has been here: the open episode's start, from the
        # registry's cache (never a query on this path). A verdict still
        # inside the sustain gate has no episode yet and is simply not in the
        # map — the consumer shows the presence without a since until the
        # stay earns its row.
        since = {
            str(gid): s.isoformat()
            for gid in matched
            if (s := self._presence.since_of(camera_id, gid)) is not None
        }
        # The durable layer: the same verdicts, written as presence episodes
        # so a stay survives track churn and restarts. A registry failure
        # must not cost the live snapshot.
        try:
            await self._presence.observe(camera_id, matched)
        except Exception:
            log.exception("presence observe failed — snapshot continues")
        return (
            {str(gid): name for gid, (name, _s) in matched.items()},
            {str(gid): src for gid, (_n, src) in matched.items()},
            since,
        )

    async def _cleanup_loop(self) -> None:
        """Periodically reap per-observation embedding samples older than
        the retention window, AND unlink their JPEG crop files from disk.

        CRITICAL: a finalized track's `crop_path`/`face_crop_path` is a COPY
        of its best sample's path string — both point at the SAME file. So
        before unlinking a deleted sample's crop we must check no surviving
        track still references it, otherwise the Identities UI ends up with
        live tracks pointing at deleted files ("no thumbnail"). We delete the
        rows first, then unlink only the files no track references.

        `tracks.embedding`/`tracks.face_embedding` carry the canonical
        vectors for re-ID, so dropping the sample rows never hurts matching."""
        assert self._pool is not None
        from pathlib import Path

        media_root = Path(self._config.media_path)
        interval = self._config.samples_cleanup_interval_s
        retention_days = self._config.samples_retention_days
        # Small initial delay so cleanup doesn't fight startup migrations.
        await asyncio.sleep(min(30, interval))
        while True:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        DELETE FROM track_embedding_samples
                        WHERE captured_at < now() - make_interval(days => $1)
                        RETURNING crop_path, face_crop_path
                        """,
                        retention_days,
                    )
                    n = len(rows)
                    crops = {r["crop_path"] for r in rows if r["crop_path"]}
                    faces = {r["face_crop_path"] for r in rows if r["face_crop_path"]}
                    # Keep any file a surviving track still points at.
                    if crops:
                        kept = await conn.fetch(
                            "SELECT DISTINCT crop_path FROM tracks WHERE crop_path = ANY($1)",
                            list(crops),
                        )
                        crops -= {r["crop_path"] for r in kept}
                    if faces:
                        kept = await conn.fetch(
                            "SELECT DISTINCT face_crop_path FROM tracks WHERE face_crop_path = ANY($1)",
                            list(faces),
                        )
                        faces -= {r["face_crop_path"] for r in kept}
                unlinked = 0
                for rel in crops | faces:
                    try:
                        (media_root / rel).unlink(missing_ok=True)
                        unlinked += 1
                    except Exception:
                        log.exception("failed to unlink crop %s", rel)
                if n > 0:
                    log.info(
                        "samples cleanup: deleted %d rows (>%dd old), unlinked %d unreferenced crop files",
                        n,
                        retention_days,
                        unlinked,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("samples cleanup failed; will retry next interval")
            await asyncio.sleep(interval)

    async def _reid_resweep_loop(self) -> None:
        """Catch face matches the one-shot finalize re-ID missed.

        re-ID runs once, at finalize. But the embedder writes a track's
        `face_embedding` asynchronously, and an operator can enrol a reference
        face long after a person was first seen -- in both cases the anonymous
        track never gets a chance to link. This loop periodically re-matches
        recent anonymous PERSON faces against every named identity's curated
        face evidence (enrolled reference photos + the identity's canonical
        face_embedding) and links any within `reid_face_cosine_threshold` --
        the exact decision finalize would have made. Face is the only signal
        allowed to assign a cross-session identity, so this never touches body
        clustering and can't trigger the over-merge vortex."""
        assert self._pool is not None
        interval = self._config.reid_resweep_interval_s
        if interval <= 0:
            return
        await asyncio.sleep(min(45, interval))  # let startup settle first
        while True:
            try:
                linked = await self._reid_resweep_once()
                if linked:
                    log.info(
                        "reid resweep: linked %d anonymous face track(s) to named identities",
                        linked,
                    )
                # Body pass runs AFTER the face pass so it only ever considers
                # tracks the stronger face signal couldn't place, and only
                # anchors to identities the face pass may have just confirmed.
                anchored = await self._reid_body_anchor_resweep_once()
                if anchored:
                    log.info(
                        "reid resweep: face-anchored body chain linked %d "
                        "anonymous track(s) to named identities",
                        anchored,
                    )
                # Body-reference pass: link anonymous PET/VEHICLE tracks to an
                # operator-enrolled reference. This is what makes references
                # enrolled AFTER a track finalized (the usual case — you enrol
                # Lumi from yesterday's sighting) actually name today's tracks.
                ref_linked = await self._reid_body_reference_resweep_once()
                if ref_linked:
                    log.info(
                        "reid resweep: body-reference linked %d anonymous "
                        "pet/vehicle track(s) to enrolled identities",
                        ref_linked,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reid resweep failed; will retry next interval")
            await asyncio.sleep(interval)

    async def _mark_face_verified(self, conn, track_id: Any, by: str) -> bool:
        """Record that a face confirmed this track — if the track has one.

        `face_verified` is not a badge. It is the condition on being an ANCHOR
        for the body chain: a track wearing it is offered to other cameras as a
        real face confirmation, and a body match to it inherits its name. So it
        has to be a fact about the row rather than a verdict remembered from
        the moment the row was written.

        It was the latter, and the two came apart. The live matcher matched a
        face against samples that finalize then did not claim — its window is
        the track's, and an orphan sits outside it — so the row finalized
        face-verified, body-sourced, carrying no face at all: 360 of 792 on
        05.09, eight of them that day. The live query is bounded now, and this
        is the second lock: a claim that cannot be seen in the row is not
        written, and says so.
        """
        applied = await conn.fetchval(
            """
            UPDATE tracks t SET face_verified = true
             WHERE t.id = $1
               AND EXISTS (
                   SELECT 1 FROM track_embedding_samples s
                    WHERE s.track_id = t.id
                      AND s.face_embedding IS NOT NULL
                      AND s.face_px >= $2)
            RETURNING t.id
            """,
            track_id, FACE_ID_MIN_PX,
        )
        if applied is None:
            log.warning(
                "face verification by %s not recorded for track %s: the track "
                "owns no face of naming size, so nothing may anchor to it",
                by, track_id,
            )
        return applied is not None

    async def _reid_resweep_once(self) -> int:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            candidates = await conn.fetch(
                """
                WITH named_face AS (
                    -- Curated face evidence per named identity: enrolled
                    -- reference photos + the identity's canonical face vector.
                    -- Only the active embedder's vectors: a face embedding is
                    -- comparable only within the model that produced it.
                    SELECT global_id, face_embedding
                    FROM identity_reference_photos
                    WHERE face_embedding IS NOT NULL
                      AND face_embedding_model = $5
                    UNION ALL
                    SELECT global_id, face_embedding
                    FROM identity_labels
                    WHERE face_embedding IS NOT NULL
                      AND face_embedding_model = $5
                ),
                anon_faces AS (
                    -- Each anonymous person track's DECENT-score face crops
                    -- (not just its single best-score one — MIN over these vs
                    -- each identity's evidence, same as finalize).
                    SELECT t.id AS track_id, s.face_embedding
                    FROM tracks t
                    JOIN track_embedding_samples s ON s.track_id = t.id
                    LEFT JOIN identity_labels l ON l.global_id = t.global_id
                    WHERE l.global_id IS NULL          -- still anonymous
                      AND t.class_id = 0               -- person
                      AND s.face_embedding IS NOT NULL
                      AND s.face_embedding_model = $5
                      AND (s.face_score IS NULL OR s.face_score >= $3)
                      -- And big enough to tell anybody apart. The embedder
                      -- re-reads an under-sized face from the recording, so a
                      -- sample that is still small here is one the footage had
                      -- no more of either.
                      AND s.face_px >= $4
                      AND t.started_at > now() - make_interval(days => $2)
                ),
                best AS (
                    SELECT track_id, named_gid, dist,
                           ROW_NUMBER() OVER (PARTITION BY track_id ORDER BY dist ASC) AS rn,
                           -- The next identity's distance, so the caller can
                           -- ask the same question finalize asks: not who is
                           -- nearest, but whether anybody else is just as near.
                           LEAD(dist) OVER (PARTITION BY track_id ORDER BY dist ASC) AS next_dist
                    FROM (
                        SELECT af.track_id, nf.global_id AS named_gid,
                               MIN(nf.face_embedding <=> af.face_embedding) AS dist
                        FROM anon_faces af CROSS JOIN named_face nf
                        GROUP BY af.track_id, nf.global_id
                    ) per_identity
                )
                SELECT track_id, named_gid, dist, next_dist
                FROM best WHERE rn = 1 AND dist < $1
                """,
                self._config.reid_face_cosine_threshold,
                self._config.reid_window_days,
                self._config.reid_face_sample_min_score,
                FACE_ID_MIN_PX,
                self._config.face_embedding_model,
            )
            # One rule, asked here rather than answered again. This pass used
            # to take the nearest identity under threshold and nothing else,
            # so it named the tracks finalize had already refused to name for
            # exactly the reason it refused them.
            keep = []
            for c in candidates:
                if survives_rivals(c["named_gid"], c["dist"], c["next_dist"]):
                    keep.append(c)
                else:
                    log.info(
                        "late face link declined for track %s: nearest %.3f and "
                        "another identity at %.3f", c["track_id"], c["dist"],
                        c["next_dist"] if c["next_dist"] is not None else -1.0,
                    )
            if not keep:
                return 0
            rows = await conn.fetch(
                f"""
                UPDATE tracks t
                SET global_id = b.named_gid,
                    identity_source = 'body',
                    -- This is a FACE match to curated evidence (references /
                    -- canonical), the strongest kind — so mark it face_verified
                    -- like finalize does. Without this a late face link named
                    -- the track but left it non-face-verified, so it could not
                    -- serve as the anchor the cross-camera body chain needs, and
                    -- the recognition never propagated to the cameras that saw
                    -- only the body (the whole point of recovering the match).
                    face_verified = true,
                    -- Late-link also extends retention to the identity's tier.
                    -- Finalize set retain_until using the track's gid AT THAT
                    -- time (anonymous → 30d); now that we've linked it to a
                    -- named/reference-enrolled identity, bump it to 60/90d so a
                    -- genuine sighting isn't pruned at the anonymous deadline.
                    -- GREATEST() never shrinks an existing deadline.
                    retain_until = GREATEST(
                        COALESCE(t.retain_until, t.ended_at, now()),
                        COALESCE(t.ended_at, now()) + (
                            {tier_sql("b.named_gid")}
                        )
                    )
                FROM (SELECT unnest($1::uuid[]) AS track_id,
                             unnest($2::uuid[]) AS named_gid) b
                WHERE t.id = b.track_id
                  -- The statement that writes the flag is the statement that
                  -- looks for the face. The candidates were selected on their
                  -- faces a moment ago, but `face_verified` decides what may
                  -- ANCHOR a body chain on another camera, and a claim that
                  -- cannot be seen in the row is not one to write down.
                  AND EXISTS (
                      SELECT 1 FROM track_embedding_samples s
                       WHERE s.track_id = t.id
                         AND s.face_embedding IS NOT NULL
                         AND s.face_px >= $3)
                RETURNING t.id
                """,  # noqa: S608
                [c["track_id"] for c in keep],
                [c["named_gid"] for c in keep],
                FACE_ID_MIN_PX,
            )
        return len(rows)

    async def _reid_body_anchor_resweep_once(self) -> int:
        """Face-anchored body chain, retroactive pass.

        Finalize does this at track end, but the anchor may not exist yet then:
        an anonymous session finalizes, and only a LATER track gets
        face-confirmed for a named identity (the patio case where the clear-face
        moment came after the shirtless session). This promotes every recent
        still-anonymous person track the chain can now name — the rule itself
        lives in `face_anchor`, so this pass and finalize cannot drift apart.

        `modality='body-anchor'` in the audit trail, and face_verified is
        deliberately left untouched on the promoted track: it inherited a name,
        it did not show a face, and only a face may anchor the next link."""
        assert self._pool is not None
        if not chain_enabled(self._config):
            return 0
        chain = anchor_sql(
            """
            SELECT t.id AS key, t.camera_id, COALESCE(t.ended_at, now()) AS at,
                   t.embedding, $2::int[] AS class_ids
            FROM tracks t
            LEFT JOIN identity_labels l ON l.global_id = t.global_id
            WHERE l.global_id IS NULL          -- still anonymous
              AND t.class_id = ANY($2::int[])
              AND t.embedding IS NOT NULL
              AND t.started_at > now() - make_interval(days => $1)
            """,
            first_param=3,
            extra="t.id <> m.key",
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE tracks t
                SET global_id = b.global_id,
                    identity_source = 'body',
                    retain_until = GREATEST(
                        COALESCE(t.retain_until, t.ended_at, now()),
                        COALESCE(t.ended_at, now()) + (
                            {tier_sql("b.global_id")}
                        )
                    )
                FROM ({chain}) b
                WHERE t.id = b.key
                RETURNING t.id, b.global_id AS named_gid, b.dist, b.same_cam
                """,  # noqa: S608
                self._config.reid_window_days,
                [0],
                *chain_params(self._config),
            )
            for r in rows:
                xcam = not r["same_cam"]
                await conn.execute(
                    "INSERT INTO identity_audit (user_id, op, payload) "
                    "VALUES (NULL, 'auto_match', $1::jsonb)",
                    json.dumps(
                        {
                            "gid": str(r["named_gid"]),
                            "track_id": str(r["id"]),
                            "dist": round(float(r["dist"]), 4),
                            "threshold": (
                                self._config.reid_face_anchor_xcam_threshold
                                if xcam
                                else self._config.reid_face_anchor_body_threshold
                            ),
                            "source": "face-anchor-xcam-resweep" if xcam else "face-anchor-resweep",
                            "modality": "body-anchor",
                        }
                    ),
                )
        return len(rows)

    async def _reid_body_reference_resweep_once(self) -> int:
        """Link anonymous PET tracks to an operator-enrolled reference.

        Finalize's body-reference match only fires for tracks finalized AFTER the
        reference was enrolled, but the usual flow is the reverse — you enrol Lumi
        from an existing sighting — so this pass re-runs the same rule over recent
        still-anonymous pet tracks. Identical logic to finalize: MIN over the
        identity's reference photos, adopt the nearest identity only when under
        the threshold AND clearly closer than the runner-up by the margin.
        Person tracks are untouched (face-only), and so are vehicles — see
        `reference_kind_for_class` for the measurement that ended appearance
        naming of cars. A reference-matched track gets the 90-day retention a
        named+enrolled identity warrants."""
        assert self._pool is not None
        pet_thr = self._config.reid_body_reference_pet_threshold
        if pet_thr <= 0:
            return 0
        pet_ids = [int(c) for c in PET_GROUP]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH anon AS (
                    SELECT t.id, t.embedding, t.class_id, t.ended_at
                    FROM tracks t
                    LEFT JOIN identity_labels l ON l.global_id = t.global_id
                    WHERE l.global_id IS NULL
                      AND t.embedding IS NOT NULL
                      AND t.class_id = ANY($1::int[])
                      AND t.started_at > now() - make_interval(days => $2)
                ),
                dists AS (
                    -- COALESCE(match_kind, kind): the POOL, decoupled from the
                    -- display taxonomy (migration 066 — the mower is kind
                    -- 'object' for the operator but matches in the pet pool
                    -- because the detector calls it a dog).
                    SELECT a.id AS track_id, a.ended_at,
                           il.global_id AS cand_gid,
                           MIN(rp.body_embedding <=> a.embedding) AS dist
                    FROM anon a
                    JOIN identity_labels il
                      ON COALESCE(il.match_kind, il.kind) = 'pet'
                    JOIN identity_reference_photos rp
                      ON rp.global_id = il.global_id AND rp.body_embedding IS NOT NULL
                    GROUP BY a.id, a.ended_at, il.global_id
                ),
                ranked AS (
                    SELECT track_id, ended_at, cand_gid, dist,
                           ROW_NUMBER() OVER (PARTITION BY track_id ORDER BY dist) AS rn,
                           LEAD(dist) OVER (PARTITION BY track_id ORDER BY dist) AS runner
                    FROM dists
                ),
                winners AS (
                    SELECT track_id, cand_gid, dist
                    FROM ranked
                    WHERE rn = 1
                      AND dist < $3::float8
                      AND (runner IS NULL OR (runner - dist) >= $4::float8)
                )
                UPDATE tracks t
                SET global_id = w.cand_gid,
                    identity_source = 'body',
                    retain_until = GREATEST(
                        COALESCE(t.retain_until, t.ended_at, now()),
                        COALESCE(t.ended_at, now()) + $5::interval
                    )
                FROM winners w
                WHERE t.id = w.track_id
                RETURNING t.id
                """,
                pet_ids,
                self._config.reid_window_days,
                pet_thr,
                self._config.reid_body_reference_margin,
                ENROLLED,
            )
        return len(rows)

    async def _state_heartbeat_loop(self) -> None:
        """Re-publish every camera's current state snapshot on a slow cadence so
        a mirror (DIDA) that connects or restarts during a quiet stretch
        self-corrects within one interval — see StateSnapshotPublisher
        .republish_all. 60 s trades a handful of idempotent messages a minute
        for never leaving a consumer latched on stale presence/zones."""
        if self._snapshot is None:
            return
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            if self._stop.is_set():
                break
            try:
                await self._snapshot.republish_all()
            except Exception:
                log.exception("state heartbeat failed; will retry next interval")

    async def _heatmap_flush_loop(self) -> None:
        """Periodic flush of in-memory heatmap counts into heatmap_daily.
        30s default is the unflushed loss window — small enough that a
        crash doesn't visibly distort a multi-day rollup, large enough
        that under heavy traffic we batch many adds per UPSERT."""
        from baba_event_manager.heatmap import flush_to_db

        interval = 30.0
        # Tiny startup delay so we don't race the first observations.
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        while True:
            buckets: dict = {}
            try:
                buckets = self._heatmap.drain()
                if buckets and self._pool is not None:
                    slug_map = await self._resolver.slug_to_id_map()
                    await flush_to_db(self._pool, buckets, slug_map)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Merge the drained window back so it's actually "kept for next
                # interval" — drain() already emptied the accumulator, so
                # without this the counts would be lost on any flush error.
                if buckets:
                    self._heatmap.merge(buckets)
                log.exception("heatmap flush failed; counts merged back for next interval")
            await asyncio.sleep(interval)

    async def _parked_loop(self) -> None:
        """Every half minute, refresh which named vehicle stands in which place
        and fold it into the state snapshots.

        A loop rather than an event hook, because the fact it reports has no
        single event: the place fills on a scene transition, but the NAME
        arrives minutes later when the plate sweep renames the track — with no
        message on the bus. Thirty seconds is far under the time a car spends
        parked, and the snapshot only republishes on change.
        """
        while True:
            try:
                await self._snapshot.update_parked(
                    await parked_assignments(self._pool)
                )
            except Exception:
                log.exception("parked refresh failed — continuing")
            await asyncio.sleep(30)

    async def _presence_loop(self) -> None:
        """Close presence episodes whose person has measurably left."""
        while True:
            try:
                await self._presence.sweep()
            except Exception:
                log.exception("presence sweep failed — continuing")
            await asyncio.sleep(30)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self._sweep_once()

    def _queue_finalization(self, camera: str, local_id: int, rec: TrackState,
                            parked: bool = False) -> None:
        if rec.db_track_id is None:
            rec.db_track_id = uuid4()
        if rec.db_track_id not in self._pending_finalizations:
            self._pending_finalizations[rec.db_track_id] = _PendingTrack(
                camera, local_id, copy.deepcopy(rec), rec, parked
            )

    async def _sweep_once(self) -> None:
        now_ns = time.time_ns()
        timeout_ns = self._config.track_timeout_ms * 1_000_000
        async with self._state_lock:
            for camera, tracks in self._state.items():
                for local_id, rec in list(tracks.items()):
                    if now_ns - rec.last_seen_ns > timeout_ns:
                        if not rec.parked_finalized:
                            self._queue_finalization(camera, local_id, rec)
                        tracks.pop(local_id)
                    elif rec.park_finalize_due:
                        rec.park_finalize_due = False
                        self._queue_finalization(camera, local_id, rec, parked=True)
        await self._drain_finalizations()

    async def _drain_finalizations(self) -> None:
        async with self._finalization_lock:
            for track_id, item in list(self._pending_finalizations.items()):
                try:
                    await self._finalize(item.camera, item.local_id, item.record)
                except Exception:
                    log.exception("finalization pending cam=%s tid=%s", item.camera, item.local_id)
                    continue
                if item.parked:
                    item.source.parked_finalized = True
                    item.source.inside_zones.clear()
                    item.source.enter_emitted.clear()
                    item.source.dwell_emitted.clear()
                self._pending_finalizations.pop(track_id)
            self._stats.set_gauge("finalization_pending", len(self._pending_finalizations))

    async def _finalize_all_pending(self) -> None:
        async with self._state_lock:
            for camera, tracks in self._state.items():
                for local_id, rec in tracks.items():
                    if not rec.parked_finalized:
                        self._queue_finalization(camera, local_id, rec)
            self._state.clear()
        deadline = time.monotonic() + 10.0
        while self._pending_finalizations:
            await self._drain_finalizations()
            if not self._pending_finalizations:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"shutdown has {len(self._pending_finalizations)} uncommitted tracks")
            await asyncio.sleep(1.0)

    async def _record_suppressed(self, local_tid: int, rec: TrackState, closed: _Closed) -> None:
        assert self._pool is not None and rec.db_track_id is not None
        inserted = await self._pool.fetchval(
            """
            INSERT INTO tracks_all (id, camera_id, local_track_id, class_id, class_name,
                                    started_at, ended_at, n_observations,
                                    ever_active, came_to_rest, suppressed_reason,
                                    retain_until)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $7::timestamptz + $12::interval)
            ON CONFLICT (id) DO NOTHING RETURNING id
            """,
            rec.db_track_id, closed.cam.id, local_tid, rec.class_id, rec.class_name,
            closed.started_at, closed.ended_at, rec.n_observations, rec.ever_active,
            rec.came_to_rest, closed.hidden_by, ANONYMOUS,
        )
        if inserted is not None:
            self._stats.incr("tracks_suppressed")

    async def _close_zones(self, cam_slug: str, local_tid: int, rec: TrackState) -> None:
        """zone_exit for every zone the track was still inside when we lost
        sight of it — but only where the matching zone_enter was emitted. A
        track that never cleared a zone's min_dwell_ms produced no enter, and
        an exit without one would be a phantom with nothing to anchor it."""
        if not rec.inside_zones:
            return
        for zid in list(rec.inside_zones.keys()):
            if zid in rec.enter_emitted:
                await self._emit_zone_event(
                    cam_slug,
                    "zone_exit",
                    zid,
                    local_tid,
                    rec.last_bbox,
                    rec.last_seen_ns,
                    class_name=rec.class_name,
                    db_track_id=rec.db_track_id,
                )
        rec.inside_zones.clear()
        rec.dwell_emitted.clear()
        rec.enter_emitted.clear()

    async def _classify(
        self, cam_slug: str, local_tid: int, rec: TrackState
    ) -> _Closed | None:
        """Whether the track existed, what it was, and which gate, if any,
        hides it. None when there is nothing to file."""
        # A visit spans first appearance → last EVIDENCE. Coasted frames are
        # emission, not presence (see TrackState.last_evidence_ns).
        end_ns = rec.last_evidence_ns or rec.last_seen_ns
        lifetime_ms = (end_ns - rec.first_seen_ns) // 1_000_000

        # Existence floor. Not a filter over subjects but over whether there
        # was one: a single detection that flickers for under a second and
        # five observations is the detector twitching, and it has no visit to
        # record. It is counted, though — every other way a track can fail to
        # reach the record now leaves a row, so this one must at least leave a
        # number, or the next "did the camera see anything" is guesswork again.
        if (
            lifetime_ms < self._config.min_track_lifetime_ms
            or rec.n_observations < self._config.min_observations
        ):
            self._stats.incr("below_existence_floor")
            return None

        cam = await self._resolver.get(cam_slug)
        if cam is None:
            log.warning("finalize: unknown camera slug %s — dropping", cam_slug)
            return None

        # Majority-vote class for the track. The detector flips between
        # similar classes (typically car↔truck) frame-to-frame on the same
        # physical object; using the *mode* over the lifetime stabilises this.
        # Decided ahead of both gates, because each of them asks what this
        # track IS and the last frame's label is the one answer none of them
        # should be given.
        if rec.class_counts:
            best_id = max(rec.class_counts, key=lambda k: rec.class_counts[k])
            rec.class_id = best_id
            rec.class_name = rec.class_names.get(best_id, rec.class_name)
        is_person = (rec.class_name or "").lower() == "person"

        dx = rec.max_cx - rec.min_cx
        dy = rec.max_cy - rec.min_cy
        movement_px = (dx * dx + dy * dy) ** 0.5
        hidden_by = self._zone_verdict(
            cam_slug, local_tid, rec, is_person, lifetime_ms
        ) or self._static_verdict(cam_slug, local_tid, rec, is_person, lifetime_ms, movement_px)
        return _Closed(
            cam=cam,
            start_ns=rec.first_seen_ns,
            end_ns=end_ns,
            movement_px=movement_px,
            # Forensic record: how the vote went, by class name.
            class_votes={
                rec.class_names.get(cid, str(cid)): n for cid, n in rec.class_counts.items()
            },
            hidden_by=hidden_by,
        )

    def _zone_verdict(
        self, cam_slug: str, local_tid: int, rec: TrackState, is_person: bool, lifetime_ms: int
    ) -> str | None:
        """Zones are how the operator declares "I care about this region";
        everything outside is noise from their POV.

        • camera has NO (valid) zones → the WHOLE FRAME is one implicit zone:
          show all activity. A new camera shouldn't silently show nothing until
          the operator draws a polygon. Zones are a refinement (focus a region,
          set dwell, label, attach rules), not a prerequisite.
        • camera HAS zones, track touched at least one + sustained dwell → fire.
        • camera HAS zones, track touched none → hide (e.g. a car on the road
          in the corner of a camera whose only zone is the driveway). The
          operator opted into region gating by drawing one.
        • camera HAS zones, track crossed a zone but never cleared any
          `min_dwell_ms` → hide (parity with the zone_enter/exit pair; kills
          2-second drive-through spam once a sensible dwell is set).
        • the track is a PERSON → neither gate applies; see `zone_gate_verdict`.
          Zones shape what a camera reports about traffic, not whether it
          reports people.

        A verdict hides the track from the timeline and writes it anyway: see
        `_record_suppressed`.
        """
        if self._zones is None or not self._zones.zones_for(cam_slug):
            return None
        verdict = zone_gate_verdict(
            rec.touched_zone, rec.ever_enter_fired, rec.parked_finalized, is_person
        )
        if verdict is not None:
            log.info(
                "suppressed %s cam=%s tid=%s class=%s lifetime=%dms — recorded, "
                "hidden from the timeline%s",
                verdict,
                cam_slug,
                local_tid,
                rec.class_name,
                lifetime_ms,
                "; run the event-manager at DEBUG for the per-zone reason "
                "(min_confidence / min_area_pct / gate)"
                if verdict == "off-zone"
                else "",
            )
        return verdict

    def _static_verdict(
        self,
        cam_slug: str,
        local_tid: int,
        rec: TrackState,
        is_person: bool,
        lifetime_ms: int,
        movement_px: float,
    ) -> str | None:
        """How far did the bbox centre travel relative to the OBJECT'S OWN
        size? A parked car has near-zero displacement and would otherwise spawn
        a track and a clip every time the tracker drops and reacquires it."""
        # Normalise by the object's bbox diagonal, NOT the frame edge. Frame-
        # fraction is resolution-dependent the wrong way: on a 320px downscaled
        # cam (shed/backyard) a parked car's ~25px bbox jitter is ~8% of the
        # frame (> any sane frame threshold → never suppressed → endless clips),
        # yet it's a small fraction of the car. Object-relative travel is
        # resolution- AND distance-independent: parked car ≈ 0.1-0.3× its size,
        # a car that drives through moves many car-lengths.
        bw = rec.last_bbox[2] - rec.last_bbox[0]
        bh = rec.last_bbox[3] - rec.last_bbox[1]
        obj_diag = (bw * bw + bh * bh) ** 0.5
        movement_ratio = movement_px / max(1.0, obj_diag)
        # People are exempt. A person who sits down scores the same 0.1-0.3 as
        # a parked car (measured on the patio: ratio 0.12 over a 472px
        # diagonal), so applying this to `person` discards exactly the subject
        # the system must never lose. The cost asymmetry is absolute: a
        # duplicate person track is clutter, a missing one is a failed NVR.
        if (
            is_person
            or self._config.static_move_ratio <= 0
            or movement_ratio >= self._config.static_move_ratio
        ):
            return None
        log.info(
            "suppressed static cam=%s tid=%s class=%s lifetime=%dms "
            "move=%.1fpx ratio=%.2f (obj_diag=%.0fpx) — recorded, "
            "hidden from the timeline",
            cam_slug,
            local_tid,
            rec.class_name,
            lifetime_ms,
            movement_px,
            movement_ratio,
            obj_diag,
        )
        return "static"

    async def _finalize(self, cam_slug: str, local_tid: int, rec: TrackState) -> None:
        self._stats.incr("finalize_calls")
        await self._close_zones(cam_slug, local_tid, rec)
        closed = await self._classify(cam_slug, local_tid, rec)
        if closed is None:
            return
        if closed.hidden_by is not None:
            await self._record_suppressed(local_tid, rec, closed)
            return

        track_id = rec.db_track_id
        if track_id is None:
            raise RuntimeError("finalization requires a reserved track UUID")
        rel_path = await self._thumbnail(cam_slug, closed, rec, track_id)
        assert self._pool is not None
        async with self._pool.acquire() as conn:  # noqa: SIM117 (kept nested on purpose)
            async with conn.transaction():
                n_samples = await self._persist_track(
                    conn, closed, local_tid, rec, track_id, rel_path
                )
                if n_samples is None:
                    return
                naming = await name_track(
                    conn,
                    self._config,
                    self._live_identity,
                    cam_slug,
                    closed.cam.id,
                    local_tid,
                    rec.class_id,
                    track_id,
                    n_samples > 0,
                )
                await self._file_naming(conn, track_id, naming)
                await self._publish_finalized(conn, closed, rec, track_id)

        log.info(
            "finalized cam=%s tid=%s class=%s duration=%dms n=%d move=%.1fpx "
            "votes=%s thumb=%s emb_samples=%d emb_set=%s reid=%s%s%s gid=%s",
            cam_slug,
            local_tid,
            rec.class_name,
            closed.lifetime_ms,
            rec.n_observations,
            closed.movement_px,
            closed.class_votes,
            rel_path or "—",
            n_samples,
            "yes" if n_samples else "no",
            naming.decision,
            f"(dist={naming.dist:.3f})" if naming.dist is not None else "",
            "[face]" if naming.face else ("[body]" if naming.body_cluster else ""),
            str(naming.global_id)[:8],
        )

    async def _thumbnail(
        self, cam_slug: str, closed: _Closed, rec: TrackState, track_id: UUID
    ) -> str | None:
        """Best-effort, before the DB write. Primary: the box-baked JPEG captured
        live from the clearest detection's own SHM frame (snapshot.py) — box on
        the subject by construction. Fallbacks (no box): a recording frame at the
        track midpoint, then a live go2rtc snapshot."""
        rel_path: str | None = None
        if rec.best_thumb_jpeg is not None:
            rel_path = self._thumbs.save_jpeg(str(track_id), rec.best_thumb_jpeg)
        if rel_path is None:
            try:
                mid_ns = (closed.start_ns + closed.end_ns) // 2
                mid_dt = datetime.fromtimestamp(mid_ns / 1e9, tz=UTC)
                assert self._pool is not None
                async with self._pool.acquire() as conn:
                    seg = await conn.fetchrow(
                        """
                        SELECT path, started_at FROM recordings
                        WHERE camera_id = $1
                          AND ended_at IS NOT NULL
                          AND started_at <= $2 AND $2 < ended_at
                        ORDER BY started_at DESC LIMIT 1
                        """,
                        closed.cam.id,
                        mid_dt,
                    )
                if seg is not None:
                    offset = (mid_dt - seg["started_at"]).total_seconds()
                    rel_path = await self._thumbs.from_recording(
                        str(track_id), seg["path"], offset
                    )
            except Exception:
                log.exception("recording-based thumbnail failed")
        if rel_path is None:
            rel_path = await self._thumbs.from_live_snapshot(cam_slug, str(track_id))
        return rel_path

    async def _persist_track(
        self,
        conn,
        closed: _Closed,
        local_tid: int,
        rec: TrackState,
        track_id: UUID,
        rel_path: str | None,
    ) -> int | None:
        """The track row, the embedding samples it claims and its canonical
        body and face embeddings. Returns how many samples it holds."""
        inserted = await conn.fetchval(
            """
            INSERT INTO tracks_all (id, camera_id, local_track_id, class_id, class_name,
                                started_at, ended_at, n_observations, thumbnail_path,
                                ever_active, came_to_rest)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (id) DO NOTHING RETURNING id
            """,
            track_id,
            closed.cam.id,
            local_tid,
            rec.class_id,
            rec.class_name,
            closed.started_at,
            closed.ended_at,
            rec.n_observations,
            rel_path,
            rec.ever_active,
            rec.came_to_rest,
        )
        if inserted is None:
            return None
        # Claim every embedding sample produced during this track's
        # lifetime (camera + local_track_id, within ±2s of the
        # finalized track window). The padding absorbs the small
        # delay between observation and the embedder's INSERT.
        await conn.execute(
            """
            UPDATE track_embedding_samples
            SET track_id = $1
            WHERE camera_id = $2
              AND local_track_id = $3
              AND track_id IS NULL
              AND captured_at BETWEEN $4 AND $5
            """,
            track_id,
            closed.cam.id,
            local_tid,
            closed.started_at - _CLAIM_WINDOW_SLACK,
            closed.ended_at + _CLAIM_WINDOW_SLACK,
        )
        # Copy the highest-confidence sample's embedding into the
        # canonical `tracks.embedding` slot. Done in pure SQL so we
        # never have to hand the vector through Python — asyncpg
        # has no native pgvector codec and we'd otherwise need the
        # pgvector library's `register_vector()` setup.
        n_total = await conn.fetchval(
            "SELECT COUNT(*) FROM track_embedding_samples WHERE track_id = $1",
            track_id,
        )
        if n_total:
            # Body side: best by confidence supplies canonical body embedding +
            # display crop. Face side: best *that has a face_embedding* supplies
            # the canonical face embedding + face crop. They come from different
            # samples and are ranked by DIFFERENT numbers, which is the whole
            # point — and they are two statements, because the face pick finds
            # no row for a track without a face, which is every vehicle, and one
            # joined statement dropped the body pick along with it.
            await conn.execute(
                """
                UPDATE tracks SET
                    embedding = body_best.embedding,
                    crop_path = body_best.crop_path
                FROM (
                    SELECT embedding, crop_path
                    FROM track_embedding_samples
                    WHERE track_id = $1
                    -- Confidence says nothing about what is in the
                    -- frame. A vehicle that came to rest
                    -- overlapping its neighbour takes its sharpest
                    -- crops exactly there — the same resting box
                    -- that read the neighbour's plate embeds the
                    -- neighbour just as willingly. So for a vehicle
                    -- a moving sample wins outright; a resting one
                    -- still qualifies when the whole track was one.
                    -- A person's stillness is presence, so only
                    -- vehicles are sorted this way.
                    ORDER BY ($2::bool AND at_rest IS TRUE) ASC,
                             confidence DESC
                    LIMIT 1
                ) AS body_best
                WHERE tracks.id = $1
                """,
                track_id,
                rec.class_id in VEHICLE_GROUP,
            )
            await set_canonical_face(conn, track_id, self._config.face_embedding_model)
        return n_total

    async def _file_naming(self, conn, track_id: UUID, naming: Naming) -> None:
        """Write who the track is: the identity, what decided it, how long the
        row is kept, and the audit trail behind an automatic name."""
        face = naming.face
        if face is not None and face["global_id"] is None and face.get("id"):
            # A pre-re-ID neighbour lent its own id as the identity; it takes
            # that identity too.
            await conn.execute(
                "UPDATE tracks SET global_id = $1 WHERE id = $2", face["id"], face["id"]
            )
        await conn.execute(
            f"""
            UPDATE tracks
            SET global_id = $1,
                identity_source = $3,
                retain_until = ended_at + (
                    {tier_sql("$1")}
                )
            WHERE id = $2
            """,  # noqa: S608
            naming.global_id,
            track_id,
            # What actually decided this name, so a place can report its
            # evidence instead of inferring it from a plate string.
            naming.source,
        )
        if naming.decision == "match":
            self._stats.incr("reid_match")
            if face is None:
                self._stats.incr("reid_body_cluster")
        elif naming.decision == "new":
            self._stats.incr("reid_new")
        elif naming.decision == "face_anchor_body":
            self._stats.incr("reid_face_anchor_body")

        # Audit-log auto-matches: gives the UI a live NOTIFY and a record of
        # where each sighting was identified — 'face' (cross-session, named or
        # anonymous) or 'body' (anonymous same-day same-camera cluster).
        # face_verified is set ONLY for face matches.
        if naming.decision == "match" and naming.dist is not None:
            if face is not None:
                modality = "face"
                threshold = self._config.reid_face_cosine_threshold
                source = face["source"]
            else:
                modality = "body"
                threshold = self._config.reid_body_cluster_threshold
                source = "body-cluster"
            await conn.execute(
                "INSERT INTO identity_audit (user_id, op, payload) VALUES (NULL, 'auto_match', $1::jsonb)",
                json.dumps(
                    {
                        "gid": str(naming.global_id),
                        "track_id": str(track_id),
                        "dist": round(naming.dist, 4),
                        "threshold": threshold,
                        "source": source,
                        "modality": modality,
                    }
                ),
            )
            if face is not None:
                await self._mark_face_verified(conn, track_id, "finalize")

        if naming.live_face:
            # The adopted live verdict passed the same face gate over the same
            # samples the finalize chain uses, so the track is face-verified by
            # it — the whole visit counts as a face identification, not just
            # the frames after the face landed.
            await self._mark_face_verified(conn, track_id, "live verdict")

        # Audited as its own modality so it's distinguishable from a face match
        # in the trail, and deliberately does NOT set face_verified: this track
        # was named by its BODY, so it must never itself serve as a face anchor
        # for a further body chain (that's the anti-drift invariant — only real
        # face confirmations anchor).
        if naming.decision == "face_anchor_body" and naming.anchor_dist is not None:
            thr = (
                self._config.reid_face_anchor_xcam_threshold
                if naming.anchor_xcam
                else self._config.reid_face_anchor_body_threshold
            )
            log.info(
                "face-anchored body chain: track %s adopted identity %s "
                "(body dist %.3f < %.3f, %s camera, face-verified anchor)",
                track_id,
                naming.global_id,
                naming.anchor_dist,
                thr,
                "cross" if naming.anchor_xcam else "same",
            )
            await conn.execute(
                "INSERT INTO identity_audit (user_id, op, payload) "
                "VALUES (NULL, 'auto_match', $1::jsonb)",
                json.dumps(
                    {
                        "gid": str(naming.global_id),
                        "track_id": str(track_id),
                        "dist": round(naming.anchor_dist, 4),
                        "threshold": thr,
                        "source": "face-anchor-xcam" if naming.anchor_xcam else "face-anchor",
                        "modality": "body-anchor",
                    }
                ),
            )

    async def _publish_finalized(
        self, conn, closed: _Closed, rec: TrackState, track_id: UUID
    ) -> None:
        await conn.execute(
            """
            INSERT INTO events (camera_id, track_id, kind, at, payload)
            VALUES ($1, $2, 'track_finalized', $3,
                    jsonb_build_object(
                      'class_id', $4::int,
                      'class_name', $5::text,
                      'duration_ms', $6::int,
                      'n_observations', $7::int,
                      'max_confidence', $8::double precision,
                      'movement_px', $9::double precision,
                      'class_votes', $10::jsonb,
                      'last_bbox', jsonb_build_object(
                        'x1', $11::double precision, 'y1', $12::double precision,
                        'x2', $13::double precision, 'y2', $14::double precision
                      )
                    ))
            """,
            closed.cam.id,
            track_id,
            closed.ended_at,
            rec.class_id,
            rec.class_name,
            closed.lifetime_ms,
            rec.n_observations,
            rec.max_confidence,
            closed.movement_px,
            json.dumps(closed.class_votes),
            rec.last_bbox[0],
            rec.last_bbox[1],
            rec.last_bbox[2],
            rec.last_bbox[3],
        )


async def _run() -> None:
    config = EventManagerConfig.from_env()
    await EventManager(config).run()


def main() -> None:
    setup_logging("event-manager")
    run_service(_run())


if __name__ == "__main__":
    main()
