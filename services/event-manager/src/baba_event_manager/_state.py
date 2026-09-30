"""Per-track in-memory state + camera slug->id resolver."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TrackState:
    first_seen_ns: int
    # LIVENESS: last tick this track appeared on the wire at all. Drives the
    # finalize timeout, so it must keep advancing while the tracker is still
    # emitting — including the coasting frames before its staleness cutoff.
    # Freezing this would finalize a track the tracker is still publishing and
    # split one visit into two records.
    last_seen_ns: int
    # EVIDENCE: last tick the subject was actually SEEN — a real detection
    # match, or an appearance-verified anchor hold. This is what a visit ENDS
    # at: Kalman-coasted frames are emission, not presence, and counting them
    # stretched a visit ~10 s past the exit so its clip kept rolling on an
    # empty scene. 0 until the first evidence-bearing wire arrives.
    last_evidence_ns: int
    # `class_id`/`class_name` start as the first-seen class but get rewritten
    # to the *majority* class at finalize time. Until then they're indicative,
    # not authoritative.
    class_id: int
    class_name: str
    max_confidence: float
    n_observations: int
    last_bbox: tuple[float, float, float, float]
    # Min/max bbox centre over track lifetime — used to detect static objects.
    min_cx: float
    max_cx: float
    min_cy: float
    max_cy: float
    # Best box-baked thumbnail captured live during the track: the JPEG of the
    # highest-confidence ACTIVE detection's own SHM frame with its box drawn
    # on. Frame and box are one array from one instant, so the box sits on the
    # subject by construction (no recording seek / clock match / browser
    # letterbox maths). `best_thumb_conf` is the confidence that produced it,
    # so a later, clearer detection replaces it. Finalize writes the JPEG as
    # the track thumbnail; a null one falls back to a recording/live snapshot.
    best_thumb_jpeg: bytes | None = None
    best_thumb_conf: float = 0.0
    # Tracker generation that owns this record (TracksMessage.epoch). When a
    # message arrives with a higher epoch, this record belongs to a dead
    # tracker generation whose track_id has since been reused for a different
    # subject; it is finalized and cleared before the reused id is processed.
    epoch: int = 0
    # Per-class-id occurrence counts. The mode wins at finalize, fixing
    # mid-track flip-flops (typical: car ↔ truck on similar-looking vehicles).
    class_counts: dict[int, int] = field(default_factory=dict)
    # First-seen names for each class_id — so we can resolve the chosen
    # class_id back to a class_name at finalize.
    class_names: dict[int, str] = field(default_factory=dict)
    # Zones the track is currently inside. Maps zone_id (UUID) → the
    # ns timestamp at which the track first entered. The dwell evaluator
    # checks (now - entry_ns) against the configured threshold; once
    # the dwell event has fired for a (track, zone) pair we record it
    # in `dwell_emitted` so we don't re-fire on every subsequent
    # observation. Both reset to empty when the zone is left or the
    # track is finalized.
    inside_zones: dict = field(default_factory=dict)
    dwell_emitted: set = field(default_factory=set)
    # True iff this track was inside at least one configured zone at any
    # point during its lifetime. Used by `_finalize` to suppress
    # `track_finalized` events for tracks that never touched a zone —
    # typical case is road traffic visible at the edge of a property
    # camera that has zones drawn only around the property itself. The
    # operator's intent is "tell me about MY property, not the street"
    # so trackless-of-zone events are noise.
    touched_zone: bool = False
    # Latch — set True the first time ANY zone_enter is emitted for
    # this track and never cleared. `enter_emitted` is per-visit (a
    # zone_exit clears it so a re-entry fires a fresh enter) which
    # makes it the wrong signal for finalize-time decisions. The
    # finalize gate wants "did this track ever produce a real enter
    # event?" — that's this latch.
    ever_enter_fired: bool = False
    # Last motion_state we observed for this track. We diff against
    # `motion_state` on each tick to emit object_parked / object_unparked
    # events only on transitions, not every observation.
    last_motion_state: str = "active"
    # Latch — True once the track has ever been observed `active`. A track
    # born parked (tracker birth-suppression: detector flicker respawning
    # a static-clutter phantom) never moved on camera, so it "entered"
    # nothing — zone evaluation is skipped until it first goes active,
    # which also stops parked bbox jitter from flapping enter/exit on a
    # zone boundary. Real subjects are observed active on arrival, so
    # this never delays their zone events.
    ever_active: bool = False
    # Zone IDs for which `zone_enter` has already fired this visit.
    # Used to gate `zone_enter` behind the per-zone `min_dwell_ms`
    # filter — entry is suppressed until the track has been inside the
    # zone continuously for that long, which kills the motorcycle-/
    # pedestrian-flash spam common on through-traffic cameras. The set
    # also gates `zone_exit`: if `zone_enter` was never emitted we
    # silently swallow the exit too, so transient passes produce no
    # events at all. Resets when the track is finalized.
    enter_emitted: set = field(default_factory=set)
    # A parked VEHICLE ends its visit where it stops, instead of holding the
    # track open for as long as it stands there. Two flags, because the finalize
    # cannot happen inside the message handler that notices the transition:
    # `park_finalize_due` asks the sweep loop to write the row, and
    # `parked_finalized` records that it did.
    #
    # The record deliberately STAYS in the state map afterwards. Dropping it
    # would make the tracker's very next message look like a brand-new subject
    # and mint a fresh visit every second the car stands there. It is revived
    # into a genuinely new track only when the vehicle moves again — which is
    # also what makes "parked for how long" a question about the place rather
    # than about the length of a track.
    #
    # Vehicles only. A person who sits down must keep their track: presence on
    # the patio is exactly what a seated subject means, and ending it there
    # would drop them out of the live roster and DIDA.
    park_finalize_due: bool = False
    parked_finalized: bool = False
    # Whether this spell of stillness has already been reported. The parked
    # transition can be deferred past `stationary` when the box touches a frame
    # edge, so "did we say it yet" is what decides, not which state we came
    # from. Cleared when the object moves again.
    parked_emitted: bool = False
    # Whether the object ever came to rest during this visit. Persisted so the
    # presence sweep can tell a car that parked (an arrival) from one that
    # drove out of frame (a departure).
    came_to_rest: bool = False
    # Pre-allocated `tracks.id` UUID assigned the first time we see this
    # local_track_id. Zone events stamp it into `events.track_id` so the
    # Sightings page can stitch zone visits to the eventual finalized
    # track without a separate backfill pass. The finalize path INSERTs
    # the `tracks` row using this same UUID rather than minting a fresh
    # one, keeping zone events and the track row referentially aligned.
    db_track_id: UUID | None = None

    def absorb(self, young: TrackState) -> None:
        """Fold a younger record of the same subject into this visit.

        The young record is seconds old and its counters are a strict subset
        of the visit, so the fold is additive; this record's first_seen_ns
        (the real entrance) and zone state win.
        """
        self.last_seen_ns = max(self.last_seen_ns, young.last_seen_ns)
        self.last_evidence_ns = max(self.last_evidence_ns, young.last_evidence_ns)
        self.n_observations += young.n_observations
        self.max_confidence = max(self.max_confidence, young.max_confidence)
        self.last_bbox = young.last_bbox
        self.min_cx = min(self.min_cx, young.min_cx)
        self.max_cx = max(self.max_cx, young.max_cx)
        self.min_cy = min(self.min_cy, young.min_cy)
        self.max_cy = max(self.max_cy, young.max_cy)
        self.ever_active = self.ever_active or young.ever_active


# --- camera resolution: slug → uuid ---


@dataclass(slots=True, frozen=True)
class CameraInfo:
    id: UUID
    # Carried so the state snapshot can publish `camera_name` without a second
    # lookup. UUID stays the identity — a rename changes only this.
    name: str = ""


class CameraResolver:
    """Maps slug → CameraInfo. Refreshes on NOTIFY cameras_changed."""

    def __init__(self) -> None:
        self._map: dict[str, CameraInfo] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, conn: asyncpg.Connection) -> None:
        rows = await conn.fetch("SELECT id, slug, name FROM cameras")
        async with self._lock:
            self._map = {
                r["slug"]: CameraInfo(id=r["id"], name=r["name"] or r["slug"]) for r in rows
            }
        log.info("camera map refreshed: %d cameras", len(self._map))

    async def get(self, slug: str) -> CameraInfo | None:
        async with self._lock:
            return self._map.get(slug)

    async def slug_to_id_map(self) -> dict[str, UUID]:
        """Snapshot for callers (heatmap flush, etc.) that need to map
        all known slugs to camera UUIDs in one go. Returns a copy so
        the caller can iterate freely without holding the lock."""
        async with self._lock:
            return {slug: info.id for slug, info in self._map.items()}
