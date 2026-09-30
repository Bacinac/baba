"""Per-camera tracker with inline OSNet ReID.

Pipeline (per detection batch from NATS `baba.detections.<camera_id>`):

  1. Decode detections + look up the matching SHM ring frame by sequence
     (the ingestor writes the NV12 frame buffer into a per-camera POSIX
     shared-memory ring; the tracker attaches the same ring through the
     shared `baba-shm` volume, so the `FrameRingReader` lookup is zero-copy).
  2. For each detection passing the class filter, slice the bbox out of
     the NV12 buffer, convert that crop only to RGB (cheap — typical
     person crop is ~100×300 px), and stack into an OSNet input batch.
  3. Run OSNet x0_25 → 512-dim L2-normalised appearance feature per
     detection.
  4. Hand the detections + features to a per-camera `norfair.Tracker`
     instance. Norfair's `reid_distance_function` (cosine over the
     OSNet features) recovers lost tracks across brief occlusions
     (person ducks behind a tree, car passes a pillar) without ID
     swap.
  5. Translate Norfair's `TrackedObject` list into our existing
     `TrackWire` shape so embedder + event-manager don't need to
     change anything downstream.
  6. Maintain the existing `MotionTracker` state machine independently
     of the tracker backend — that's about centroid stillness, not
     about which library does association.

If `BABA_TRACKER_REID_MODEL` is empty or the file is missing, the
tracker falls back to Norfair's pure-IoU association (no
`reid_distance_function`, no OSNet load). That keeps the service
runnable on a stripped-down deployment that hasn't downloaded the
ReID weights yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import msgspec
import norfair
import numpy as np
from baba_core import StatsCollector, drain_quietly, mask_credentials, setup_logging
from baba_core.embed import OSNetOnnxBackend
from baba_core.frame_ring import PIXEL_FORMAT_NV12, FrameRingReader, RingFrame
from baba_core.nats_conn import connect as nats_connect
from baba_core.phantom_match import DEFAULT_MIN_BIRTHS as PHANTOM_DEFAULT_MIN_BIRTHS
from baba_core.phantom_match import DEFAULT_MIN_SPAN_S as PHANTOM_DEFAULT_MIN_SPAN_S
from baba_core.phantom_match import REAL_SUBJECT_CONF as PHANTOM_DEFAULT_REAL_CONF
from baba_core.pipeline_settings import Settings
from baba_core.runtime import run_service
from baba_core.tunables import TRACKING_TUNABLES
from baba_core.wire import (
    SUBJECT_ACTIVITY_TEMPLATE,
    SUBJECT_TELEMETRY_TEMPLATE,
    SUBJECT_TRACKS_TEMPLATE,
    ActivityMessage,
    DetectionsMessage,
    DetectionWire,
    TrackerTelemetry,
    TracksMessage,
    TrackWire,
)
from home_core.health import HealthMarker
from home_core.tasks import spawn
from norfair import Detection, Tracker
from norfair.distances import VectorizedDistance

from baba_tracker.anchor_hold import AnchorHold
from baba_tracker.camera_settings import CameraSettings
from baba_tracker.config import TrackerConfig
from baba_tracker.identity_stamp import IdentityStamp
from baba_tracker.phantom_spots import PhantomSpots
from baba_tracker.zone_masks import ZoneMasks

log = logging.getLogger("baba.tracker")

# Base for the tracker generation stamped on every TracksMessage. Captured once
# at import so it differs across process restarts; per-camera Norfair
# recreations add to it (see PerCameraTrackers.epoch).
_PROC_EPOCH = time.time_ns()

# Wire formats (frame-in detections, tracks-out) live in baba_core.wire —
# one canonical schema shared by every producer/consumer. `_DetData` below
# is tracker-internal (norfair opaque payload), not on the bus.

SUBJECT_DETECTIONS_IN = "baba.detections.*"


# Motion states — same string codes as before so embedder + event-manager
# branches keep working without redeploy.
MOTION_ACTIVE = "active"
MOTION_STATIONARY = "stationary"
MOTION_PARKED = "parked"


_PERSON_CLASS_ID = 0


class ClassStabilizer:
    """Confidence-weighted majority vote over a track's class history.

    Small detectors flip classes on odd viewpoints while the track itself
    survives (association is IoU+ReID, not class): a carport car reads as
    `motorcycle` one frame and `dog` the next, Lumi the dog reads as `cat`.
    The vote spans ALL classes — cross-group flips (car→dog) are exactly as
    much jitter as in-group ones — so downstream consumers (events, DIDA
    object_class, per-class rules, UI) see the majority label, AND the object
    never disappears the way a raised per-class threshold would make it (the
    flipped detection is usually the ONLY one covering the object).

    The single exception is `person`: a person detection is reported
    IMMEDIATELY, majority or not — a "car" that turns out to be a person is
    an alert, not jitter to smooth, and seconds of voting latency there are
    seconds of missed presence. (Symmetrically, person votes are excluded
    from the majority pick, so a track that momentarily read as person
    doesn't get sticky-labelled person once the flip passes.)

    Votes are capped (renormalized at ~300 confidence-weight ≈ a few minutes
    at 2 fps) so a genuinely new long-term occupant of the same track id can
    win the vote instead of fighting hours of accumulated history."""

    _GC_GRACE_MS = 60_000
    _VOTE_CAP = 300.0

    def __init__(self) -> None:
        self._tracks: dict[int, dict[str, Any]] = {}

    def stable(
        self, track_id: int, class_id: int, class_name: str, confidence: float, ts_ns: int
    ) -> tuple[int, str]:
        st = self._tracks.get(track_id)
        if st is None:
            st = {"votes": {}, "names": {}, "last_ns": ts_ns}
            self._tracks[track_id] = st
        st["last_ns"] = ts_ns
        st["votes"][class_id] = st["votes"].get(class_id, 0.0) + confidence
        st["names"][class_id] = class_name
        total = sum(st["votes"].values())
        if total > self._VOTE_CAP:
            scale = self._VOTE_CAP / total
            for k in st["votes"]:
                st["votes"][k] *= scale
        if class_id == _PERSON_CLASS_ID:
            return class_id, class_name
        candidates = {k: v for k, v in st["votes"].items() if k != _PERSON_CLASS_ID}
        best = max(candidates, key=candidates.__getitem__)
        return best, st["names"][best]

    def gc(self, ts_ns: int) -> None:
        cutoff = ts_ns - self._GC_GRACE_MS * 1_000_000
        stale = [tid for tid, st in self._tracks.items() if st["last_ns"] < cutoff]
        for tid in stale:
            del self._tracks[tid]


class BoxHold:
    """Latches a stationary/parked track's emitted bbox + confidence to the
    strongest detection seen at its resting spot, instead of following the
    per-frame detection down as a nano detector fragments a static object.

    Motive (west's cut-off carport car): the car arrives in motion, gets a
    clean whole-car box at high confidence, births a track — then parks, and on
    a 3.55:1 letterbox the per-frame detection collapses to a low-conf corner
    fragment every other frame. The box shrinks and the score flickers even
    though nothing physically changed. A normally-framed camera (the red car)
    never sees this because its parked detection stays a full box — same
    tracker, the difference is purely the detector's per-frame quality.

    The track already KNOWS the object is parked (MotionTracker) and a parked
    object doesn't change shape, so the emitted box/score should hold at the
    best value observed AT that rest position, not chase the fragment:

    - `active` (moving): follow the live detection (the box must track real
      motion) and hold nothing — dropped so the next rest re-captures at the
      NEW spot, never a stale one.
    - `stationary`/`parked`: keep a high-water (box, conf) — the best-confidence
      detection at this spot. A parked object stays put, so every detection is
      at the same place and "best confidence" is unambiguously the fullest,
      cleanest box; the intermittent good frame (west alternates 78%↔36%) sets
      the hold and the fragments can't pull it back down.

    General, not west-specific: every parked car and standing person on every
    camera gets a stable box/score. Touches nothing in the detector — the
    architectural answer to detection jitter instead of threshold tuning."""

    _GC_GRACE_MS = 60_000

    def __init__(self) -> None:
        self._tracks: dict[int, dict[str, Any]] = {}

    def resolve(
        self,
        track_id: int,
        bbox: tuple[float, float, float, float],
        confidence: float,
        motion_state: str,
        ts_ns: int,
    ) -> tuple[tuple[float, float, float, float], float]:
        """The (bbox, confidence) to EMIT: live while active, the held
        high-water while stationary/parked."""
        if motion_state == MOTION_ACTIVE:
            self._tracks.pop(track_id, None)
            return bbox, confidence
        st = self._tracks.get(track_id)
        if st is None or confidence > st["conf"]:
            st = {"bbox": bbox, "conf": confidence}
            self._tracks[track_id] = st
        st["last_ns"] = ts_ns
        return st["bbox"], st["conf"]

    def gc(self, ts_ns: int) -> None:
        cutoff = ts_ns - self._GC_GRACE_MS * 1_000_000
        stale = [tid for tid, st in self._tracks.items() if st["last_ns"] < cutoff]
        for tid in stale:
            del self._tracks[tid]


def _trim_history(st: dict[str, Any], cutoff: int) -> None:
    if len(st["history"]) > 2:
        i = 0
        while i + 1 < len(st["history"]) and st["history"][i][0] < cutoff:
            i += 1
        if i > 0:
            st["history"] = st["history"][i:]


def _update_move_streak(st: dict[str, Any], cx: float, cy: float, thr: float) -> None:
    """Demotion signal. The window max-extent is the wrong measure for
    leaving stillness: a detector that alternates between two box
    hypotheses on a static object (tall/short crop of the same clutter
    phantom) keeps the extent permanently above threshold and flaps
    parked↔active forever. Instead measure the CURRENT position against
    the window median (the dominant pose) and require the excursion to
    be sustained: a 1-2 frame hypothesis flip resets the streak, while
    genuine departure drifts monotonically and demotes ~1 s later —
    an acceptable delay on unpark alerts for flap-free stillness."""
    xs = [p[1] for p in st["history"]]
    ys = [p[2] for p in st["history"]]
    med_x = sorted(xs)[len(xs) // 2]
    med_y = sorted(ys)[len(ys) // 2]
    off_median = ((cx - med_x) ** 2 + (cy - med_y) ** 2) ** 0.5
    if off_median > thr * 2.0:
        st["move_streak"] = st.get("move_streak", 0) + 1
    else:
        st["move_streak"] = 0


def _window_displacement(history: list[tuple[int, float, float]]) -> float:
    """Centroid travel (max extent) over the rolling window, tolerating ONE
    outlier frame.

    An occlusion glitch — a pillar momentarily splitting a parked car so the
    detector boxes only the visible half — throws a single wild centroid into
    the window. With a plain max-extent measure that one frame inflates the
    displacement for the whole window: it blocks promotion to `stationary`
    for 8 s AND demotes an already-parked object, so a glitch every ~10 s
    keeps the state machine churning forever (live incident: shed carport car
    stuck cycling `STATIONARY 3s`). Dropping the single point farthest from
    the window median and taking the tighter extent ignores lone glitches;
    real movement spans consecutive frames and still registers fully."""
    if len(history) < 2:
        return 0.0
    xs = [p[1] for p in history]
    ys = [p[2] for p in history]

    def extent(ix: list[int]) -> float:
        exs = [xs[i] for i in ix]
        eys = [ys[i] for i in ix]
        dx = max(exs) - min(exs)
        dy = max(eys) - min(eys)
        return (dx * dx + dy * dy) ** 0.5

    all_ix = list(range(len(history)))
    full = extent(all_ix)
    if len(history) < 5:
        return full
    sx = sorted(xs)
    sy = sorted(ys)
    mx = sx[len(sx) // 2]
    my = sy[len(sy) // 2]
    worst = max(all_ix, key=lambda i: (xs[i] - mx) ** 2 + (ys[i] - my) ** 2)
    trimmed = extent([i for i in all_ix if i != worst])
    return min(full, trimmed)


def _bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0.0 or ih <= 0.0:
        return 0.0
    inter = iw * ih
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    denom = area_a + area_b - inter
    return inter / denom if denom > 0.0 else 0.0


class MotionTracker:
    """Per-camera motion state for every track_id we've seen.

    Same state machine as the previous ByteTrack-backed implementation:
    centroid history kept inside a `static_window_ms` rolling window,
    promote to `stationary` when max displacement stays under an
    OBJECT-RELATIVE radius, promote to `parked` after `park_threshold_ms`
    of continued stillness, demote back to `active` on a 2×radius
    movement (hysteresis keeps a parked car parked across bbox jitter).

    The stillness radius is `static_move_ratio × bbox_diagonal` (floored
    at `static_move_min_px`), recomputed per update from the object's
    current size. A fixed pixel threshold couldn't fit both a 400px car
    and a 60px distant one, and moved with each camera's resolution;
    scaling to the object makes it resolution-independent.

    Birth-suppression (parked ghosts): detector confidence on a static
    object flickers around the threshold, so Norfair kills and respawns
    the track every few seconds — and each respawn used to be born
    `active`, re-announcing the same motionless object forever (patio
    2026-07-13: table clutter read as "person" produced an endless
    object_parked/zone-event stream on an empty terrace). We remember
    the bbox of every stationary/parked track for `_GHOST_TTL_MS` past
    its last still sighting; a newborn track whose first bbox
    IoU-matches a same-class ghost is born `parked` (inheriting the
    ghost's stillness timer) instead of starting `active`. A genuinely new object cannot
    materialise at IoU≥0.5 inside a parked ghost without approaching
    on camera first — and even a wrongly inherited track promotes back
    to `active` on its first real movement, so recall on live subjects
    is untouched.
    """

    _GC_GRACE_MS = 60_000
    # Measured 2026-07-28: under night IR the detector loses a parked car for
    # minutes to an hour at a stretch (longest observed gap 52 min), so a five
    # minute ghost expired long before each rebirth and the standing car was
    # re-announced as a fresh arrival all night. Two hours covers the longest
    # measured dropout with headroom; the standing object refreshes its ghost
    # on every still sighting, so this is per-gap, not per-night.
    _GHOST_TTL_MS = 7_200_000
    # How long a spot's "something arrived here under its own power" survives
    # the subject that earned it.
    #
    # The flag used to be a bare bool on the ghost, inherited by whatever was
    # born on that spot next — and a track that inherited it wrote it straight
    # back into the ghost it left behind. So a corner that produces a static
    # false positive kept renewing a stranger's provenance for ever: measured
    # on patio, three tracks that travelled 2.3, 0.7 and 2.0 px against box
    # diagonals of 114-202 px, all carrying `ever_active`, on a spot with
    # 10,282 births and 3,587 banked movers. Two consequences, both live: the
    # phantom passed the gate that reports a person to DIDA and turned the
    # radio on over an empty terrace, and a spot with a banked mover is one
    # the phantom registry may never suppress.
    #
    # A track may still INHERIT the flag — a seated person's flicker respawn
    # never travels itself — but only a track that EARNED it by travelling
    # refreshes the clock. Beyond this the provenance lapses and the spot is
    # what it looks like: furniture. A person sitting longer than this is held
    # by the appearance anchor, which is the mechanism for that and does not
    # depend on this flag.
    _MOVER_PROVENANCE_MS = 7_200_000
    _GHOST_IOU = 0.5
    # Consecutive over-threshold excursions (current pos vs window median)
    # required to leave stationary/parked — see the demotion comment below.
    _DEMOTE_STREAK = 3
    # Lifetime travel (in units of the object's own bbox diagonal) beyond
    # which a track counts as a "real mover" — a subject that ARRIVED on
    # camera. Static-clutter phantoms jitter ~0.3-0.4 diag (tall/short
    # hypothesis flip); a person walking to a seat covers many diagonals.
    # The flag is sticky and inherited through the parked-ghost chain, so
    # a seated person's flicker respawns keep their presence provenance.
    _REAL_MOVER_RATIO = 1.0
    # A SECOND, lower travel latch, used only to answer the anchor's question
    # "did this subject arrive under its own power, or has it always been part
    # of the furniture?".
    #
    # `_REAL_MOVER_RATIO` cannot answer it. That one guards the phantom-spot
    # registry, where a single false mover permanently disqualifies a spot from
    # ever suppressing (568 spots are already dead that way), so it is
    # deliberately strict: a full bbox diagonal of lifetime travel. But a person
    # who walks to a chair and sits gets a track that dies in 5-7 s — measured
    # on the patio — and never banks that much travel, so the anchor refused to
    # protect exactly the subject it was written for.
    #
    # One threshold cannot serve both: a false positive costs the registry a
    # spot forever, while a false negative costs Marko his presence. Furniture
    # does not need a full body-length to be told apart from a person — the
    # static phantoms on this camera repeat at IDENTICAL coordinates for
    # minutes (centroid extent of a couple of pixels), so a third of a diagonal
    # still leaves two orders of magnitude of margin. And an anchor armed in
    # error is bounded anyway: appearance, takeover and corroboration all
    # release it. A spot killed in error is not bounded by anything.
    _ARRIVAL_RATIO = 0.35

    def __init__(
        self,
        static_move_ratio: float,
        static_move_min_px: float,
        static_window_ms: int,
        park_threshold_ms: int,
    ) -> None:
        self._static_move_ratio = static_move_ratio
        self._static_move_min_px = static_move_min_px
        self._static_window_ms = static_window_ms
        self._park_threshold_ms = park_threshold_ms
        self._tracks: dict[int, dict[str, Any]] = {}
        # track_id → (bbox, class_name, state_since_ns, last_parked_ns,
        #             mover_earned_ns)
        self._ghosts: dict[int, tuple[tuple[float, float, float, float], str, int, int, bool]] = {}

    def _forget_ghosts_at(
        self,
        bbox: tuple[float, float, float, float],
        class_name: str,
        track_id: int,
    ) -> int:
        """Drop every ghost describing this spot, not just this track's.

        A ghost describes a SPOT, and the registry exists precisely because a
        flickering static object cycles through track ids: each new id matches
        the ghost, is born parked, and writes its own entry at the same box. N
        entries per physical spot is the steady state, not an edge case.

        Popping one by track id left the rest to hold the spot parked for the
        remaining two hours of their own TTL, so the demotion — "the object
        provably moved, newborns here must not inherit parked" — did not mean
        what it said.
        """
        gone = [
            tid for tid, g in self._ghosts.items()
            if tid == track_id
            or (g[1] == class_name and _bbox_iou(bbox, g[0]) >= self._GHOST_IOU)
        ]
        for tid in gone:
            del self._ghosts[tid]
        return len(gone)

    def _match_ghost(
        self,
        bbox: tuple[float, float, float, float],
        class_name: str,
        ts_ns: int,
    ) -> tuple[tuple[float, float, float, float], str, int, int, bool] | None:
        ttl_ns = self._GHOST_TTL_MS * 1_000_000
        for ghost in self._ghosts.values():
            if ghost[1] != class_name:
                continue
            if ts_ns - ghost[3] > ttl_ns:
                continue
            if _bbox_iou(bbox, ghost[0]) >= self._GHOST_IOU:
                return ghost
        return None

    def matches_static_ghost(
        self,
        bbox: tuple[float, float, float, float],
        class_name: str,
        ts_ns: int,
    ) -> bool:
        """True when `bbox` sits on a parked same-class spot whose chain never
        really moved — a static-clutter phantom. Used by the activity verdict:
        phantom flickers must not hold a camera at full frame rate, but a
        flash detection on a REAL parked person's spot (real_mover ghost)
        keeps the camera at target fps so a seated person stays observed."""
        ghost = self._match_ghost(bbox, class_name, ts_ns)
        return ghost is not None and not self._ghost_mover(ghost, ts_ns)

    def _ghost_mover(self, ghost: tuple | None, ts_ns: int) -> bool:
        """Whether this spot still carries "something arrived here under its
        own power". Inheritable, but it lapses: see _MOVER_PROVENANCE_MS."""
        if ghost is None or not ghost[4]:
            return False
        return ts_ns - ghost[4] < self._MOVER_PROVENANCE_MS * 1_000_000

    def peek(self, track_id: int) -> tuple[str, int, bool, bool]:
        """This track's motion state without touching it.

        For a tick where Norfair coasted: it returned the same detection with
        a predicted box, so there is nothing new to say about whether the
        subject moved, and saying it anyway is what promoted departing
        subjects to `stationary`. An unknown track reads as active, which is
        what a track with no stillness history has always meant.
        """
        st = self._tracks.get(track_id)
        if st is None:
            return MOTION_ACTIVE, 0, False, False
        return st["state"], st["state_since_ns"], st["real_mover"], st["arrived"]

    def update(
        self,
        track_id: int,
        bbox: tuple[float, float, float, float],
        class_name: str,
        ts_ns: int,
    ) -> tuple[str, int, bool, bool]:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        bbox_diag = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        st = self._tracks.get(track_id)
        if st is None:
            st = self._birth(track_id, bbox, class_name, ts_ns, cx, cy)
            return st["state"], st["state_since_ns"], st["real_mover"], st["arrived"]

        st["history"].append((ts_ns, cx, cy))
        st["last_seen_ns"] = ts_ns
        self._record_travel(st, cx, cy, bbox_diag, ts_ns)

        window_ns = self._static_window_ms * 1_000_000
        _trim_history(st, ts_ns - window_ns)
        displacement = _window_displacement(st["history"])

        # Object-relative stillness radius, floored so tiny boxes don't get a
        # sub-pixel threshold that no real detection could ever satisfy.
        thr = max(self._static_move_min_px, self._static_move_ratio * bbox_diag)
        _update_move_streak(st, cx, cy, thr)
        # "Enough history to judge stillness" = the track has existed for at
        # least the window. Deriving this from the trimmed history span instead
        # is off-by-a-frame — the trim drops the entry just outside the window,
        # so the span settles a frame short of window_ns and the promotion to
        # `stationary` almost never fires. Track age is unambiguous.
        has_full_window = (ts_ns - st["created_ns"]) >= window_ns
        # Demotion needs the same full window the promotion does: a track
        # seconds old has a median made of 2-3 points, so a hypothesis flip
        # reads as a sustained excursion and a born-parked phantom respawn
        # fired one object_unparked per flicker (observed live on .11).
        demote = has_full_window and st["move_streak"] >= self._DEMOTE_STREAK
        self._transition(
            st,
            track_id,
            bbox,
            class_name,
            ts_ns,
            still=has_full_window and displacement <= thr,
            demote=demote,
        )

        # Record BOTH stationary and parked spots: a flickering phantom
        # often dies before it survives park_threshold_ms, so waiting for
        # `parked` would never seed the ghost and every respawn would be
        # born active again (observed live: object_parked trickle at the
        # stationary promotion, ~8 s into each respawn). A ghost from a
        # real briefly-still subject is popped the moment they move (both
        # demotion branches above), so it only outlives its track when
        # the object genuinely stayed motionless through the flicker.
        if st["state"] in (MOTION_STATIONARY, MOTION_PARKED):
            self._ghosts[track_id] = (
                bbox, class_name, st["state_since_ns"], ts_ns,
                st["mover_earned_ns"],
            )

        return st["state"], st["state_since_ns"], st["real_mover"], st["arrived"]

    def _birth(
        self,
        track_id: int,
        bbox: tuple[float, float, float, float],
        class_name: str,
        ts_ns: int,
        cx: float,
        cy: float,
    ) -> dict[str, Any]:
        ghost = self._match_ghost(bbox, class_name, ts_ns)
        st = {
            "history": [(ts_ns, cx, cy)],
            "state": MOTION_PARKED if ghost is not None else MOTION_ACTIVE,
            # Inherit the ghost's stillness timer so the operator-visible
            # "PARKED 25m" survives the detector flicker that recycled
            # the track id.
            "state_since_ns": ghost[2] if ghost is not None else ts_ns,
            "last_seen_ns": ts_ns,
            "created_ns": ts_ns,
            # Lifetime centroid extent — feeds the real-mover latch.
            "min_cx": cx,
            "max_cx": cx,
            "min_cy": cy,
            "max_cy": cy,
            # Provenance is inherited through the ghost chain: a seated
            # person's flicker respawn never travels itself, but its
            # ancestor did.
            "real_mover": self._ghost_mover(ghost, ts_ns),
            # Same provenance inheritance, lower bar — see _ARRIVAL_RATIO.
            "arrived": self._ghost_mover(ghost, ts_ns),
            # When a track at this spot last EARNED the flag by travelling.
            # Inherited, never refreshed by a track that did not travel —
            # that renewal is what made a phantom immortal.
            "mover_earned_ns": (ghost[4] if ghost is not None else 0),
        }
        self._tracks[track_id] = st
        if st["state"] == MOTION_PARKED:
            self._ghosts[track_id] = (
                bbox, class_name, st["state_since_ns"], ts_ns,
                st["mover_earned_ns"],
            )
        return st

    def _record_travel(
        self, st: dict[str, Any], cx: float, cy: float, bbox_diag: float, ts_ns: int
    ) -> None:
        if cx < st["min_cx"]:
            st["min_cx"] = cx
        elif cx > st["max_cx"]:
            st["max_cx"] = cx
        if cy < st["min_cy"]:
            st["min_cy"] = cy
        elif cy > st["max_cy"]:
            st["max_cy"] = cy
        # Evaluated on every sample, not only while a flag is unset. A track
        # that INHERITED the flags would otherwise never look at its own
        # travel, so a subject that respawned and then genuinely walked could
        # not refresh the provenance clock — it would lapse on its ancestor's
        # timestamp while the subject was still moving.
        dx = st["max_cx"] - st["min_cx"]
        dy = st["max_cy"] - st["min_cy"]
        travel = (dx * dx + dy * dy) ** 0.5
        if travel >= self._REAL_MOVER_RATIO * bbox_diag:
            st["real_mover"] = True
            # Earned, not inherited — this is the only thing that restarts the
            # clock, and it is why a phantom cannot renew a stranger's
            # provenance by being born on their spot.
            st["mover_earned_ns"] = ts_ns
        if travel >= self._ARRIVAL_RATIO * bbox_diag:
            st["arrived"] = True

    def _transition(
        self,
        st: dict[str, Any],
        track_id: int,
        bbox: tuple[float, float, float, float],
        class_name: str,
        ts_ns: int,
        *,
        still: bool,
        demote: bool,
    ) -> None:
        prev_state = st["state"]
        if prev_state == MOTION_ACTIVE:
            if still:
                st["state"] = MOTION_STATIONARY
                st["state_since_ns"] = ts_ns
        elif prev_state == MOTION_STATIONARY:
            if demote:
                st["state"] = MOTION_ACTIVE
                st["state_since_ns"] = ts_ns
                self._forget_ghosts_at(bbox, class_name, track_id)
            elif ts_ns - st["state_since_ns"] >= self._park_threshold_ms * 1_000_000:
                st["state"] = MOTION_PARKED
                # Deliberately NOT resetting state_since_ns: the object has
                # been still since the stationary promotion, and "PARKED 5m"
                # should mean five minutes of stillness — not five minutes
                # since a bookkeeping transition (operator-reported: the
                # visible timer jumping back to 0 on the promotion reads as
                # a tracking glitch).
        elif prev_state == MOTION_PARKED and demote:
            st["state"] = MOTION_ACTIVE
            st["state_since_ns"] = ts_ns
            # The object provably moved — this spot is no longer a static-
            # object location, so newborns there must not inherit parked.
            self._forget_ghosts_at(bbox, class_name, track_id)

    def inherit_arrival(self, track_id: int) -> None:
        """A track that CONTINUES a known subject (anchor handover) is an
        arrival by provenance — its predecessor walked in, and the hold just
        proved they are the same person. Without this the successor starts
        earning travel from zero, and if it dies within a few seconds (live:
        patio 155→166 died before banking 0.35 diagonals) it is never
        anchor-eligible, so the NEXT break drops the baton. `real_mover` is
        deliberately NOT inherited — the phantom registry keeps its full-
        diagonal proof."""
        st = self._tracks.get(track_id)
        if st is not None:
            st["arrived"] = True

    def set_params(
        self,
        static_move_ratio: float,
        park_threshold_ms: int,
        static_window_ms: int,
        static_move_min_px: float,
    ) -> None:
        """Apply per-camera tuning (Settings → Cameras → Mirovanje) to a LIVE
        state machine — track histories and states survive the change, so the
        operator can widen the stillness radius on an occlusion-split scene
        and watch the flapping stop without resetting parked timers.

        All four, not two. `static_window_ms` and `static_move_min_px` are
        editable in the same panel and were read once, at construction; a
        MotionTracker is rebuilt only when its camera is evicted, which never
        happens on an always-on camera. So the api reported the new value, the
        UI rendered it as in force, and the running state machine kept the one
        it was born with for the life of the container — the exact failure the
        settings module says it exists to prevent."""
        self._static_move_ratio = static_move_ratio
        self._park_threshold_ms = park_threshold_ms
        self._static_window_ms = static_window_ms
        self._static_move_min_px = static_move_min_px

    def gc(self, ts_ns: int) -> None:
        cutoff = ts_ns - self._GC_GRACE_MS * 1_000_000
        stale = [tid for tid, st in self._tracks.items() if st["last_seen_ns"] < cutoff]
        for tid in stale:
            del self._tracks[tid]
        # Ghosts outlive their track by design (that's the whole point) but
        # expire once nothing has parked on the spot for the TTL.
        ghost_cutoff = ts_ns - self._GHOST_TTL_MS * 1_000_000
        expired = [tid for tid, g in self._ghosts.items() if g[3] < ghost_cutoff]
        for tid in expired:
            del self._ghosts[tid]


# ---------------------------------------------------------------------------
# Crop extraction — same NV12 fast-path the embedder uses, adapted to
# yield 0..N RGB crops at once for an OSNet batch.
# ---------------------------------------------------------------------------


_MIN_BBOX_SIDE_PX = 8


def _crop_nv12_to_rgb(
    nv12: np.ndarray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    logical_h: int,
    logical_w: int,
) -> np.ndarray | None:
    """Slice an NV12 flat buffer at bbox and return the RGB crop, or
    None if the bbox is too small / out of bounds. Coordinates are
    snapped to even pixels so chroma subsampling stays aligned."""
    x1 = max(0, min(logical_w - 1, x1))
    y1 = max(0, min(logical_h - 1, y1))
    x2 = max(0, min(logical_w, x2))
    y2 = max(0, min(logical_h, y2))
    if x2 - x1 < _MIN_BBOX_SIDE_PX or y2 - y1 < _MIN_BBOX_SIDE_PX:
        return None

    x1 &= ~1
    y1 &= ~1
    if x2 % 2:
        x2 = min(x2 + 1, logical_w)
        if x2 % 2:
            x2 -= 1
    if y2 % 2:
        y2 = min(y2 + 1, logical_h)
        if y2 % 2:
            y2 -= 1
    if x2 - x1 < _MIN_BBOX_SIDE_PX or y2 - y1 < _MIN_BBOX_SIDE_PX:
        return None

    crop_h = y2 - y1
    crop_w = x2 - x1
    y_crop = nv12[y1:y2, x1:x2]
    uv_top = logical_h + y1 // 2
    uv_bottom = logical_h + y2 // 2
    uv_crop = nv12[uv_top:uv_bottom, x1:x2]
    nv12_crop = np.empty((crop_h + crop_h // 2, crop_w), dtype=np.uint8)
    nv12_crop[:crop_h, :] = y_crop
    nv12_crop[crop_h:, :] = uv_crop
    return cv2.cvtColor(nv12_crop, cv2.COLOR_YUV2RGB_NV12)


def _crop_rgb(
    rgb: np.ndarray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> np.ndarray | None:
    """Same contract as `_crop_nv12_to_rgb` for ingestors that publish
    RGB instead of NV12 (e.g. CPU-only decoder path). No chroma
    alignment concerns."""
    h, w = rgb.shape[:2]
    x1 = max(0, min(w - 1, x1))
    y1 = max(0, min(h - 1, y1))
    x2 = max(0, min(w, x2))
    y2 = max(0, min(h, y2))
    if x2 - x1 < _MIN_BBOX_SIDE_PX or y2 - y1 < _MIN_BBOX_SIDE_PX:
        return None
    return rgb[y1:y2, x1:x2]


# ---------------------------------------------------------------------------
# Norfair adapter.
# ---------------------------------------------------------------------------


def _last_track_embedding(t) -> np.ndarray | None:
    """Most-recent OSNet embedding a Norfair tracked object carries. Norfair
    keeps `past_detections` per-tracker and the latest detection might lag an
    embedding (e.g. a freshly-spawned tracker before its first successful
    inference call). Shared by the ReID distance callback and the appearance
    anchor (anchor_hold.py) — one definition of "this track's appearance"."""
    d = t.last_detection
    if d is not None and getattr(d, "embedding", None) is not None:
        return d.embedding
    for past in reversed(getattr(t, "past_detections", []) or []):
        if getattr(past, "embedding", None) is not None:
            return past.embedding
    return None


def _make_reid_distance(threshold_of):
    """Build a Norfair ReID callback that compares L2-normalised appearance
    vectors via cosine distance. Missing embedding on either side returns a
    value ABOVE `threshold` so Norfair reads it as "no match".

    The no-match value is derived from the threshold rather than hardcoded:
    it used to return a flat 1.0, which only happened to work because the
    default threshold is 0.30. Raise `BABA_TRACKER_REID_DISTANCE_THRESHOLD` to
    1.0 or beyond — a legitimate thing to try when loosening re-ID — and every
    embedding-less pair would have silently MERGED instead of being rejected,
    exactly inverting the guard.

    `threshold_of` is read at CALL time rather than captured. It used to be a
    number taken once when the Norfair instance was built, and the instance is
    rebuilt only when `lost_seconds` or `reid_lost_seconds` changes — which
    resets every track on that camera. So editing this threshold in the UI did
    nothing at all until an unrelated knob was nudged, and then it landed
    retroactively along with a track reset nobody asked for."""

    def reid_distance(matched_not_init, unmatched_tracker) -> float:
        no_match = max(1.0, threshold_of()) + 1.0
        a = _last_track_embedding(unmatched_tracker)
        b = _last_track_embedding(matched_not_init)
        if a is None or b is None:
            return no_match
        # Both are L2-normalised by OSNetOnnxBackend.embed, so cosine
        # distance reduces to 1 - dot product.
        return float(1.0 - np.dot(a, b))

    return reid_distance


def _iou_distance(candidates: np.ndarray, objects: np.ndarray) -> np.ndarray:
    """Primary association metric: 1 - IoU between every detection's bbox
    and every track's estimate, each raveled as [x1, y1, x2, y2]."""
    c = candidates[:, None, :]
    o = objects[None, :, :]
    inter = np.clip(np.minimum(c[..., 2], o[..., 2]) - np.maximum(c[..., 0], o[..., 0]), 0.0, None) * np.clip(
        np.minimum(c[..., 3], o[..., 3]) - np.maximum(c[..., 1], o[..., 1]), 0.0, None
    )
    area_c = np.clip(c[..., 2] - c[..., 0], 0.0, None) * np.clip(c[..., 3] - c[..., 1], 0.0, None)
    area_o = np.clip(o[..., 2] - o[..., 0], 0.0, None) * np.clip(o[..., 3] - o[..., 1], 0.0, None)
    iou = np.divide(inter, area_c + area_o - inter, out=np.zeros_like(inter), where=inter > 0.0)
    return 1.0 - iou


_IOU = VectorizedDistance(_iou_distance)


def _norfair_tracker(**kwargs: Any) -> Tracker:
    t = Tracker(distance_function="iou", **kwargs)
    # Norfair's constructor takes a metric by name or a per-pair callable.
    # Its own "iou" divides 0 by 0 for a pair with no area between them, and
    # a NaN in the matrix raises out of `update`; ours scores it no overlap.
    t.distance_function = _IOU
    return t


class PerCameraTrackers:
    """Per-camera Norfair instance, lazily created. The state machine
    parameters are baked into the closure so swapping `cfg` between
    cameras is a no-op (we only ever construct one trackers object)."""

    def __init__(
        self,
        cfg: TrackerConfig,
        reid: OSNetOnnxBackend | None,
        tunables: Settings,
        on_rebuild: Callable[[str], None],
    ) -> None:
        self._cfg = cfg
        self._reid = reid
        self._tunables = tunables
        # Told when a camera's Norfair instance is replaced and its track ids
        # therefore start again.
        self._on_rebuild = on_rebuild
        # Per-camera Norfair generation counter. Bumped on every (re)creation;
        # the emitted epoch is _PROC_EPOCH + this, so it is unique per (process,
        # camera, generation) and strictly increases across process restarts.
        self._epoch_seq: dict[str, int] = {}
        self._trackers: dict[str, Tracker] = {}
        # (lost_seconds, reid_lost_seconds) each instance was built with —
        # these are baked into Norfair's counters at construction, so a
        # settings change recreates the camera's tracker (one-time track
        # reset, acceptable for a rare operator calibration action).
        self._applied: dict[str, tuple[float, float]] = {}

    def get(self, camera_id: str, lost_seconds: float, reid_lost_seconds: float) -> Tracker:
        t = self._trackers.get(camera_id)
        applied = self._applied.get(camera_id)
        if t is not None and applied != (lost_seconds, reid_lost_seconds):
            log.info(
                "tracker windows changed for camera=%s (lost=%ss reid=%ss) — "
                "recreating Norfair instance (tracks reset once)",
                camera_id,
                lost_seconds,
                reid_lost_seconds,
            )
            self._trackers.pop(camera_id, None)
            t = None
            rebuilt = True
        else:
            rebuilt = False
        if t is None:
            kwargs: dict[str, Any] = {
                # IoU distance space: 1.0 = no overlap, 0.0 = perfect.
                # 0.7 lets us accept overlaps as low as 30% IoU as a
                # tentative match, which matches the per-frame bbox
                # wobble we see from RT-DETR on stationary subjects.
                "distance_threshold": 0.7,
                # Set per update by `_advance`, which is the only place that
                # knows what one real observation is worth in nominal frames.
                # This value is a floor for the first call and nothing more.
                "initialization_delay": 0,
                # Seconds → nominal frames. Wall-clock correctness at any
                # actual per-camera rate comes from `_advance` replaying
                # skipped nominal ticks, not from this constant.
                "hit_counter_max": max(1, round(lost_seconds * self._cfg.frame_rate)),
                "past_detections_length": 5,
            }
            if self._reid is not None:
                reid_threshold = self._tunables.f("reid_distance_threshold")
                kwargs["reid_distance_function"] = _make_reid_distance(
                    lambda: self._tunables.f("reid_distance_threshold")
                )
                # Norfair's own copy is refreshed per update in `_advance`;
                # this is only its starting value.
                kwargs["reid_distance_threshold"] = reid_threshold
                kwargs["reid_hit_counter_max"] = max(
                    1, round(reid_lost_seconds * self._cfg.frame_rate)
                )
            t = _norfair_tracker(**kwargs)
            # New Norfair instance → track ids restart at 1; bump the camera's
            # generation so downstream can tell the reused ids apart.
            self._epoch_seq[camera_id] = self._epoch_seq.get(camera_id, -1) + 1
            if rebuilt:
                # A fresh Tracker allocates a fresh object factory, so the
                # next track on this camera is id 1 again — while every map
                # keyed by the old ids still holds their entries. The new
                # id 1 then inherits the previous id 1's motion history, its
                # class votes, its held box and its birth qualification.
                self._on_rebuild(camera_id)
            self._trackers[camera_id] = t
            self._applied[camera_id] = (lost_seconds, reid_lost_seconds)
            log.info(
                "created Norfair tracker for camera=%s (reid=%s, lost=%ss, reid_lost=%ss)",
                camera_id,
                "on" if self._reid is not None else "off",
                lost_seconds,
                reid_lost_seconds,
            )
        return t

    def epoch(self, camera_id: str) -> int:
        """Current tracker generation for a camera, unique per (process, camera,
        generation) and strictly increasing across restarts."""
        return _PROC_EPOCH + self._epoch_seq.get(camera_id, 0)

    def __len__(self) -> int:
        return len(self._trackers)

    def drop(self, camera_id: str) -> bool:
        """Forget a camera's Norfair tracker (e.g. camera disabled/removed).
        Recreated lazily on the next detection. Returns True if one existed."""
        self._applied.pop(camera_id, None)
        return self._trackers.pop(camera_id, None) is not None


def _advance(
    tracker: Tracker,
    detections: list[Detection],
    dt_s: float,
    frame_rate: int,
    reid_threshold: float | None = None,
) -> list[norfair.TrackedObject]:
    """Advance a Norfair tracker by `dt_s` seconds of wall time, then feed it
    this tick's detections.

    Norfair's counters are frame-based and its `period` argument only scales
    the GAIN side (hit() adds 2×period) — `tracker_step()` decays counters by
    exactly 1 per update() call regardless of period. With adaptive per-camera
    rates (cameras.idle_fps) that would make a lost track at 1 fps survive
    15× longer in wall time than the same track at 15 fps. So we make decay
    time-correct by REPLAYING the skipped nominal ticks as empty updates:
    each replay decays counters by 1 and advances the Kalman predict, exactly
    as if the detector had run on those frames and seen nothing. The real
    frame then lands with period=p so a continuously-seen track still nets
    +p per tick (gain 2p − p decay) and saturates, while a vanished one nets
    −p and dies after `lost_track_seconds` regardless of the current rate.

    The replay burst is capped at 3 s of nominal ticks: a longer gap is a
    camera/pipeline outage, and letting tracks linger slightly past such a
    gap is preferable to an unbounded replay loop."""
    p = round(dt_s * frame_rate)
    p = max(1, min(p, frame_rate * 3))
    # One real observation must not be enough to earn an id.
    #
    # Norfair gives a newborn `hit_counter = period` and calls it initializing
    # while that is <= `initialization_delay`. The delay used to be a constant
    # in REAL frames (frame_rate // 5 = 3) while the counter is in the NOMINAL
    # frames this function replays: a camera at 2 fps hands over p = 8, so
    # `8 <= 3` was false at every birth and no object was ever initializing.
    #
    # Two things followed. A single detection acquired an id immediately —
    # shed recorded 4057 births in a day on an empty yard. And Norfair only
    # offers INITIALIZING objects to its re-identification stage, so
    # `reid_distance_function`, `reid_distance_threshold` and
    # `reid_lost_seconds` — all wired, all exposed in the UI — could never
    # fire: a subject that walked behind the shed came back as a new id.
    #
    # p is what one real observation is worth right now, so a newborn is
    # initializing and a second observation clears it. It follows the actual
    # rate, including when adaptive fps changes it underneath.
    tracker.initialization_delay = p
    if reid_threshold is not None:
        # Live, for the same reason: Norfair keeps its own copy of the
        # threshold and the instance holding it is rebuilt only by a change
        # that resets every track on the camera.
        tracker.reid_distance_threshold = reid_threshold
    for _ in range(p - 1):
        tracker.update(None, period=1)
    out = tracker.update(detections=detections, period=p)
    # Norfair sizes a newborn's life at exactly `period` and assumes the next
    # observation arrives after that many decays. Under the replay above the
    # next real frame costs p_next decays, so a newborn born on a short
    # interval (the first frame after a restart is worth p = 1) is already
    # below zero when its second observation lands: dead, never initialised,
    # and — because Norfair offers every dead object to re-identification —
    # the target every later newborn of the same standing car merges INTO.
    # `merge` restores `initial_period * 2` = 2, which dies again next tick,
    # and the loop never emits. Measured on west after the 08:56 deploy of
    # 15.09: 13 minutes with two cars in plain view and zero tracks, the
    # departing car acquired only at the far gate, where its look had finally
    # drifted past the re-ID threshold.
    #
    # The dip also arms `reid_hit_counter` on a track that is matched every
    # tick, and Norfair drops such a track when that countdown runs out —
    # a continuously seen car died every `reid_lost_seconds` and came back as
    # a new id. So whatever was seen on this frame is alive: it carries at
    # least one nominal second of life and no re-identification countdown.
    seen = {id(d) for d in detections}
    for o in tracker.tracked_objects:
        if id(o.last_detection) in seen:
            o.hit_counter = max(o.hit_counter, frame_rate)
            o.reid_hit_counter = None
    return out


# ---------------------------------------------------------------------------
# Per-detection ancillary state. Norfair carries opaque `data` on each
# Detection through to the matched TrackedObject — we use that to pipe
# class_id / class_name / confidence through without parallel arrays.
# ---------------------------------------------------------------------------


class _DetData(msgspec.Struct, frozen=True):
    class_id: int
    class_name: str
    confidence: float
    # True when this detection cleared the per-camera BIRTH threshold. A track
    # is emitted only once it has matched at least one birth_eligible detection
    # (see `_tracked_to_wire`); below-birth detections keep an already-qualified
    # track alive but can't qualify one on their own.
    birth_eligible: bool = True


async def _build_norfair_detections(
    raw_dets: list[DetectionWire],
    rgb_crops: list[np.ndarray | None],
    embed: Callable[[list[np.ndarray]], Awaitable[np.ndarray]] | None,
    class_filter: tuple[int, ...],
) -> tuple[list[Detection], int, dict[str, int]]:
    """Combine raw detections with their RGB crops, run a single
    batched OSNet pass, and emit Norfair Detection objects with their
    embedding pre-attached.

    Class-filtered detections are dropped. A detection whose crop is missing
    is NOT: it is tracked WITHOUT an embedding. Losing the appearance vector
    costs re-ID (`_make_reid_distance` reads a missing embedding as "no
    match", so it simply never revives a dead track) — but the subject is
    still detected, still associated by IoU, and still becomes a track.
    Dropping it instead made a frame-ring stall silently blind the whole
    system: the detector kept publishing people at 0.93 while every one of
    them was discarded here, producing zero tracks and an empty Activity feed
    with not one error in the log (incident 2026-07-25).

    Returns (detections, n_missing_crop, dropped_by_class) so the caller can
    report the degradation loudly instead of swallowing it."""
    kept_idx: list[int] = []
    embed_slots: list[int] = []
    filt_crops: list[np.ndarray] = []
    dropped: dict[str, int] = {}
    for i, det in enumerate(raw_dets):
        if class_filter and det.class_id not in class_filter:
            # The declared allowlist is `detector_global_rules`, which the
            # operator edits in Settings and the detector already applies.
            # This is a SECOND one, frozen in the environment, and it has to
            # be widened by hand whenever the first one is. It dropped
            # silently — the only counterless discard on this path — so a
            # class enabled in the UI simply never became a track and nothing
            # anywhere said why.
            dropped[det.class_name] = dropped.get(det.class_name, 0) + 1
            continue
        slot = len(kept_idx)
        kept_idx.append(i)
        crop = rgb_crops[i]
        if crop is not None:
            embed_slots.append(slot)
            filt_crops.append(crop)

    embeddings = await embed(filt_crops) if embed is not None and filt_crops else None
    emb_by_slot: dict[int, np.ndarray] = {}
    if embeddings is not None:
        for k, slot in enumerate(embed_slots):
            emb_by_slot[slot] = embeddings[k]

    out: list[Detection] = []
    for slot, i in enumerate(kept_idx):
        det = raw_dets[i]
        points = np.array(
            [[det.x1, det.y1], [det.x2, det.y2]],
            dtype=np.float32,
        )
        out.append(
            Detection(
                points=points,
                scores=np.array([det.confidence, det.confidence], dtype=np.float32),
                embedding=emb_by_slot.get(slot),
                data=_DetData(
                    class_id=det.class_id,
                    class_name=det.class_name,
                    confidence=det.confidence,
                    birth_eligible=det.birth_eligible,
                ),
            )
        )
    return out, len(kept_idx) - len(embed_slots), dropped


# COCO vehicle classes. The nano detector boxes one vehicle under two classes
# (car+truck) as readily as one, and a cut-off car split across a full-frame
# box + a tile fragment can carry either label per id — so treat the whole
# group as same-class when deduping tracks. `person` is deliberately NOT here:
# a person in front of a car must never be deduped away.
_VEHICLE_GROUP = frozenset({2, 3, 5, 7})  # car, motorcycle, bus, truck
_TRACK_DEDUP_IOU = 0.6
_TRACK_CONTAIN_FRAC = 0.7


def _dedup_wire_tracks(tracks: list[TrackWire]) -> list[TrackWire]:
    """Suppress an emitted track that heavily overlaps a LARGER same-class (or
    same-vehicle-group) track — one physical object carrying two track ids.

    West's cut-off carport car parks as a full-car box plus, intermittently, a
    corner-fragment box on a second id (live: id=10 grey car + a transient 48%
    fragment). Keep the larger box (the whole object), drop the fragment.

    Overlap-gated so distinct neighbours survive: the red car and the grey car
    beside it measured IoU ~0.07 / 19% containment — nowhere near the 0.6 IoU /
    0.7 containment bar — so both stay. Stateless per frame, mirroring the
    detector's own dedup but at the track layer, where two persistent ids each
    keep their own detection stream alive so the per-frame detector dedup can't
    reach them. The suppressed track stays alive inside Norfair (like the
    birth-qualified gate); it's just invisible downstream this frame."""
    if len(tracks) < 2:
        return tracks
    kept: list[TrackWire] = []
    # Largest first: a fragment is by definition smaller than the whole object,
    # so the whole is always the survivor and `t` is always the ≤ candidate.
    for t in sorted(tracks, key=lambda w: -((w.x2 - w.x1) * (w.y2 - w.y1))):
        area_t = max(0.0, t.x2 - t.x1) * max(0.0, t.y2 - t.y1)
        dup = False
        for k in kept:
            same = t.class_id == k.class_id or (
                t.class_id in _VEHICLE_GROUP and k.class_id in _VEHICLE_GROUP
            )
            if not same:
                continue
            ix = max(0.0, min(t.x2, k.x2) - max(t.x1, k.x1))
            iy = max(0.0, min(t.y2, k.y2) - max(t.y1, k.y1))
            inter = ix * iy
            if inter <= 0.0:
                continue
            area_k = max(0.0, k.x2 - k.x1) * max(0.0, k.y2 - k.y1)
            union = area_t + area_k - inter
            iou = inter / union if union > 0 else 0.0
            contained = inter / area_t if area_t > 0 else 0.0
            if iou >= _TRACK_DEDUP_IOU or contained >= _TRACK_CONTAIN_FRAC:
                dup = True
                break
        if not dup:
            kept.append(t)
    return kept


def _matched_detection(to: norfair.TrackedObject) -> tuple[int, Any, _DetData] | None:
    """(track id, Norfair detection, our payload) for a tracked object that
    carries one of our detections; None for anything else."""
    if to.id is None:
        return None
    det = to.last_detection
    if det is None or not isinstance(det.data, _DetData):
        return None
    return int(to.id), det, det.data


def _tracked_to_wire(
    tracked_objects: list[norfair.TrackedObject],
    motion: MotionTracker,
    classes: ClassStabilizer,
    box_hold: BoxHold,
    ts_ns: int,
    birth_qualified: set[int],
    *,
    slug: str,
    spots: PhantomSpots | None,
    zones: ZoneMasks | None,
    frame_width: int,
    frame_height: int,
    last_matched: dict[int, tuple[int, int]],
    emit_stale_ns: int,
) -> tuple[list[TrackWire], int, dict[int, bool]]:
    """Map Norfair's confirmed `TrackedObject`s to our wire format, plus the
    number of class-stabilizer corrections this tick (telemetry: a flip storm
    is a scene-quality fingerprint). Unconfirmed (initialising) trackers are
    skipped — downstream wants a stable id, and Norfair's
    `initialization_delay` already gates against single-frame flashes.

    Two-threshold gate: `birth_qualified` (mutated in place, per camera) is the
    set of track ids that have matched at least one birth-eligible detection. A
    Norfair object that only ever matched below-birth (maintain-only) detections
    — a low-confidence clutter phantom — is NOT emitted, but keeps living inside
    Norfair so a later high-confidence hit can qualify it. Once qualified, a
    track stays emitted while any detection keeps it alive, however far its
    score decays (seated person, parked car).

    Two spatial gates run on top of it, both needing normalised coords (so a
    zero frame_width — a producer that predates the field — disables them
    rather than silently mis-mapping a polygon onto pixel coords):

    * ignore zones: operator says "never track anything here" (neighbour's
      property). Unconditional and cheap, so it runs first, before any state
      is spent on the track.
    * static-phantom spots: the registry has learned that nothing born at
      these pixels has EVER moved, over hours (see phantom_spots). Applied to
      what we EMIT, not to what we track — `observe()` runs before the gate
      and takes the live `ever_moved`, so a suppressed track that moves is
      emitted from that very tick and clears its spot for good."""
    out: list[TrackWire] = []
    flips = 0
    # track_id → "travelled far enough to be a subject rather than furniture".
    # A lower bar than the wire's `ever_moved`; only the anchor consumes it.
    arrived: dict[int, bool] = {}
    has_dims = frame_width > 0 and frame_height > 0
    now_s = ts_ns / 1e9
    for to in tracked_objects:
        matched = _matched_detection(to)
        if matched is None:
            continue
        tid, det, data = matched
        # Emission staleness: Norfair keeps a lost track alive for the whole
        # association window (patio: 100 s) and returns it COASTING — same
        # stale `last_detection`, Kalman-predicted box. Emitting that
        # published a phantom at the old spot while a new track already
        # followed the subject (observed: 57 s of overlapping double tracks
        # for one patio visit). A fresh match swaps the `last_detection`
        # OBJECT, so its identity is the match signal; past the cutoff the
        # track stays in Norfair (re-association memory, id continuity) but
        # is not emitted — presence beyond it belongs to the appearance
        # anchor, which verifies the pixels instead of trusting a prediction.
        det_key = id(det)
        prev = last_matched.get(tid)
        fresh = prev is None or prev[0] != det_key
        if fresh:
            last_matched[tid] = (det_key, ts_ns)
        elif ts_ns - prev[1] > emit_stale_ns:
            continue
        pts = det.points
        x1, y1 = float(pts[0][0]), float(pts[0][1])
        x2, y2 = float(pts[1][0]), float(pts[1][1])
        nbox = (
            (
                x1 / frame_width,
                y1 / frame_height,
                x2 / frame_width,
                y2 / frame_height,
            )
            if has_dims
            else None
        )
        # Operator-declared dead ground: nothing here is ever a subject, so
        # bail before spending class/motion/phantom state on it.
        if nbox is not None and zones is not None and zones.ignored(slug, nbox):
            continue
        if data.birth_eligible:
            birth_qualified.add(tid)
        if tid not in birth_qualified:
            continue  # maintain-only / phantom — invisible downstream
        # Stable class first — the motion tracker's parked-ghost registry
        # keys spots by the majority-vote class so a one-frame class flip
        # can't dodge (or wrongly claim) a ghost match at birth.
        s_class_id, s_class_name = classes.stable(
            int(to.id), data.class_id, data.class_name, data.confidence, ts_ns
        )
        if s_class_id != data.class_id:
            flips += 1
        # Only a tick that actually saw the subject may testify about whether
        # it moved. While Norfair coasts it returns the SAME `last_detection`
        # with a predicted box, and feeding those to the state machine filled
        # the stillness history with identical centroids — so a subject that
        # had LEFT read as perfectly still, was promoted to `stationary`, and
        # left a parked ghost at its exit box for the ghost registry's two
        # hours. The comment below says motion.update runs on the live box;
        # `fresh` is what makes that true.
        if fresh:
            m_state, m_since, m_ever_moved, m_arrived = motion.update(
                int(to.id), (x1, y1, x2, y2), s_class_name, ts_ns
            )
        else:
            m_state, m_since, m_ever_moved, m_arrived = motion.peek(int(to.id))
        arrived[int(to.id)] = m_arrived
        # Learned static clutter. `observe` both records the evidence and
        # returns the verdict — see its ordering contract. A spot inside a
        # parking zone is EXEMPT: a vehicle that sits in its designated spot
        # for days banks no mover and looks identical to clutter, but the
        # operator drew the zone there precisely because things park (and stay)
        # there. observe() still runs (records the birth + banks movers, so the
        # registry keeps learning and self-heals) — only the suppress verdict
        # is overridden.
        suppressed = (
            nbox is not None
            and spots is not None
            and spots.observe(slug, s_class_name, nbox, tid, m_ever_moved, now_s, data.confidence)
        )
        if suppressed and zones is not None and zones.in_parking(slug, nbox):
            suppressed = False
        if suppressed:
            continue
        # motion.update ran on the LIVE box (it must see real movement); the
        # hold only rewrites what we EMIT — a parked track keeps its
        # established whole-object box + score instead of a degraded fragment.
        (hx1, hy1, hx2, hy2), h_conf = box_hold.resolve(
            int(to.id), (x1, y1, x2, y2), data.confidence, m_state, ts_ns
        )
        out.append(
            TrackWire(
                track_id=int(to.id),
                x1=hx1,
                y1=hy1,
                x2=hx2,
                y2=hy2,
                class_id=s_class_id,
                class_name=s_class_name,
                confidence=h_conf,
                motion_state=m_state,
                state_since_ns=m_since,
                ever_moved=m_ever_moved,
                first_seen_ns=int(getattr(to, "_baba_first_ns", 0) or 0),
                # Evidence timestamp, not emission timestamp: `last_matched`
                # holds the tick this track last matched a REAL detection, so
                # the coasting frames before the staleness cutoff no longer
                # read as presence downstream.
                last_matched_ns=int(last_matched.get(tid, (0, ts_ns))[1]),
            )
        )
    # Drop qualified ids whose track has died out of Norfair, so the set can't
    # grow unbounded across the process lifetime.
    live_ids = {int(to.id) for to in tracked_objects if to.id is not None}
    birth_qualified &= live_ids
    for dead in [t for t in last_matched if t not in live_ids]:
        del last_matched[dead]
    if spots is not None:
        # Same bound on the registry's per-track bookkeeping. The spots
        # themselves survive — they're the accumulated evidence.
        spots.forget_tracks(slug, live_ids)
    # Collapse one physical object carried on two ids (west's cut-off car:
    # whole-box + corner fragment) to a single emitted box.
    out = _dedup_wire_tracks(out)
    return out, flips, arrived


# ---------------------------------------------------------------------------
# Service core.
# ---------------------------------------------------------------------------

_INTAKE_QUEUE_MAX = 64
_DROP_LOG_EVERY_S = 5.0


def _load_reid(cfg: TrackerConfig) -> OSNetOnnxBackend | None:
    """OSNet, or None for IoU-only tracking.

    A missing model file falls back to IoU-only so a stripped-down deployment
    still starts, with a clear warning; the operator can drop the ONNX in and
    bounce the container. A production build (`require_reid`) refuses that
    instead: silently downgrading to IoU-only means ID swaps through
    occlusions while reporting healthy.
    """
    reid: OSNetOnnxBackend | None = None
    if cfg.reid_model_path:
        model_path = Path(cfg.reid_model_path)
        if model_path.exists():
            try:
                reid = OSNetOnnxBackend(model_path)
            except Exception:
                log.exception(
                    "osnet load failed (path=%s) — falling back to IoU-only tracker",
                    model_path,
                )
        else:
            log.warning(
                "BABA_TRACKER_REID_MODEL=%s missing on disk — falling back to IoU-only tracker",
                model_path,
            )
    if reid is None and cfg.require_reid:
        raise RuntimeError(
            f"OSNet ReID unavailable (model={cfg.reid_model_path!r}) but require_reid is set "
            "(BABA_VARIANT nvidia/intel). Provide the ONNX in /models or set "
            "BABA_REQUIRE_REID=0 to explicitly run IoU-only tracking."
        )
    return reid


def _tunables(cfg: TrackerConfig) -> Settings:
    """Operator-tunable thresholds: env supplies the deployment default, the
    `tracking_defaults` row overrides it, and everything READS from here
    rather than copying — so a change in the UI lands on the next tick
    instead of the next restart."""
    return Settings(
        TRACKING_TUNABLES,
        {
            "anchor_hold_dist": cfg.anchor_hold_dist,
            "anchor_miss_limit": cfg.anchor_miss_limit,
            "anchor_check_interval_s": cfg.anchor_check_interval_s,
            "anchor_max_age_h": cfg.anchor_max_age_h,
            "anchor_max_age_nonperson_h": cfg.anchor_max_age_nonperson_h,
            "anchor_corroboration_min": cfg.anchor_corroboration_min,
            "identity_pet_threshold": cfg.identity_pet_threshold,
            "identity_margin": cfg.identity_margin,
            "reid_distance_threshold": cfg.reid_distance_threshold,
            "static_move_min_px": cfg.static_move_min_px,
            "static_window_ms": cfg.static_window_ms,
            "emit_stale_s": cfg.emit_stale_s,
            "phantom_min_births": PHANTOM_DEFAULT_MIN_BIRTHS,
            "phantom_min_span_s": PHANTOM_DEFAULT_MIN_SPAN_S,
            "phantom_real_subject_conf": PHANTOM_DEFAULT_REAL_CONF,
        },
    )


async def _every(stop: asyncio.Event, seconds: float) -> AsyncIterator[None]:
    """Tick every `seconds` until `stop` is set; a stop mid-wait ends it at once."""
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        if stop.is_set():
            return
        yield


def _crop(frame: RingFrame, bbox: tuple[float, float, float, float]) -> np.ndarray | None:
    x1, y1, x2, y2 = (int(v) for v in bbox)
    if frame.pixel_format == PIXEL_FORMAT_NV12:
        return _crop_nv12_to_rgb(frame.pixels, x1, y1, x2, y2, frame.height, frame.width)
    return _crop_rgb(frame.pixels, x1, y1, x2, y2)


@dataclass
class _TrackState:
    """Everything one camera keeps per Norfair track id.

    Norfair's id counter lives on its Tracker instance, so a rebuilt one
    numbers from 1 again. Dropping this whole keeps the new id 1 from
    inheriting the old id 1's motion history and parked timer, its class
    votes, its held box and its birth qualification.
    """

    motion: MotionTracker
    classes: ClassStabilizer = field(default_factory=ClassStabilizer)
    box_hold: BoxHold = field(default_factory=BoxHold)
    # Two-threshold tracking: ids that have matched a birth-eligible
    # detection. Only these are emitted; `_tracked_to_wire` latches on
    # qualify and prunes on death.
    birth_qualified: set[int] = field(default_factory=set)
    # tid → (id(last_detection), last_matched_ns) for the emission staleness
    # cutoff — see _tracked_to_wire.
    match_freshness: dict[int, tuple[int, int]] = field(default_factory=dict)
    # Initializing trackers seen on the previous tick — the activity
    # verdict's inertia, see `TrackerService._activity`.
    initializing: set[int] = field(default_factory=set)


@dataclass
class _Telemetry:
    """A camera's window for baba.telemetry.tracker.<slug>, flushed each minute."""

    births: int = 0
    flips: int = 0
    known: dict[int, int] = field(default_factory=dict)
    states: tuple[int, int, int] = (0, 0, 0)


@dataclass
class _Camera:
    """What the tracker knows about the camera itself. Survives a Norfair
    rebuild: the ring reader, the frame counter and the telemetry are still
    true when the track ids restart."""

    seen_ns: int = 0
    frames: int = 0
    # Previous batch's ingest timestamp — `_advance` replays the gap so
    # Norfair's decay stays time-correct when the ingestor drops to idle_fps.
    last_batch_ns: int | None = None
    reader: FrameRingReader | None = None
    telemetry: _Telemetry = field(default_factory=_Telemetry)
    no_crop_warned_ns: int = 0
    class_drop_warned_ns: dict[str, int] = field(default_factory=dict)


class _Intake:
    """One bounded queue and drain worker per camera.

    A slow or bursting camera backs up, and at the bound drops, only its own
    frames instead of stalling the single subscription callback for every
    camera. ~4 s of headroom at the cameras' detection rate. Overflow drops the
    STALEST frame (the tracker wants the freshest position) and says so,
    rate-limited per camera — nats.py's own subscription-level drop is silent
    and names no camera.
    """

    def __init__(
        self,
        handle: Callable[[DetectionsMessage], Awaitable[None]],
        stats: StatsCollector,
    ) -> None:
        self._handle = handle
        self._stats = stats
        self._queues: dict[str, asyncio.Queue[DetectionsMessage]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._drop_logged: dict[str, float] = {}

    def put(self, wire: DetectionsMessage) -> None:
        cam = wire.camera_id
        q = self._queues.get(cam)
        if q is None:
            q = self._queues[cam] = asyncio.Queue(maxsize=_INTAKE_QUEUE_MAX)
            self._workers[cam] = spawn(self._drain(q), name=f"tracker-intake-{cam}", log=log)
        if not q.full():
            q.put_nowait(wire)
            return
        q.get_nowait()
        q.put_nowait(wire)
        self._stats.incr("dropped")
        now = time.monotonic()
        if now - self._drop_logged.get(cam, 0.0) >= _DROP_LOG_EVERY_S:
            self._drop_logged[cam] = now
            log.warning(
                "tracker slow consumer cam=%s: intake queue full (%d), "
                "dropping stale frames — detections arriving faster than tracked",
                cam,
                _INTAKE_QUEUE_MAX,
            )

    async def _drain(self, q: asyncio.Queue[DetectionsMessage]) -> None:
        while True:
            await self._handle(await q.get())

    def drop(self, cam: str) -> None:
        self._queues.pop(cam, None)
        self._drop_logged.pop(cam, None)
        w = self._workers.pop(cam, None)
        if w is not None:
            w.cancel()

    async def stop(self) -> None:
        workers = list(self._workers.values())
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w


class TrackerService:
    """Association and publication for every camera on the bus."""

    def __init__(self, cfg: TrackerConfig, nc, reid: OSNetOnnxBackend | None) -> None:
        self._cfg = cfg
        self._nc = nc
        self._reid = reid
        self._tunables = _tunables(cfg)
        self.stats = StatsCollector(service="tracker")
        self._health = HealthMarker("baba", "tracker")
        self._encoder = msgspec.msgpack.Encoder()
        self._decoder = msgspec.msgpack.Decoder(DetectionsMessage)
        self._trackers = PerCameraTrackers(cfg, reid, self._tunables, self._reset_tracks)
        # The hold and the identity stamp are appearance comparisons in OSNet
        # space, so without ReID both stay off rather than degrade to
        # something that can't tell a person from a couch.
        self._anchor_hold = AnchorHold(
            self._tunables, classes=cfg.anchor_classes, available=reid is not None
        )
        self._ident_stamp = IdentityStamp(cfg.dsn, self._tunables, available=reid is not None)
        self._settings = CameraSettings(
            cfg.dsn, on_change=self._apply_camera_settings, tunables=self._tunables
        )
        self._spots: PhantomSpots | None = None
        self._zones: ZoneMasks | None = None
        self._cameras: dict[str, _Camera] = {}
        self._tracks: dict[str, _TrackState] = {}
        self._intake = _Intake(self.handle, self.stats)
        # OSNet off the event loop: one thread, so every camera's crops queue
        # for the same session instead of contending for it, while the loop
        # keeps associating and publishing for the others.
        self._osnet = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="osnet")
            if reid is not None
            else None
        )

    async def start(self) -> None:
        await self._settings.start()
        await self._settings.publish_defaults(self._tunables)
        # Static-phantom suppression (self-learning) + ignore zones (operator).
        # Both fail toward MORE detections: if Postgres is unreachable neither
        # can load, nothing is suppressed, and the tracker behaves exactly as
        # it did before they existed. That is the safe direction — a phantom
        # the operator can see beats a person silently dropped — so this must
        # not be fatal the way a missing OSNet model is.
        spots = PhantomSpots(self._cfg.dsn, self._tunables)
        try:
            await spots.start()
        except Exception:
            log.exception("phantom spots failed to start — static-clutter suppression is OFF")
        else:
            self._spots = spots
        zones = ZoneMasks(self._cfg.dsn)
        try:
            await zones.start()
        except Exception:
            log.exception("ignore zones failed to load — no camera is masked")
        else:
            self._zones = zones

    def background(self, stop: asyncio.Event) -> list[asyncio.Task[None]]:
        return [
            spawn(self._beat(stop), name="tracker-health", log=log),
            spawn(self._evict_idle(stop), name="tracker-idle-cleanup", log=log),
            spawn(self._flush_telemetry(stop), name="tracker-telemetry", log=log),
            spawn(self._flush_spots(stop), name="tracker-spots-flush", log=log),
            spawn(self._ident_stamp.refresh_loop(), name="tracker-identity-refresh", log=log),
        ]

    async def stop(self) -> None:
        await self._intake.stop()
        if self._osnet is not None:
            self._osnet.shutdown(wait=True, cancel_futures=True)
        await self.stats.stop()
        await self._settings.stop()
        if self._spots is not None:
            # Flushes the in-flight window before closing the pool.
            await self._spots.stop()
        if self._zones is not None:
            await self._zones.stop()
        for cam in self._cameras.values():
            if cam.reader is not None:
                with contextlib.suppress(Exception):
                    cam.reader.detach()

    async def route(self, msg) -> None:
        # The subscription callback only decodes and hands off to the owning
        # camera's queue; it never waits on tracking work.
        try:
            wire = self._decoder.decode(msg.data)
        except Exception:
            log.exception("tracker: undecodable detection message dropped")
            return
        self._intake.put(wire)

    async def handle(self, wire: DetectionsMessage) -> None:
        try:
            await self._track(wire)
        except Exception:
            log.exception("tracker handler failed")

    # --- per-camera state ---------------------------------------------------

    def _camera(self, slug: str) -> _Camera:
        cam = self._cameras.get(slug)
        if cam is None:
            cam = self._cameras[slug] = _Camera()
        return cam

    def _track_state(self, slug: str) -> _TrackState:
        ts = self._tracks.get(slug)
        if ts is None:
            ts = self._tracks[slug] = _TrackState(
                MotionTracker(
                    static_move_ratio=self._settings.stillness_ratio(
                        slug, self._cfg.static_move_ratio
                    ),
                    static_move_min_px=self._tunables.f("static_move_min_px"),
                    static_window_ms=self._tunables.i("static_window_ms"),
                    park_threshold_ms=self._settings.park_ms(slug, self._cfg.park_threshold_ms),
                )
            )
        return ts

    def _reset_tracks(self, slug: str) -> None:
        """Forget everything keyed by this camera's track ids, keeping the
        camera itself."""
        self._tracks.pop(slug, None)
        if self._spots is not None:
            # Per-track bookkeeping only — the birth evidence for this camera
            # stays in Postgres and reloads if the camera comes back.
            self._spots.evict_camera(slug)
        self._anchor_hold.forget_camera(slug)

    def _evict(self, slug: str) -> None:
        """Drop every piece of per-camera state — the idle sweeper's and the
        settings listener's (camera disabled or removed in the UI)."""
        self._trackers.drop(slug)
        self._reset_tracks(slug)
        self._ident_stamp.forget_camera(slug)
        self._intake.drop(slug)
        cam = self._cameras.pop(slug, None)
        if cam is not None and cam.reader is not None:
            with contextlib.suppress(Exception):
                cam.reader.detach()

    def _apply_camera_settings(self) -> None:
        """Push fresh per-camera stillness params onto LIVE MotionTrackers
        (state survives) and evict cameras the operator disabled."""
        for slug, ts in list(self._tracks.items()):
            ts.motion.set_params(
                self._settings.stillness_ratio(slug, self._cfg.static_move_ratio),
                self._settings.park_ms(slug, self._cfg.park_threshold_ms),
                int(self._tunables.f("static_window_ms")),
                float(self._tunables.f("static_move_min_px")),
            )
        for slug in self._settings.disabled_slugs():
            if slug in self._cameras or slug in self._tracks:
                log.info("evicting disabled camera state cam=%s", slug)
                self._evict(slug)

    # --- one detection batch ------------------------------------------------

    async def _track(self, wire: DetectionsMessage) -> None:
        slug = wire.camera_id
        self.stats.incr("messages")
        self.stats.incr("detections_in", len(wire.detections))
        cam = self._camera(slug)
        cam.seen_ns = time.time_ns()
        tracker = self._trackers.get(
            slug,
            self._settings.lost_seconds(slug, self._cfg.lost_track_seconds),
            self._settings.reid_lost_seconds(slug, self._cfg.reid_lost_seconds),
        )
        ts = self._track_state(slug)

        dets = self._admit(wire)
        frame, crops = self._frame(wire, cam, dets)
        norfair_dets, n_no_crop, dropped_class = await _build_norfair_detections(
            dets, crops, self._embed if self._reid is not None else None, self._cfg.class_id_filter
        )
        self._warn_unusable(wire, cam, len(dets), n_no_crop, dropped_class)
        with self.stats.timer("assoc_ms"):
            tracked = _advance(
                tracker,
                norfair_dets,
                self._dt(cam, wire),
                self._cfg.frame_rate,
                self._tunables.f("reid_distance_threshold") if self._reid is not None else None,
            )
        # Stamp each Norfair object with the tick it FIRST appeared on —
        # including while it is still initialising, which is the whole point:
        # by the time an object earns an id and clears the birth gate, the
        # subject has been on camera for seconds. Stored ON the object (no id()
        # bookkeeping: Python reuses ids after GC, and this dies with the
        # object instead of leaking or colliding).
        for to in tracker.tracked_objects:
            if getattr(to, "_baba_first_ns", None) is None:
                to._baba_first_ns = wire.timestamp_ns
        tracks_out, flips, arrived = _tracked_to_wire(
            tracked,
            ts.motion,
            ts.classes,
            ts.box_hold,
            wire.timestamp_ns,
            ts.birth_qualified,
            slug=slug,
            spots=self._spots,
            zones=self._zones,
            frame_width=wire.frame_width,
            frame_height=wire.frame_height,
            last_matched=ts.match_freshness,
            emit_stale_ns=int(self._tunables.f("emit_stale_s") * 1e9),
        )
        appearance = self._appearances(tracked)
        if self._anchor_hold.enabled:
            tracks_out = await self._hold(wire, ts, tracks_out, arrived, appearance, frame, dets)
        # Identity at the source: name matched pets/vehicles on the wire. Runs
        # AFTER the anchor merge so a held car keeps its name (sticky per track
        # id — the synthetic wire reuses the id).
        if self._ident_stamp.enabled:
            tracks_out = self._ident_stamp.stamp(slug, tracks_out, appearance, wire.timestamp_ns)
        self._count(cam, wire, tracks_out, flips)
        self.stats.incr("tracks_out", len(tracks_out))
        self.stats.observe("active_tracks", len(tracked))
        self.stats.set_gauge("cameras", len(self._trackers))

        await self._nc.publish(
            SUBJECT_ACTIVITY_TEMPLATE.format(camera_id=slug),
            self._encoder.encode(self._activity(wire, tracker, ts, tracks_out)),
        )
        ts.motion.gc(wire.timestamp_ns)
        ts.classes.gc(wire.timestamp_ns)
        ts.box_hold.gc(wire.timestamp_ns)
        cam.frames += 1
        await self._nc.publish(
            SUBJECT_TRACKS_TEMPLATE.format(camera_id=slug),
            self._encoder.encode(
                TracksMessage(
                    camera_id=slug,
                    sequence=wire.sequence,
                    timestamp_ns=wire.timestamp_ns,
                    tracks=tracks_out,
                    pts_ns=wire.pts_ns,
                    frame_width=wire.frame_width,
                    frame_height=wire.frame_height,
                    epoch=self._trackers.epoch(slug),
                )
            ),
        )
        if cam.frames % 30 == 0:
            log.info(
                "camera=%s frames_tracked=%d active_tracks=%d reid=%s",
                slug,
                cam.frames,
                len(tracks_out),
                "on" if self._reid is not None else "off",
            )

    def _ignored(self, wire: DetectionsMessage, bbox: tuple[float, float, float, float]) -> bool:
        if self._zones is None or wire.frame_width <= 0 or wire.frame_height <= 0:
            return False
        fw, fh = wire.frame_width, wire.frame_height
        return self._zones.ignored(
            wire.camera_id, (bbox[0] / fw, bbox[1] / fh, bbox[2] / fw, bbox[3] / fh)
        )

    def _admit(self, wire: DetectionsMessage) -> list[DetectionWire]:
        """This batch's detections minus operator-declared dead ground.

        Ignored ground dies at the INPUT, not just at emission. The
        emission-only gate kept feeding Norfair: the fan's detections birthed
        an internal (never-emitted) track every few seconds, the activity
        verdict counted it as "initializing" round the clock, and the camera
        never earned its idle rate — the plant ran at full speed to discard
        the same junk. Dropping them here also spares their OSNet crops and
        keeps them out of anchor corroboration and re-seat candidate pools.
        """
        kept = [d for d in wire.detections if not self._ignored(wire, (d.x1, d.y1, d.x2, d.y2))]
        if len(kept) != len(wire.detections):
            self.stats.incr("dets_ignored_zone", len(wire.detections) - len(kept))
        return kept

    def _frame(
        self, wire: DetectionsMessage, cam: _Camera, dets: list[DetectionWire]
    ) -> tuple[RingFrame | None, list[np.ndarray | None]]:
        """The ring frame this batch was detected on, and each detection's RGB crop.

        A sequence that has aged out of the ring gives no frame: the tick is
        still tracked, by IoU alone, since a missing frame is usually
        transient (camera reset, ring re-init) and shouldn't drop tracks.
        Fetched even on a 0-detection batch while the camera holds an
        appearance anchor — the anchor check needs the pixels precisely when
        the detector sees nothing.
        """
        crops: list[np.ndarray | None] = [None] * len(dets)
        if self._reid is None or not (dets or self._anchor_hold.has(wire.camera_id)):
            return None, crops
        try:
            if cam.reader is None:
                cam.reader = FrameRingReader(wire.camera_id)
            frame = cam.reader.get_by_sequence(wire.sequence)
        except Exception:
            log.exception("frame ring lookup failed cam=%s", wire.camera_id)
            return None, crops
        if frame is not None:
            for i, d in enumerate(dets):
                crops[i] = _crop(frame, (d.x1, d.y1, d.x2, d.y2))
        return frame, crops

    def _warn_unusable(
        self,
        wire: DetectionsMessage,
        cam: _Camera,
        n_dets: int,
        n_no_crop: int,
        dropped_class: dict[str, int],
    ) -> None:
        now = time.time_ns()
        for cname, n in dropped_class.items():
            self.stats.incr("dets_dropped_class", n)
            if now - cam.class_drop_warned_ns.get(cname, 0) > int(600e9):
                cam.class_drop_warned_ns[cname] = now
                log.warning(
                    "cam=%s dropping %r detections — the detector publishes "
                    "it but BABA_TRACKER_CLASSES does not list it, so it can "
                    "never become a track. Enabling a class in Settings is "
                    "not enough on its own; this list has to agree.",
                    wire.camera_id, cname,
                )
        if n_no_crop:
            # Tracking continues without appearance (IoU only), but re-ID is
            # blind for these — say so. A frame-ring stall used to discard
            # them outright and blind the pipeline in silence.
            self.stats.incr("dets_without_crop", n_no_crop)
            if now - cam.no_crop_warned_ns > int(30e9):
                cam.no_crop_warned_ns = now
                log.warning(
                    "cam=%s %d/%d detections have no crop — frame %d missing "
                    "from the ring; tracking them by IoU only, re-ID degraded "
                    "(camera fps too high for the pipeline, or the ring was "
                    "recreated and this reader is behind)",
                    wire.camera_id,
                    n_no_crop,
                    n_dets,
                    wire.sequence,
                )

    def _dt(self, cam: _Camera, wire: DetectionsMessage) -> float:
        prev, cam.last_batch_ns = cam.last_batch_ns, wire.timestamp_ns
        if prev is not None and wire.timestamp_ns > prev:
            return (wire.timestamp_ns - prev) / 1e9
        return 1.0 / self._cfg.frame_rate

    def _appearances(self, tracked: list[norfair.TrackedObject]) -> dict[int, np.ndarray]:
        """This tick's OSNet appearance per live track — shared by the anchor
        (settled-subject snapshot) and the identity stamp."""
        if not (self._anchor_hold.enabled or self._ident_stamp.enabled):
            return {}
        out: dict[int, np.ndarray] = {}
        for to in tracked:
            if to.id is None:
                continue
            e = _last_track_embedding(to)
            if e is not None:
                out[int(to.id)] = e
        return out

    async def _embed(self, crops: list[np.ndarray]) -> np.ndarray:
        reid = self._reid

        def run() -> np.ndarray:
            with self.stats.timer("reid_ms"):
                return reid.embed(crops)

        return await asyncio.get_running_loop().run_in_executor(self._osnet, run)

    async def _embed_at(
        self, frame: RingFrame, bbox: tuple[float, float, float, float]
    ) -> np.ndarray | None:
        c = _crop(frame, bbox)
        if c is None or c.size == 0 or self._reid is None:
            return None
        e = await self._embed([c])
        return e[0] if len(e) else None

    async def _hold(
        self,
        wire: DetectionsMessage,
        ts: _TrackState,
        tracks_out: list[TrackWire],
        arrived: dict[int, bool],
        appearance: dict[int, np.ndarray],
        frame: RingFrame | None,
        dets: list[DetectionWire],
    ) -> list[TrackWire]:
        """This tick's emissions merged with the appearance anchors' holds.

        Settled subjects register or refresh their anchor from the emitted
        tracks; an anchor whose track died emits a synthetic hold wire while
        the spot still LOOKS like them. Downstream (telemetry, activity
        verdict, event-manager) sees one merged list — the hold is
        indistinguishable from a parked track, which is the point.
        """
        slug = wire.camera_id
        crop_embed = functools.partial(self._embed_at, frame) if frame is not None else None
        for w in tracks_out:
            await self._anchor_hold.note(
                slug,
                w,
                appearance.get(w.track_id),
                wire.timestamp_ns,
                wire.frame_width,
                wire.frame_height,
                crop_embed,
                arrived=arrived.get(w.track_id, False),
            )
        held, continuations = await self._anchor_hold.sweep(
            slug,
            tracks_out,
            crop_embed,
            wire.timestamp_ns,
            # Every published detection, not only the anchorable classes: the
            # hold decides for itself which of them may corroborate which
            # subject. Filtering here threw away the patio's dog/cat boxes,
            # which are overwhelmingly the SAME SEATED PERSON under a flipped
            # label — measured 8k-19k pet detections and up to 3956 class
            # flips a day on that one camera.
            candidate_dets=[(d.class_id, (d.x1, d.y1, d.x2, d.y2)) for d in dets],
            frame_w=wire.frame_width,
            frame_h=wire.frame_height,
            emb_by_tid=appearance,
            is_ignored=functools.partial(self._ignored, wire),
        )
        # Stitch the seam. The hold just proved this new id is the subject it
        # was holding, so say so on the wire rather than leaving the
        # event-manager to rediscover it through re-ID — which is exactly what
        # fails on a person sitting with their back to the camera.
        if continuations:
            # The successor inherits "arrived under its own power" — see
            # MotionTracker.inherit_arrival. Before the next tick's note() so
            # the new track is anchor-eligible from its first emission, not
            # after re-earning travel.
            for tid in continuations:
                ts.motion.inherit_arrival(tid)
            tracks_out = [
                msgspec.structs.replace(
                    t,
                    continues_track_id=continuations[t.track_id][0],
                    identity_gid=t.identity_gid or continuations[t.track_id][1],
                    identity_name=t.identity_name or continuations[t.track_id][2],
                )
                if t.track_id in continuations
                else t
                for t in tracks_out
            ]
        return tracks_out + held

    def _count(
        self, cam: _Camera, wire: DetectionsMessage, tracks_out: list[TrackWire], flips: int
    ) -> None:
        tel = cam.telemetry
        tel.flips += flips
        for t in tracks_out:
            if t.track_id not in tel.known:
                tel.births += 1
            tel.known[t.track_id] = wire.timestamp_ns
        tel.states = (
            sum(1 for t in tracks_out if t.motion_state == MOTION_ACTIVE),
            sum(1 for t in tracks_out if t.motion_state == MOTION_STATIONARY),
            sum(1 for t in tracks_out if t.motion_state == MOTION_PARKED),
        )

    def _activity(
        self,
        wire: DetectionsMessage,
        tracker: Tracker,
        ts: _TrackState,
        tracks_out: list[TrackWire],
    ) -> ActivityMessage:
        """Scene-activity verdict for the ingestor's adaptive frame rate.

        "Active" = anything not parked, or an initializing Norfair tracker that
        has survived TWO consecutive ticks. A single-frame FP flicker spawns a
        fresh initializing tracker every time it fires, and counting those
        re-armed the ingestor's active-hold forever on fully-parked scenes
        (operator report: parked-only camera pinned at target_fps). A real
        newcomer keeps being detected frame after frame, so the inertia costs
        it one extra frame of ramp-up (~1 s at idle_fps=1). Keyed by object
        identity: Norfair keeps TrackedObject instances alive across updates,
        and a freed ghost can't reappear in the next tick's set by definition.
        """
        init_now: set[int] = set()
        person_incoming = False
        for to in tracker.tracked_objects:
            if not getattr(to, "is_initializing", False) or not to.hit_counter_is_positive:
                continue
            init_now.add(id(to))
            det = to.last_detection
            data = det.data if det is not None else None
            # A person never waits out the inertia: presence is the one signal
            # where a second of verdict latency is a second of missed coverage.
            # FP person-flickers cost at most one 15 s active-hold — acceptable
            # versus ramping late on a real arrival. Exception: a "person"
            # materialising exactly on a STATIC parked person-spot (a ghost
            # chain that never really moved) is the clutter phantom
            # re-flickering, not an arrival — without this check one confident
            # phantom pins the camera at target_fps around the clock. A flash
            # on a REAL parked person's spot does count: it keeps the camera at
            # target fps so a seated person stays observed.
            if isinstance(data, _DetData) and data.class_id == _PERSON_CLASS_ID:
                pts = det.points
                det_bbox = (float(pts[0][0]), float(pts[0][1]), float(pts[1][0]), float(pts[1][1]))
                if not ts.motion.matches_static_ghost(det_bbox, data.class_name, wire.timestamp_ns):
                    person_incoming = True
        n_initializing = len(init_now & ts.initializing)
        ts.initializing = init_now
        n_parked = sum(1 for t in tracks_out if t.motion_state == MOTION_PARKED)
        return ActivityMessage(
            camera_id=wire.camera_id,
            active=person_incoming or n_initializing > 0 or n_parked < len(tracks_out),
            timestamp_ns=wire.timestamp_ns,
            n_tracks=len(tracks_out),
            n_initializing=n_initializing,
            n_parked=n_parked,
        )

    # --- background loops ---------------------------------------------------

    async def _beat(self, stop: asyncio.Event) -> None:
        self._health.touch()
        async for _ in _every(stop, 5):
            self._health.touch()

    async def _evict_idle(self, stop: asyncio.Event) -> None:
        idle_ns = self._cfg.camera_idle_s * 1_000_000_000
        async for _ in _every(stop, 30):
            now = time.time_ns()
            for slug in [s for s, cam in self._cameras.items() if now - cam.seen_ns > idle_ns]:
                self._evict(slug)
                log.info(
                    "evicted idle camera state cam=%s (no detections > %ds)",
                    slug,
                    self._cfg.camera_idle_s,
                )

    async def _flush_telemetry(self, stop: asyncio.Event) -> None:
        last = time.monotonic()
        async for _ in _every(stop, 60):
            now_mono = time.monotonic()
            prune_ns = time.time_ns() - 600_000_000_000  # forget ids idle >10 min
            for slug, cam in list(self._cameras.items()):
                tel = cam.telemetry
                tel.known = {k: v for k, v in tel.known.items() if v > prune_ns}
                a, st_, pk = tel.states
                anchor_stats = self._anchor_hold.drain_stats(slug)
                msg_out = TrackerTelemetry(
                    camera_id=slug,
                    window_s=now_mono - last,
                    births=tel.births,
                    flips=tel.flips,
                    n_active=a,
                    n_stationary=st_,
                    n_parked=pk,
                    anchors_held=anchor_stats.get("held", 0),
                    anchors_released=anchor_stats.get("released", 0),
                    anchors_released_uncorroborated=anchor_stats.get("uncorroborated", 0),
                    anchors_refused=anchor_stats.get("refused", 0),
                )
                tel.births = 0
                tel.flips = 0
                await self._nc.publish(
                    SUBJECT_TELEMETRY_TEMPLATE.format(source="tracker", camera_id=slug),
                    self._encoder.encode(msg_out),
                )
            last = now_mono

    async def _flush_spots(self, stop: asyncio.Event) -> None:
        # Birth evidence is counted in memory on the hot path and written back
        # on this timer — a phantom births ~1/min per camera, so per-birth
        # writes would be pure overhead. A crash loses at most one window; the
        # counts are cumulative and simply resume.
        ticks = 0
        async for _ in _every(stop, 30):
            if self._spots is None:
                continue
            await self._spots.flush()
            ticks += 1
            # ~every 30 min: pick up cameras added since start and drop spots
            # nothing has been born at for the prune window.
            if ticks % 60 == 0:
                await self._spots.refresh()
                await self._spots.prune()
                total, sup = self._spots.stats()
                log.info("phantom spots: %d tracked, %d suppressing", total, sup)


async def run(cfg: TrackerConfig) -> None:
    nc = await nats_connect(cfg.nats_url, name="tracker")
    log.info("connected to %s", mask_credentials(cfg.nats_url))
    reid = _load_reid(cfg)
    service = TrackerService(cfg, nc, reid)
    await service.start()
    await nc.subscribe(SUBJECT_DETECTIONS_IN, cb=service.route)
    log.info(
        "subscribed to %s (class filter: %s, reid: %s)",
        SUBJECT_DETECTIONS_IN,
        cfg.class_id_filter or "ALL",
        "on" if reid is not None else "off",
    )
    await service.stats.start(nc)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = service.background(stop)
    await stop.wait()
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await service.stop()
    await drain_quietly(nc)


def main() -> None:
    setup_logging("tracker")
    cfg = TrackerConfig.from_env()
    run_service(run(cfg))


if __name__ == "__main__":
    main()
