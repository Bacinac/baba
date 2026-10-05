"""State-evaluator service.

Evaluates the persistent state of fixed scene regions (gate open/closed,
garage door up/down, light on/off) on a slow per-region cadence. There is no
detector class for these — we few-shot them:

  * the operator draws a region polygon and captures a handful of reference
    crops per state ("open", "closed"), which we embed with DINOv2 (a
    general-purpose visual feature extractor — the right tool for scene
    structure, unlike OSNet which is tuned for person identity). A reference
    can come from the live ring OR from a recorded moment (`at`), because a
    state you want to teach is usually not the one happening right now;
  * every `sample_interval_s` we grab the latest frame from the ingestor's SHM
    ring, crop + mask the region, embed it, and nearest-neighbour it against
    the prototypes;
  * a state only *commits* after `hysteresis_n` consecutive identical
    classifications (debounces a momentary occlusion / lighting blip), and a
    nearest distance beyond `unknown_margin` commits to 'unknown' (occluded
    gate, car parked in front) rather than guessing.

Transitions become ordinary `scene_state_change` rows in `events`, which fires
NOTIFY 'events_new' — the same path zone/track events take to the API
WebSocket, Activity feed, and DIDA bus. No new transport.

The service also answers two NATS request/reply control subjects so the thin
API container (which has no frame-ring access) can drive capture + live test:
  * baba.rpc.state.capture {region_id, state_label, at?} → take the frame (live
    ring, or decoded from the recording covering `at`), crop, embed, store a
    prototype; reply {prototype_id, crop_path}.
  * baba.rpc.state.eval    {region_id}                   → classify right now
    without committing; reply {state, raw_label, distance, per_state}.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import cv2
import numpy as np
from baba_core import (
    DINOv2OnnxBackend,
    FrameRingReader,
    StatsCollector,
    cap_long_edge,
    drain_quietly,
    dsn_from_env,
    parse_vector,
    setup_logging,
    vector_literal,
)
from baba_core.ffmpeg_decoder import ffprobe_stream, scaled_dims
from baba_core.frame_ring import PIXEL_FORMAT_NV12, RingFrame
from baba_core.nats_conn import connect as nats_connect
from baba_core.occupancy import (
    announce_episodes,
    claim_place,
    name_departures,
    open_missing_episodes,
    reconcile_places,
    release_place_if_free,
)
from baba_core.paths import SCENE_CROPS, MediaLayout
from baba_core.pg_listen import ResilientListener
from baba_core.plate_stack import make_plate_stack
from baba_core.recordings import covers_until_sql
from baba_core.runtime import run_service
from baba_core.wire import SUBJECT_STATE_CAPTURE, SUBJECT_STATE_EVAL
from home_core.health import HealthMarker
from home_core.tasks import spawn

from baba_state_evaluator.headlight import HeadlightWatcher
from baba_state_evaluator.plate_reader import PlateReader

log = logging.getLogger("baba.state-evaluator")

# Decoding one frame out of a recorded segment is an operator-driven action,
# not a hot path: a generous ceiling that still can't wedge the control loop.
_RECORDING_DECODE_TIMEOUT_S = 25.0

# Loop wakes this often; each region is actually evaluated only every
# `sample_interval_s` (scene state changes slowly — seconds, not frames).
TICK_S = 1.0

# Floor for the staleness window: a region's frame is "stale" when the ring
# sequence hasn't advanced for at least max(2*sample_interval_s, this). The
# floor keeps a slow (e.g. 1 fps) camera sampled on a short interval from being
# mislabelled stale between frames.
_STALE_FLOOR_S = 6.0

# A region polygon's bbox must be at least this many px on each side for a
# meaningful crop. Below this DINOv2 has nothing to look at.
MIN_REGION_PX = 24

# Where reference crops land, relative to the media root.


# --- crop + classify helpers ------------------------------------------------


def _region_rgb_crop(
    frame: RingFrame, polygon_norm: list[list[float]]
) -> tuple[np.ndarray, float] | None:
    """Return a uint8 RGB crop of the region (everything outside the polygon
    masked to black, then tight-bbox cropped, long edge capped) and the spread
    of luminance INSIDE the polygon, or None if the polygon is degenerate / too
    small.

    The spread is measured before the long-edge cap and only over the polygon's
    own pixels, so the black the mask paints around a non-rectangular region
    cannot pass for darkness the camera actually saw.

    Masking to the polygon makes the region definition meaningful: a person
    walking through a corner of the bounding box doesn't perturb the embedding,
    and capture + evaluation share the EXACT same representation (which is all
    nearest-neighbour matching needs). At a few-seconds cadence per region the
    full-frame NV12→RGB conversion is immaterial, so we take the simple,
    robust path rather than the embedder's chroma-aligned partial crop.
    """
    if frame.pixel_format == PIXEL_FORMAT_NV12:
        rgb = cv2.cvtColor(frame.pixels, cv2.COLOR_YUV2RGB_NV12)
    else:
        rgb = frame.pixels
    h, w = rgb.shape[:2]
    if not polygon_norm or len(polygon_norm) < 3:
        return None
    pts = np.array(
        [[round(p[0] * w), round(p[1] * h)] for p in polygon_norm], dtype=np.int32
    )
    pts[:, 0] = np.clip(pts[:, 0], 0, w - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, h - 1)
    x1, y1 = int(pts[:, 0].min()), int(pts[:, 1].min())
    x2, y2 = int(pts[:, 0].max()) + 1, int(pts[:, 1].max()) + 1
    if x2 - x1 < MIN_REGION_PX or y2 - y1 < MIN_REGION_PX:
        return None
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 255)
    masked = cv2.bitwise_and(rgb, rgb, mask=mask)
    crop = np.ascontiguousarray(masked[y1:y2, x1:x2])
    inside = mask[y1:y2, x1:x2] > 0
    lum = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    contrast = float(lum[inside].std()) if inside.any() else 0.0
    return cap_long_edge(crop), contrast


def _classify(
    query: np.ndarray,
    protos: list[tuple[str, np.ndarray]],
    states: Sequence[str] | None = None,
) -> tuple[str | None, float, dict[str, float]]:
    """Nearest-neighbour over the few-shot prototypes. All vectors are
    L2-normalised, so cosine distance = 1 - dot, in [0, 2] (same space as the
    pgvector <=> operator the rest of BABA uses). Returns the best label, its
    distance, and the per-state minimum distance (for the Test panel).

    `states` is what the region is ALLOWED to be, and a prototype carrying any
    other label is ignored. Without that the column was decoration and any row
    that reached the table could become a committed state: a probe capture,
    deleted from the table a second later, still sat in the in-memory set long
    enough for three regions to commit `__probe__` and file transitions for it.
    A state the operator never declared is not a state.
    """
    allowed = frozenset(states) if states else None
    best_label: str | None = None
    best_dist = 2.0
    per_state: dict[str, float] = {}
    for label, vec in protos:
        if allowed is not None and label not in allowed:
            continue
        d = 1.0 - float(np.dot(query, vec))
        if d < per_state.get(label, 2.0):
            per_state[label] = d
        if d < best_dist:
            best_dist = d
            best_label = label
    return best_label, best_dist, per_state


# How far the winning state must beat the runner-up before the verdict counts as
# an answer at all. A distance inside `unknown_margin` says the picture
# RESEMBLES something trained; it does not say the region can tell its states
# apart. Shed's P1 spent the dark hour of 31.08 choosing between empty=0.355 and
# present=0.375 — and at 05:15 between 0.388 and 0.389 — then committed the
# winner of a coin toss and released a car that had not moved.
#
# Measured on the same regions: a real answer wins by a mile. West P2's actual
# departure that morning was 0.164 against 0.727, the daytime reads 0.033
# against 0.600, a blinded frame 0.000 against 0.533. Every honest verdict
# cleared 0.26; every coin toss was under 0.07.
_SEPARATION = 0.10


# Below this spread of luminance the region carries no picture, and no
# appearance match over it means anything. Measured on west across the night of
# 02.-03.09, every region, from the recordings: while the floodlight was off
# (20:30, and 05:00 through 05:30) the four place regions read 0.0-2.0; every
# frame that carried a picture — floodlit night, dawn, daylight — read 9.9-63.
# Nothing has ever been measured in between. This covers darkness only: the IR
# glare that whites out west's left third still reads 26.6 and is a picture the
# region can be wrong about, not an absence of one.
_BLIND_CONTRAST = 5.0

# How much a region may change between two consecutive samples and still be a
# scene rather than something in front of the lens. A place holds still; rain on
# the glass under the IR floodlight does not — the drops and the insects they
# catch are a different picture every sample, bright enough to pass for a scene.
# West P1, 15.09 05:45: that glare read `empty` over a car that never moved.
#
# Measured on west P1 from the recordings, two samples 8 s apart as the
# evaluator takes them: under the glare (05:32-06:04) every pair read 11.4-42.2.
# Over the seven days before, 1008 pairs: 977 under 3, and what rose above that
# was a transient — cloud shadow crossing a parked car at 15:20, insects in the
# IR at 05:20, the floodlight switching. A transient costs a withheld verdict or
# two, which the hysteresis absorbs; the glare withholds for as long as it lasts.
_UNSTEADY = 6.0
_THUMB_W = 64

# How long a region may go without an answer — `unknown` or a frozen frame —
# before the state it holds stops being published as true. Withheld verdicts
# are meant to be short: a car crossing the gate, the leaf swinging, the IR
# switch at dusk cost a few samples. On 05.10 the gate read `unknown` for ten
# minutes over a closed gate, and the state it held said open the whole time.
_UNSURE_AFTER_S = 180.0


def verdict(
    best_label: str | None, best_dist: float, per_state: dict[str, float],
    unknown_margin: float,
) -> str:
    """What the region is allowed to SAY on appearance: its label, or `unknown`."""
    if best_label is None or best_dist > unknown_margin:
        return "unknown"
    others = sorted(d for label, d in per_state.items() if label != best_label)
    if others and others[0] - best_dist < _SEPARATION:
        return "unknown"
    return best_label


def teachable(state_label: str, contrast: float) -> bool:
    """Whether a crop showing this much may become a reference for this class.

    The same measurement the read side makes, made where references are
    WRITTEN. It was computed on this path already and thrown away, so a crop
    with nothing in it could be enrolled as what a place looks like — and the
    nightly harvest runs at 05:15, which on west is inside the hour the left
    third of the frame is mean 0.0. A reference taught from a blind frame
    teaches the class to mean "blind", and then the region says `empty` at
    every dawn.

    `blinded` is the exception by construction: it is the one class whose
    references are supposed to show nothing.
    """
    return state_label == "blinded" or contrast >= _BLIND_CONTRAST


def region_thumb(crop: np.ndarray) -> np.ndarray:
    """The region at a size where only structure survives: sensor noise and
    compression shimmer average out, a car driving in or a sheet of lit drops
    does not."""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]
    size = (_THUMB_W, max(1, round(_THUMB_W * h / w)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.float32)


def unsteadiness(previous: np.ndarray | None, current: np.ndarray) -> float | None:
    """Mean absolute luminance change between two samples' thumbnails, or None
    when there is no comparable previous sample."""
    if previous is None or previous.shape != current.shape:
        return None
    return float(np.abs(current - previous).mean())


def read_region(
    contrast: float,
    vector: np.ndarray,
    protos: list[tuple[str, np.ndarray]],
    states: Sequence[str] | None,
    unknown_margin: float,
    change: float | None = None,
) -> tuple[str, float, dict[str, float]]:
    """What the region says right now, as one answer for every caller.

    Blindness is MEASURED, never matched. Taught as an appearance, `blinded`
    was a set of black reference crops, and nearest-neighbour has no notion of
    "this one means there was nothing to see" — so the label behaved like any
    other and collected whatever came closest. West P1 holds a black car
    photographed from two metres; on 31.08, in 117-luma daylight, it flipped
    present<->blinded twelve times between 13:19 and 14:50 at 0.28-0.40,
    because a dark bonnet filling the frame really is nearer a black frame than
    it is to the yard behind it. A `blinded` region abstains from the departure
    vote, so every one of those flips was the place quietly becoming
    unreleasable.

    A region may only say `blinded` if the operator declared it can — the same
    rule the appearance path follows — and the states it is judged on never
    include it, so the classifier can no longer reach for it.
    """
    judged = [s for s in (states or ()) if s != "blinded"] or None
    best_label, best_dist, per_state = _classify(vector, protos, judged)
    if states and "blinded" in states and contrast < _BLIND_CONTRAST:
        return "blinded", best_dist, per_state
    if change is not None and change >= _UNSTEADY:
        return "unknown", best_dist, per_state
    return (
        verdict(best_label, best_dist, per_state, unknown_margin),
        best_dist,
        per_state,
    )


# pgvector <-> numpy helpers live in baba_core.pgvector (vector_literal /
# parse_vector), imported above.


# --- config + runtime -------------------------------------------------------


@dataclass(slots=True)
class RegionConfig:
    id: UUID
    camera_id: UUID
    camera_slug: str
    name: str
    polygon: list[list[float]]
    states: list[str]
    sample_interval_s: int
    hysteresis_n: int
    unknown_margin: float
    enabled: bool
    # The physical spot this region watches, when it is one several cameras can
    # see. NULL for a region that only stands for itself (a gate, a doorway).
    place: str | None = None


@dataclass(slots=True)
class RegionRuntime:
    current_state: str | None = None
    pending_label: str | None = None
    pending_count: int = 0
    next_eval_at: float = 0.0  # monotonic clock
    # Staleness tracking: the newest ring sequence we last saw advance, and the
    # monotonic time it advanced. A frozen camera keeps the same sequence.
    last_seen_seq: int | None = None
    last_new_seq_at: float = 0.0  # monotonic clock
    last_thumb: np.ndarray | None = None
    last_thumb_at: float = 0.0  # monotonic clock


@dataclass(slots=True)
class _Frame:
    """A captured-and-embedded region crop, threaded out of the executor."""

    crop: np.ndarray
    vector: np.ndarray
    contrast: float


class CaptureError(Exception):
    """A capture failed for a reason the operator can act on. `reason` is a
    stable slug the UI maps to a message (same contract as the existing
    no_frame / bad_region replies)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_at(raw: object) -> datetime | None:
    """Optional 'capture from a recorded moment' timestamp. Absent/empty means
    the live ring. A naive value is read as UTC — the API sends an aware one."""
    if raw in (None, ""):
        return None
    at = datetime.fromisoformat(str(raw))
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at


# --- service core -----------------------------------------------------------


class StateEvaluator:
    def __init__(
        self,
        dsn: str,
        nats_url: str,
        backend: DINOv2OnnxBackend,
        media_root: Path,
        model_key: str,
    ) -> None:
        self._dsn = dsn
        self._nats_url = nats_url
        self._backend = backend
        self._media_root = media_root
        self._model_key = model_key
        self._crops_dir = MediaLayout(media_root).scene_crops
        self._crops_dir.mkdir(parents=True, exist_ok=True)
        self._pool: asyncpg.Pool | None = None
        self._nc = None
        self._listener: ResilientListener | None = None
        self._readers: dict[str, FrameRingReader] = {}
        self._regions: dict[UUID, RegionConfig] = {}
        self._protos: dict[UUID, list[tuple[str, np.ndarray]]] = {}
        self._runtime: dict[UUID, RegionRuntime] = {}
        self._stats = StatsCollector(service="state-evaluator")
        # Serialises config reconcile against the eval loop + control requests,
        # and serialises ONNX inference (one session, low volume).
        self._lock = asyncio.Lock()
        # Monotonic time of the last FRESH region evaluation (a real read→embed→
        # classify, not a 'stale' hold). The watchdog in _run() watches this to
        # detect a wedged eval loop; seeded to "now" so it has a full grace window.
        self._last_progress = time.monotonic()

    def _reader_for(self, slug: str) -> FrameRingReader:
        r = self._readers.get(slug)
        if r is None:
            r = FrameRingReader(slug)
            self._readers[slug] = r
        return r

    @property
    def pool(self) -> Any:
        """The connection pool, for passengers that share this service's DB
        access rather than opening a second pool against the same database."""
        assert self._pool is not None
        return self._pool

    @property
    def nc(self) -> Any:
        """This service's NATS connection, on the same terms as `pool`."""
        assert self._nc is not None
        return self._nc

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        self._listener = ResilientListener(
            self._dsn,
            ["scene_regions_changed", "cameras_changed"],
            on_notify=self._on_changed,
            on_connect=self._reconcile,
            name="state-evaluator-listen",
        )
        await self._listener.start()
        self._nc = await nats_connect(self._nats_url, name="state-evaluator")
        await self._nc.subscribe(SUBJECT_STATE_CAPTURE, cb=self._on_capture_request)
        await self._nc.subscribe(SUBJECT_STATE_EVAL, cb=self._on_eval_request)
        await self._stats.start(self._nc)
        log.info(
            "state-evaluator ready: control subjects %s / %s, model=%s",
            SUBJECT_STATE_CAPTURE,
            SUBJECT_STATE_EVAL,
            self._model_key,
        )

    def _on_changed(self, *_a) -> None:
        if self._pool is not None:
            spawn(self._reconcile())

    async def _reconcile(self) -> None:
        """Reload region config + prototypes + persisted status from the DB.
        Region/prototype sets are small (a handful), so a full rebuild on every
        change is cheaper than tracking deltas. Current committed state is
        carried over from scene_region_status so a reload never re-emits a
        transition for a state that was already current; hysteresis counters
        reset (the next few ticks rebuild them)."""
        assert self._pool is not None
        async with self._lock:
            async with self._pool.acquire() as conn:
                region_rows = await conn.fetch(
                    """
                    SELECT r.id, r.camera_id, c.slug AS camera_slug, r.name,
                           r.polygon, r.states, r.sample_interval_s,
                           r.hysteresis_n, r.unknown_margin, r.enabled,
                           r.place
                    FROM scene_regions r
                    JOIN cameras c ON c.id = r.camera_id
                    WHERE c.enabled
                    """
                )
                proto_rows = await conn.fetch(
                    """
                    SELECT region_id, state_label, embedding::text AS embedding
                    FROM scene_region_prototypes
                    WHERE embedding IS NOT NULL
                    """
                )
                # A disabled region is not being watched, so its last belief
                # must not be resumed when it comes back.
                await conn.execute(
                    """
                    DELETE FROM scene_region_status s
                    USING scene_regions r
                    WHERE s.region_id = r.id AND NOT r.enabled
                    """
                )
                status_rows = await conn.fetch(
                    "SELECT region_id, current_state FROM scene_region_status"
                )

            status_map = {r["region_id"]: r["current_state"] for r in status_rows}
            protos: dict[UUID, list[tuple[str, np.ndarray]]] = {}
            for r in proto_rows:
                protos.setdefault(r["region_id"], []).append(
                    (r["state_label"], parse_vector(r["embedding"]))
                )

            now = time.monotonic()
            regions: dict[UUID, RegionConfig] = {}
            runtime: dict[UUID, RegionRuntime] = {}
            for r in region_rows:
                rid = r["id"]
                regions[rid] = RegionConfig(
                    id=rid,
                    camera_id=r["camera_id"],
                    camera_slug=r["camera_slug"],
                    name=r["name"],
                    polygon=json.loads(r["polygon"]) if isinstance(r["polygon"], str) else r["polygon"],
                    states=list(r["states"] or []),
                    sample_interval_s=r["sample_interval_s"],
                    hysteresis_n=r["hysteresis_n"],
                    unknown_margin=float(r["unknown_margin"]),
                    enabled=r["enabled"],
                    place=r["place"],
                )
                runtime[rid] = RegionRuntime(
                    current_state=status_map.get(rid), next_eval_at=now
                )

            self._regions = regions
            self._protos = protos
            self._runtime = runtime
            self._stats.set_gauge("regions", len(regions))
        log.info(
            "reconciled: %d region(s), %d with prototypes",
            len(self._regions),
            sum(1 for rid in self._regions if self._protos.get(rid)),
        )
        await self._sweep_orphan_crops()

    async def _sweep_orphan_crops(self) -> None:
        """Drop scene crops no prototype points at any more.

        The crop is written before its row is inserted, so anything failing in
        between leaves a file nothing will ever look at again, and nothing
        removed it. Only files older than an hour are candidates, which is
        several orders of magnitude past the gap between those two writes.
        """
        assert self._pool is not None
        if not self._crops_dir.is_dir():
            return
        cutoff = time.time() - 3600

        def _candidates() -> list[Path]:
            return [
                f for f in self._crops_dir.iterdir()
                if f.is_file() and f.stat().st_mtime < cutoff
            ]

        try:
            files = await asyncio.to_thread(_candidates)
        except OSError:
            log.exception("scene crop sweep: cannot list %s", self._crops_dir)
            return
        if not files:
            return
        rels = [MediaLayout.rel(SCENE_CROPS, f.name) for f in files]
        async with self._pool.acquire() as conn:
            kept = await conn.fetch(
                "SELECT crop_path FROM scene_region_prototypes"
                " WHERE crop_path = ANY($1)",
                rels,
            )
        keep = {r["crop_path"] for r in kept}
        unlinked = 0
        for f, rel in zip(files, rels, strict=True):
            if rel in keep:
                continue
            try:
                f.unlink(missing_ok=True)
                unlinked += 1
            except OSError:
                log.exception("scene crop sweep: cannot unlink %s", f)
        if unlinked:
            log.info("scene crop sweep: unlinked %d unreferenced file(s)", unlinked)

    # --- periodic evaluation ------------------------------------------------

    async def run_eval_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = time.monotonic()
            # Snapshot the ids under the lock; evaluate outside the per-region
            # path which re-acquires the lock itself.
            due: list[UUID] = []
            async with self._lock:
                for rid, region in self._regions.items():
                    if not region.enabled:
                        continue
                    rt = self._runtime.get(rid)
                    if rt is not None and now >= rt.next_eval_at:
                        due.append(rid)
            for rid in due:
                try:
                    await self._tick_region(rid)
                except Exception:
                    # One bad tick must not kill the loop; log loud, keep going.
                    self._stats.incr("tick_errors")
                    log.exception("region %s evaluation tick failed", rid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)

    async def _tick_region(self, region_id: UUID) -> None:
        async with self._lock:
            region = self._regions.get(region_id)
            rt = self._runtime.get(region_id)
            if region is None or rt is None:
                return
            # Re-arm next slot regardless of outcome so a frameless camera
            # doesn't get hammered every tick.
            rt.next_eval_at = time.monotonic() + region.sample_interval_s
            protos = self._protos.get(region_id) or []
            if len({lbl for lbl, _ in protos}) < 2:
                return  # need >= 2 labelled states to classify
            frame = self._reader_for(region.camera_slug).get_latest()
            if frame is None:
                return
            # Staleness guard. SHM readers survive ingestor/camera death (the
            # frame ring is a shared /dev/shm volume), so get_latest() keeps
            # returning the last frame indefinitely. Classifying a frozen frame
            # would show e.g. "gate closed", freshly evaluated, for hours after
            # the camera actually died — the silent degradation the fail-loud
            # rule forbids. A live camera pushes new frames with a monotonically
            # advancing sequence; if the newest sequence hasn't advanced for a
            # couple of sample intervals, the ring is frozen. Hold the last
            # confident state, surface 'stale', and evaluate nothing. Using the
            # sequence (not pts_ns, which is monotonic-since-decoder-open and
            # resets on reconnect) plus the evaluator's own monotonic clock keeps
            # slow-but-alive cameras from being flagged.
            now_mono = time.monotonic()
            if frame.sequence != rt.last_seen_seq:
                rt.last_seen_seq = frame.sequence
                rt.last_new_seq_at = now_mono
            elif now_mono - rt.last_new_seq_at >= max(
                2.0 * region.sample_interval_s, _STALE_FLOOR_S
            ):
                self._stats.incr("stale_frames")
                rt.pending_label = None
                rt.pending_count = 0
                await self._status_mark_stale(region_id)
                return
            cap = await self._embed_region(frame, region.polygon)
            if cap is None:
                self._stats.incr("bad_region")
                return
            self._stats.incr("evaluations")
            thumb = region_thumb(cap.crop)
            # Only the sample just before is a comparison: after a gap the scene
            # is allowed to have moved on.
            recent = now_mono - rt.last_thumb_at <= 2.0 * region.sample_interval_s
            change = unsteadiness(rt.last_thumb if recent else None, thumb)
            rt.last_thumb, rt.last_thumb_at = thumb, now_mono
            raw_label, best_dist, _per_state = read_region(
                cap.contrast, cap.vector, protos, region.states,
                region.unknown_margin, change,
            )
            if change is not None and change >= _UNSTEADY:
                self._stats.incr("unsteady")
            is_known = raw_label != "unknown"
            await self._status_eval_tick(region_id, raw_label, best_dist)
            # The whole read→embed→classify path just ran on a fresh frame — the
            # heartbeat the watchdog trusts (an 'unknown' still counts: the
            # pipeline is alive, only the appearance is untrained). A stale frame
            # returns above WITHOUT reaching here, so a wedged ring stops this.
            self._last_progress = time.monotonic()

            # 'unknown' = nearest prototype is beyond the margin, i.e. an
            # un-trained appearance: dusk, the day↔night IR cut-filter switch,
            # headlight glare, heavy rain. Treat it as "sensor unsure" — HOLD
            # the last confident state and emit NOTHING, so a lighting change
            # never masquerades as a gate state change. (Capture references in
            # that condition to actually classify it instead of holding.) Reset
            # the hysteresis run so a genuine state after the unknown period has
            # to re-confirm from scratch.
            if not is_known:
                rt.pending_label = None
                rt.pending_count = 0
                return

            # Hysteresis: only commit after N consecutive identical reads.
            if raw_label == rt.pending_label:
                rt.pending_count += 1
            else:
                rt.pending_label = raw_label
                rt.pending_count = 1
            if rt.pending_count >= region.hysteresis_n and raw_label != rt.current_state:
                from_state = rt.current_state
                rt.current_state = raw_label
                await self._commit_transition(
                    region, from_state, raw_label, best_dist
                )

    async def _frame_from_recording(self, region: RegionConfig, at: datetime) -> RingFrame:
        """The frame this camera saw at `at`, decoded out of its recorded
        segment and handed back exactly as the SHM ring would have carried it:
        scaled to the ingestor's geometry and in NV12.

        Both of those are load-bearing, because a prototype is only ever
        compared against other vectors in this space:

          * geometry — `_region_rgb_crop` masks a NORMALISED polygon and
            `cap_long_edge` never upscales, so the same region cropped from the
            full-resolution segment reaches DINOv2 sharper and larger than the
            downscaled ring crop of the identical scene. Re-scaling here with
            the camera's own `downscale_max_edge` (via the same `scaled_dims`
            the decoders use) puts the crop back at live size.
          * NV12 — the live path pays cv2's `COLOR_YUV2RGB_NV12`, which is
            BT.601, while the stream is BT.709. Handing over RGB that ffmpeg
            converted correctly would shift every colour away from what the
            live captures encode. Capture and evaluation only have to agree
            with EACH OTHER, so we reproduce the live path, not the right one.

        Residual difference vs a live capture: swscale here vs the decoder's
        (possibly hardware) scaler, and nothing else — the segment is a stream
        copy of the very frames the ingestor saw.
        """
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            SELECT r.path, r.started_at, c.downscale_max_edge
            FROM recordings r
            JOIN cameras c ON c.id = r.camera_id
            WHERE r.camera_id = $1
              AND r.started_at <= $2
              AND $2 < {covers_until_sql("r.started_at", "r.ended_at")}
            ORDER BY r.started_at DESC
            LIMIT 1
            """,  # noqa: S608
            region.camera_id,
            at,
        )
        # Recording is activity-driven, so the archive has holes by design: a
        # moment with no segment is a normal answer, not a fault.
        if row is None:
            raise CaptureError("no_recording")
        seg = self._media_root / row["path"]
        if not seg.exists():
            # Row outlived the file (retention prune races the index).
            raise CaptureError("recording_file_missing")
        offset_s = (at - row["started_at"]).total_seconds()

        try:
            _codec, native_w, native_h = await ffprobe_stream(str(seg))
        except RuntimeError as e:
            log.warning("probe failed for %s: %s", seg, e)
            raise CaptureError("decode_failed") from None
        out_w, out_h = scaled_dims(native_w, native_h, row["downscale_max_edge"])

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            # Before -i: seek by index, then decode forward to the exact
            # timestamp. Cheap even on a 60s segment with sparse keyframes.
            "-ss",
            f"{offset_s:.3f}",
            "-i",
            str(seg),
            "-frames:v",
            "1",
            "-vf",
            f"scale={out_w}:{out_h}",
            "-pix_fmt",
            "nv12",
            "-f",
            "rawvideo",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=_RECORDING_DECODE_TIMEOUT_S
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise CaptureError("decode_timeout") from None
        if proc.returncode != 0:
            log.warning(
                "decode failed for %s @%.3fs: %s",
                seg,
                offset_s,
                err.decode(errors="replace").strip()[:300],
            )
            raise CaptureError("decode_failed")
        want = out_w * out_h * 3 // 2
        # Short read = the seek landed past the end of a segment that is
        # shorter than its ended_at implies (recorder killed mid-write).
        if len(out) < want:
            raise CaptureError("no_frame_at_timestamp")
        pixels = np.frombuffer(out[:want], dtype=np.uint8).reshape(out_h * 3 // 2, out_w)
        return RingFrame(
            sequence=0,
            pts_ns=int(at.timestamp() * 1_000_000_000),
            pixels=pixels.copy(),  # RingFrame's contract: pixels are owned
            pixel_format=PIXEL_FORMAT_NV12,
            width=out_w,
            height=out_h,
        )

    async def _embed_region(
        self, frame: RingFrame, polygon: list[list[float]]
    ) -> _Frame | None:
        got = _region_rgb_crop(frame, polygon)
        if got is None:
            return None
        crop, contrast = got
        with self._stats.timer("embed_ms"):
            vecs = await asyncio.to_thread(self._backend.embed, [crop])
        if vecs.shape[0] != 1:
            return None
        return _Frame(crop=crop, vector=vecs[0], contrast=contrast)

    # --- DB writes ----------------------------------------------------------

    async def _status_eval_tick(
        self, region_id: UUID, raw_label: str, distance: float
    ) -> None:
        """Record this read, and whether the held state is still vouched for.

        Only the held state read again, or a committed transition, ends an
        unsure run. A first read of a NEW label is still pending hysteresis;
        clearing on it would publish the held state for a sample or two
        between `unknown` and the new one — an `open` flash on a gate that is
        closing, which DIDA would act on."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scene_region_status
                    (region_id, last_eval_at, last_label_raw, last_distance,
                     unsure_since)
                VALUES ($1, now(), $2, $3,
                        CASE WHEN $2 = 'unknown' THEN now() END)
                ON CONFLICT (region_id) DO UPDATE SET
                    last_eval_at  = now(),
                    last_label_raw = EXCLUDED.last_label_raw,
                    last_distance  = EXCLUDED.last_distance,
                    unsure_since = CASE
                        WHEN EXCLUDED.last_label_raw = 'unknown'
                            THEN COALESCE(scene_region_status.unsure_since, now())
                        WHEN EXCLUDED.last_label_raw = scene_region_status.current_state
                            THEN NULL
                        ELSE scene_region_status.unsure_since END,
                    unsure = CASE
                        WHEN EXCLUDED.last_label_raw = 'unknown'
                            THEN now() - COALESCE(scene_region_status.unsure_since, now())
                                 >= make_interval(secs => $4)
                        WHEN EXCLUDED.last_label_raw = scene_region_status.current_state
                            THEN false
                        ELSE scene_region_status.unsure END
                """,
                region_id,
                raw_label,
                round(distance, 4),
                _UNSURE_AFTER_S,
            )

    async def _status_mark_stale(self, region_id: UUID) -> None:
        """Flag a region's status stale WITHOUT bumping last_eval_at.

        The UI then reads a growing last_eval_at age together with
        last_label_raw='stale' as "camera not producing frames", while
        current_state holds its last confident value and no transition fires.
        On a region that was never evaluated, seed last_eval_at once so the row
        exists; on subsequent stale ticks leave it frozen so the age keeps
        climbing."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scene_region_status
                    (region_id, last_eval_at, last_label_raw, last_distance,
                     unsure_since)
                VALUES ($1, now(), 'stale', NULL, now())
                ON CONFLICT (region_id) DO UPDATE SET
                    last_label_raw = 'stale',
                    unsure_since = COALESCE(scene_region_status.unsure_since, now()),
                    unsure = now() - COALESCE(scene_region_status.unsure_since, now())
                        >= make_interval(secs => $2)
                """,
                region_id,
                _UNSURE_AFTER_S,
            )

    async def _commit_transition(
        self,
        region: RegionConfig,
        from_state: str | None,
        to_state: str,
        distance: float,
    ) -> None:
        assert self._pool is not None
        payload = json.dumps(
            {
                "region_id": str(region.id),
                "region_name": region.name,
                "from_state": from_state,
                "to_state": to_state,
                "distance": round(distance, 4),
            }
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO scene_region_status
                        (region_id, current_state, current_state_since)
                    VALUES ($1, $2, now())
                    ON CONFLICT (region_id) DO UPDATE SET
                        current_state = EXCLUDED.current_state,
                        current_state_since = now(),
                        unsure_since = NULL,
                        unsure = false
                    """,
                region.id,
                to_state,
            )
            await conn.execute(
                """
                    INSERT INTO events (camera_id, kind, payload)
                    VALUES ($1, 'scene_state_change', $2::jsonb)
                    """,
                region.camera_id,
                payload,
            )
            # The episode opens unnamed — the plate read that names it lands
            # minutes later and `reconcile_places` joins the two by time.
            # Inside the same transaction as the status write, so the registry
            # can never disagree with the state that justified it.
            if region.place:
                if to_state == "present":
                    await claim_place(conn, region.place)
                else:
                    # Every transition away from `present` asks, including
                    # `unknown -> empty`: a view that went blind before it
                    # went empty still has to be able to close the place once
                    # it can see again. Whether it actually closes is
                    # `release_place_if_free`'s call, not this one's.
                    await release_place_if_free(conn, region.place)
        self._stats.incr("transitions")
        log.info(
            "region %s (%s): %s -> %s (d=%.3f)",
            region.name,
            region.id,
            from_state,
            to_state,
            distance,
        )

    # --- control: capture + eval (NATS request/reply) -----------------------

    async def _on_capture_request(self, msg) -> None:
        reply = {"error": None, "prototype_id": None, "crop_path": None}
        try:
            req = json.loads(msg.data)
            region_id = UUID(req["region_id"])
            state_label = str(req["state_label"])
            created_by = req.get("created_by")
            at = _parse_at(req.get("at"))
            try:
                async with self._lock:
                    region = self._regions.get(region_id)
                if region is None:
                    raise CaptureError("unknown_region")

                # Decoding a recorded frame shells out to ffprobe + ffmpeg and
                # takes seconds. Do it BEFORE taking the lock: the lock gates
                # the eval loop, and an operator enrolling a reference must
                # never stall live scene-state evaluation. The live path reads
                # the ring under the lock as before (sub-ms).
                frame = await self._frame_from_recording(region, at) if at else None

                async with self._lock:
                    # Re-check: the region can be deleted while we decode.
                    region = self._regions.get(region_id)
                    if region is None:
                        raise CaptureError("unknown_region")
                    if frame is None:
                        frame = self._reader_for(region.camera_slug).get_latest()
                        if frame is None:
                            raise CaptureError("no_frame")
                    cap = await self._embed_region(frame, region.polygon)
                    if cap is None:
                        raise CaptureError("bad_region")
                    if not teachable(state_label, cap.contrast):
                        log.warning(
                            "refusing to teach %r from a blind crop: contrast "
                            "%.1f is under %.1f — the camera is showing nothing "
                            "there, so this would teach the class to mean that",
                            state_label, cap.contrast, _BLIND_CONTRAST,
                        )
                        raise CaptureError("blinded")
                    proto_id = uuid4()
                    crop_rel = MediaLayout.rel(SCENE_CROPS, f"{proto_id}.jpg")
                    self._write_crop(crop_rel, cap.crop)
                    await self._insert_prototype(
                        proto_id, region_id, state_label, cap.vector,
                        crop_rel, created_by, at,
                    )
                    self._protos.setdefault(region_id, []).append(
                        (state_label, cap.vector)
                    )
                    self._stats.incr("captures")
                    reply["prototype_id"] = str(proto_id)
                    reply["crop_path"] = crop_rel
            except CaptureError as e:
                reply["error"] = e.reason
        except Exception as e:
            log.exception("capture request failed")
            reply["error"] = f"internal: {e}"
        if msg.reply:
            await msg.respond(json.dumps(reply).encode())

    async def _on_eval_request(self, msg) -> None:
        reply: dict = {"error": None, "state": None, "raw_label": None,
                       "distance": None, "per_state": {}}
        try:
            req = json.loads(msg.data)
            region_id = UUID(req["region_id"])
            async with self._lock:
                region = self._regions.get(region_id)
                rt = self._runtime.get(region_id)
                protos = self._protos.get(region_id) or []
                if region is None or rt is None:
                    reply["error"] = "unknown_region"
                elif len({lbl for lbl, _ in protos}) < 2:
                    reply["error"] = "need_two_states"
                else:
                    frame = self._reader_for(region.camera_slug).get_latest()
                    if frame is None:
                        reply["error"] = "no_frame"
                    else:
                        cap = await self._embed_region(frame, region.polygon)
                        if cap is None:
                            reply["error"] = "bad_region"
                        else:
                            raw, best_dist, per_state = read_region(
                                cap.contrast, cap.vector, protos,
                                region.states, region.unknown_margin,
                            )
                            reply["state"] = rt.current_state
                            reply["raw_label"] = raw
                            reply["distance"] = round(best_dist, 4)
                            reply["per_state"] = {
                                k: round(v, 4) for k, v in per_state.items()
                            }
        except Exception as e:
            log.exception("eval request failed")
            reply["error"] = f"internal: {e}"
        if msg.reply:
            await msg.respond(json.dumps(reply).encode())

    def _write_crop(self, rel: str, crop_rgb: np.ndarray) -> None:
        abs_path = self._media_root / rel
        try:
            cv2.imwrite(
                str(abs_path),
                cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, 90],
            )
        except Exception:
            log.exception("failed to write scene crop %s", abs_path)

    async def _insert_prototype(
        self,
        proto_id: UUID,
        region_id: UUID,
        state_label: str,
        vec: np.ndarray,
        crop_rel: str,
        created_by: str | None,
        at: datetime | None,
    ) -> None:
        assert self._pool is not None
        created_by_uuid = UUID(created_by) if created_by else None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scene_region_prototypes
                    (id, region_id, state_label, embedding, embedding_model,
                     crop_path, created_by, captured_at)
                VALUES ($1, $2, $3, $4::vector, $5, $6, $7,
                        COALESCE($8::timestamptz, now()))
                """,
                proto_id,
                region_id,
                state_label,
                vector_literal(vec),
                self._model_key,
                crop_rel,
                created_by_uuid,
                # The moment the reference shows, not when it was asked for.
                at,
            )

    async def stop(self) -> None:
        await self._stats.stop()
        for r in self._readers.values():
            r.detach()
        self._readers.clear()
        if self._listener is not None:
            await self._listener.stop()
        if self._nc is not None:
            try:
                await drain_quietly(self._nc)
            except Exception:
                log.exception("nats drain raised during shutdown — ignored")
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                log.exception("asyncpg pool close raised during shutdown — ignored")


async def _run() -> None:
    dsn = dsn_from_env()
    nats_url = os.environ.get("BABA_NATS_URL", "nats://nats:4222")
    model_env = os.environ.get(
        "BABA_STATE_EVALUATOR_MODEL", "/models/dinov2-vits14.onnx"
    ).strip()
    model_path = Path(model_env)
    model_key = model_path.stem  # e.g. 'dinov2-vits14' — provenance tag
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    # Instantiate the DINOv2 backend directly (NOT make_backend, which would
    # silently fall back to random-vector StubBackend on a missing model — that
    # would produce garbage classifications). A missing model file raises here
    # and the container crash-loops loudly, which is the correct fail-loud
    # behaviour: scene-state evaluation cannot work without the model.
    backend = DINOv2OnnxBackend(model_path)
    media_root = Path(os.environ.get("BABA_MEDIA_PATH", "/media"))

    svc = StateEvaluator(dsn, nats_url, backend, media_root, model_key)
    await svc.start()

    # Plate reading rides along here because this is the only service that
    # decodes native recorded frames, and native resolution is the whole point:
    # the plate that reads at 96-183 px in the segment reaches the SHM ring at
    # 45-86 px, the band where one plate produced two dozen different strings.
    # Opt-in, because the weights are BYOM (see plate_stack): a deployment that
    # has not named both models reads no plates rather than refusing to start.
    plate_stack = make_plate_stack(
        os.environ.get("BABA_PLATE_DETECTOR", "").strip(),
        os.environ.get("BABA_PLATE_OCR", "").strip(),
        models_dir=Path(os.environ.get("BABA_MODELS_PATH", "/models")),
    )
    plate_reader = (
        PlateReader(svc.pool, media_root, plate_stack) if plate_stack else None
    )
    svc._stats.set_label("plates", "on" if plate_stack else "off")

    # And the other half of reading a plate at night: while a headlight is
    # drowning the zone there is nothing in the footage to read, so the camera
    # is swapped into its plate profile for the seconds that lasts. Runs here
    # for the same reason the reader does — this service already knows which
    # zone the plate is read in, and it is watching the ring anyway.
    headlight = HeadlightWatcher(svc.pool, svc.nc, svc._reader_for)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Eval-liveness watchdog. The health marker only proves this asyncio process
    # is still scheduling tasks — it keeps ticking even when the eval loop wedges
    # (an SHM frame-ring reader that kept a dead attachment after the writer
    # recreated the segment → every region reads a frozen frame, marks 'stale',
    # and scene state silently freezes at its last value). BABA has NO autoheal,
    # so a merely-unhealthy container is never restarted; the only lever that
    # heals is process exit + `restart: unless-stopped`. A fresh eval of ANY
    # region proves the read→embed→classify path is alive, so a single dead camera
    # (its regions go stale, others keep progressing) does NOT trip this — only a
    # pipeline-wide stall does. Exit LOUD then, so we're recreated with a fresh
    # reader (exactly what a manual `compose up state-evaluator` did by hand).
    STALL_LIMIT_S = 120.0
    async def _watchdog() -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=10)
            if stop.is_set():
                break
            regions, protos = svc._regions, svc._protos  # atomic refs (reconcile replaces whole dict)
            n_eval = sum(
                1 for rid, r in regions.items()
                if r.enabled and len({lbl for lbl, _ in protos.get(rid) or []}) >= 2
            )
            stalled_for = time.monotonic() - svc._last_progress
            if n_eval > 0 and stalled_for > STALL_LIMIT_S:
                log.critical(
                    "eval loop stalled: no fresh evaluation for %.0fs while %d region(s) "
                    "evaluable (frame ring wedged?) — exiting for restart",
                    stalled_for, n_eval,
                )
                os._exit(1)

    async def _reconcile_places() -> None:
        # Its own task besides the call the plate reader makes after each
        # successful read: a fill that lands AFTER its read still has to find
        # it, and a restart must pick up whatever was left unjoined.
        while not stop.is_set():
            try:
                async with svc._pool.acquire() as conn:
                    # Order matters: a place occupied with no episode gets one
                    # first, so the read-join in the same pass can name it.
                    await open_missing_episodes(conn)
                    await reconcile_places(conn)
                    # Last: only reads the arrival join has already declined
                    # are left, so a departure can never outbid a fill.
                    await name_departures(conn)
                    # Then say it out loud. After the naming passes, so a stay
                    # named at either end is announced with its name.
                    await announce_episodes(conn)
            except Exception:
                log.exception("place reconcile sweep failed — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=30.0)

    tick_task = spawn(
        HealthMarker("baba", "state-evaluator").run_loop(), name="state-evaluator-health", log=log
    )
    bind_task = spawn(_reconcile_places(), name="state-evaluator-reconcile")
    watchdog_task = spawn(_watchdog(), name="state-evaluator-watchdog")
    eval_task = spawn(svc.run_eval_loop(stop), name="state-evaluator-eval")
    tasks = [tick_task, bind_task, watchdog_task, eval_task]
    if plate_reader is not None:
        tasks.append(spawn(plate_reader.run(stop), name="state-evaluator-plates"))
    tasks.append(spawn(headlight.run(stop), name="state-evaluator-headlight"))

    await stop.wait()
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await svc.stop()


def main() -> None:
    setup_logging("state-evaluator")
    run_service(_run())


if __name__ == "__main__":
    main()
