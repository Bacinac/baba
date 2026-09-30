"""Appearance-anchored presence hold for settled subjects.

The problem (measured live on patio, 2026-07-21): a person who arrives moving
births a track at high confidence, then sits down — and their detections decay
to 0.26–0.41, all birth-ineligible, often with multi-second gaps. Once the
Norfair track dies, nothing can re-establish it: birth-ineligible detections
maintain but never qualify, and lowering the birth gate would qualify every
static-clutter phantom instead. Detector confidence CANNOT distinguish "seated
real person" from "empty-couch false positive" — both emit the same ~0.3
person box. The only signal that separates them is APPEARANCE.

So: when a confirmed real person settles, remember what their spot LOOKS like,
and hold their track alive for as long as the spot still looks like them.

  * ANCHOR — an emitted (birth-qualified, past suppression) track of an
    anchored class, from its FIRST emitted tick (waiting for it to settle meant
    the hold could only arm from tracks that never needed it — see note()) →
    store its bbox + OSNet embedding of its crop, PLUS a CONTEXT patch embedding (an adjacent static patch — furniture,
    floor). Only a real mover can create an anchor: an empty-couch FP never
    births an emitted track in the first place. NOT at the frame edge: a person
    "settling" at the door is leaving, not sitting, and a partial edge crop
    gives razor-thin release margins (live: door release read 0.325 vs the
    0.30 gate). Anchors are for seats, not exits.
  * HOLD — while the anchor's own Norfair track is gone, periodically embed the
    CURRENT frame at the anchor bbox and compare: distance below the hold
    threshold means the person is still there → emit a synthetic TrackWire with
    the SAME track id (parked, ever_moved) so person_count, zone occupancy and
    dwell holds (the patio heater) survive with no detections at all. The
    anchor embedding is EMA-updated on every confirmed check so hours of light
    drift never accumulate into a false release. (Global GAIN changes — patio
    light on/off, dusk — barely move OSNet at all: measured 0.045–0.070.
    The EMA + normalisation absorb those without any special casing.)
  * GLOBAL-CHANGE RE-BASELINE — an IR cut-filter flip restructures the whole
    image in one frame (measured: same occupied crop day-vs-grayscale = 0.51 —
    indistinguishable from "empty" at 0.57). A person LEAVING changes only
    their own spot; an IR flip changes the CONTEXT patch too. So on a spot
    mismatch, check the context: context ALSO moved → global illumination
    event → re-capture both embeddings from the current frame and keep
    holding (no miss). Context stable → the person really left → miss.
  * RELEASE — only through:
      1. appearance: the spot stops looking like the person while the context
         is stable, `miss_limit` consecutive checks (~6 s) — they left;
      2. takeover: a live emitted person track overlaps the anchor — they
         stood up and the real track carries presence again;
      3. corroboration decay — the PRIMARY liveness signal, not a backstop.
         Appearance separation is only trustworthy in good light: measured
         0.52 (person vs empty seat, sunlit) but 0.234 on a small DARK night
         crop — below the 0.30 gate, so at night the appearance check cannot
         see a departure at all (live incident 2026-07-22: a door anchor held
         an empty doorway 15 min and was finally released not by the person
         leaving but by SUNRISE changing the scene, 0.684). A detector hit is
         the signal that does discriminate in both regimes: a real seated
         person yields hits on ~75% of frames (any confidence — birth-
         ineligible counts), an empty spot yields none. So if NO person
         detection has touched the anchor for `corroboration_min`, release —
         and keep that window SHORT (default 90 s: a seated person at ~2 fps
         is corroborated every ~0.7 s, so 90 s of silence is emptiness, while
         a phantom is bounded to 90 s instead of a quarter hour);
      4. sanity age cap (warning-logged safety valve, not a policy).
    Detection gaps, suppression and every other trigger leave it alone.

Validated from recordings before implementation and live on the operator's
smoke break (see memory patio-presence-anchor-hold): hold at 0.062, seat
release at ~0.52, empty-drift noise ≤ 0.14 over 25 min of moving sun.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np
from baba_core import PET_GROUP, VEHICLE_GROUP
from baba_core.detection_gate import size_pct as gate_size_pct
from baba_core.pipeline_settings import Settings
from baba_core.wire import TrackWire

log = logging.getLogger("baba.tracker.anchor")

CropEmbed = Callable[[tuple[float, float, float, float]], Awaitable[np.ndarray | None]]

MOTION_ACTIVE = "active"
MOTION_PARKED = "parked"

_PERSON_CLASS_ID = 0

# EMA weight for the confirmed-check anchor update. Small on purpose: absorbs
# lighting drift over minutes-to-hours while a single odd frame can't drag the
# anchor toward "empty".
_EMA_ALPHA = 0.05
# A safety valve on the registry, not a model of how many seats a camera has.
# An anchor lives for its track's whole life plus the hold after it, so the
# population is every anchored-class track alive or dead within the
# corroboration window. Measured over 30 days: patio peaked at 18, door and
# south at 12. The old 6 was a third of the patio's real need.
_MAX_PER_CAMERA = 32
_CAP_LOG_INTERVAL_NS = 60_000_000_000
# A bbox within this fraction of the frame edge never anchors — same margin the
# parked-transition suppression uses (a subject at the edge is entering or
# leaving, and a partial crop's release margin is too thin to trust).
_EDGE_MARGIN_FRAC = 0.015
# Context patch horizontal offset, as a fraction of the anchor's own width.
# Adjacent — near enough to share the illumination event, far enough that the
# person's own pixels (and their departure) don't touch it.
_CTX_SHIFT = 1.15
# Detector hit counts as corroboration when its box overlaps the anchor at
# least this much (loose on purpose — seated boxes jitter).
_CORR_IOU = 0.10
# Re-derive the context patch once the subject has moved off the box it was
# captured around. Generous, so a seated person's jitter never pays for an
# extra OSNet embed; tight enough that a walk across the patio does.
_CTX_REFRESH_IOU = 0.35
# Re-seat: how many candidate detections per check may pay an OSNet crop embed
# when the anchor goes looking for a subject that left its spot.
_RESEAT_MAX_CANDIDATES = 3
# Takeover by GEOMETRY: a live track overlapping the anchor's box this much
# already occupies the spot, so the two cannot be separate presences.
_TAKEOVER_IOU = 0.30
# Takeover by APPEARANCE: a live track this close to the anchor's vector is the
# subject, wherever it stands. Tighter than the hold gate on purpose — it hands
# over identity, so it uses the same bar as the face-anchored body chain
# (measured on the patio: Marko cross-position 0.176-0.238, empty/other ~0.5).
_TAKEOVER_APPEAR = 0.25
# A successor must have LIVED a little before it may take the baton. Norfair
# spawns short-lived duplicate tracks around a moving person, and three times
# in one evening the handover went to a track that died within a second while
# the real one (internally coasting) came back and found its record given
# away — forking the visit. Two seconds filters the mayflies; the anchor's own
# hold bridges the wait, which is its job anyway. first_seen_ns == 0 (unknown
# producer) passes, preserving old behaviour.
_TAKEOVER_MIN_AGE_NS = 2_000_000_000
# A NON-person anchor must be at least this fraction of the frame (larger
# relative dimension). A real parked car fills a good chunk of any camera that
# matters; a 35x19 px phantom "car" in the middle of the patio does not — and
# without the person-only corroboration bound, an anchor on one would sit
# there comparing the static scene to itself for up to the 168 h age cap.
_NONPERSON_ANCHOR_MIN_PCT = 8.0  # 5.0 let a 36x34 px phantom through by HEIGHT (34/576=5.9%)


def _corroborates(class_id: int, group: str) -> bool:
    """Whether a detection of `class_id` counts as "something is still here"
    for a subject of `group`.

    Looser than the baton rule, deliberately, and only in one direction: a
    PET box may corroborate a PERSON. On this property the detector flips a
    seated person to dog or cat thousands of times a day — one patio track
    was stamped `Lumi (pet)` and anchored `class=person` twenty-nine seconds
    apart — so refusing those boxes made the sitter's own detections argue
    that the seat was empty.

    It stays safe because corroboration only says the seat is not empty; WHO
    is in it remains the appearance check's decision, and a dog that really
    does take the chair fails that check and releases the anchor on mismatch.
    The reverse is not allowed: a person box must not prop up a pet anchor,
    which would let anyone walking past hold a dog's presence open.
    """
    cls_group = _class_group(class_id)
    if cls_group == group:
        return True
    return group == "person" and cls_group == "pet"


def _class_group(class_id: int) -> str:
    """Subjects hand the baton only within their own kind: a person anchor may
    follow person detections, a vehicle anchor vehicle ones. Pets pool (the
    detector flips dog/cat), vehicles pool (car/truck/bus/moto)."""
    if class_id == _PERSON_CLASS_ID:
        return "person"
    if class_id in PET_GROUP:
        return "pet"
    if class_id in VEHICLE_GROUP:
        return "vehicle"
    return str(class_id)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _context_bbox(
    bbox: tuple[float, float, float, float], frame_w: int, frame_h: int
) -> tuple[float, float, float, float] | None:
    """Same-size patch horizontally adjacent to the anchor, on whichever side
    has room, clamped to the frame. None when neither side fits (a huge bbox on
    a narrow frame) — the caller then runs without context discrimination."""
    w = bbox[2] - bbox[0]
    if w <= 0:
        return None
    shift = w * _CTX_SHIFT
    right = (bbox[0] + shift, bbox[1], bbox[2] + shift, bbox[3])
    if right[2] <= frame_w:
        return right
    left = (bbox[0] - shift, bbox[1], bbox[2] - shift, bbox[3])
    if left[0] >= 0:
        return left
    return None


@dataclass(slots=True)
class _Anchor:
    bbox: tuple[float, float, float, float]  # pixel coords, detector frame space
    emb: np.ndarray  # L2-normalised OSNet vector of the settled subject's crop
    class_id: int
    class_name: str
    confidence: float  # held (BoxHold high-water) score to keep emitting
    created_ns: int
    state_since_ns: int
    # Identity the held subject carried, if the tracker had stamped one
    # (pets/vehicles — persons are named downstream by face). Carried onto the
    # synthetic wire so a held pet keeps its name instead of going anonymous
    # the moment its track breaks, and handed to whoever takes the anchor over.
    identity_gid: str = ""
    identity_name: str = ""
    ctx_bbox: tuple[float, float, float, float] | None = None
    ctx_emb: np.ndarray | None = None
    last_check_ns: int = 0
    # Last time the hold POSITIVELY verified the subject: a passed appearance
    # check or a successful re-seat. Distinct from the emission tick — a hold
    # keeps the presence ALIVE every tick, but only verification is EVIDENCE.
    # Stamping every hold tick as evidence stretched a visit ~10 s past the
    # subject's real exit (the anchor spends its miss window discovering the
    # departure, and every tick of that window claimed "seen just now"), so
    # the clip rolled on an empty scene — with the operator's post-roll at 0.
    last_verified_ns: int = 0
    misses: int = 0
    # Last time ANY person detection overlapped the anchor (init = creation).
    corroborated_ns: int = 0
    # Perf: only log the hold once per state change, not every tick.
    holding: bool = field(default=False)


@dataclass
class _Tick:
    """What one sweep of one camera sees, shared by every anchor on it."""

    cam: str
    emitted: list[TrackWire]
    crop_embed: CropEmbed | None
    ts_ns: int
    candidate_dets: list[tuple[int, tuple[float, float, float, float]]] | None
    frame_w: int
    frame_h: int
    emb_by_tid: dict[int, np.ndarray]
    anchors: dict[int, _Anchor]


def _corroborated(a: _Anchor, candidate_dets) -> bool:
    group = _class_group(a.class_id)
    return any(
        _corroborates(cls, group) and _iou(db, a.bbox) >= _CORR_IOU
        for cls, db in candidate_dets or ()
    )


def _old_enough(w: TrackWire, ts_ns: int) -> bool:
    return w.first_seen_ns <= 0 or ts_ns - w.first_seen_ns >= _TAKEOVER_MIN_AGE_NS


def _lookalike(a: _Anchor, group: str, tick: _Tick) -> tuple[TrackWire | None, bool]:
    """The live same-group track whose CURRENT OSNet embedding matches the
    anchor, at any position — and whether a matching one is still inside the
    mayfly window. Its existence must pause the release clock: live on the
    patio the anchor released one second before the true successor aged in,
    and the seam split the visit."""
    took: TrackWire | None = None
    best_dist: float | None = None
    pending_young = False
    for w in tick.emitted:
        if _class_group(w.class_id) != group:
            continue
        e = tick.emb_by_tid.get(w.track_id)
        if e is None:
            continue
        w_dist = float(1.0 - np.dot(a.emb, e))
        if w_dist >= _TAKEOVER_APPEAR:
            continue
        if not _old_enough(w, tick.ts_ns):
            pending_young = True
            continue
        if best_dist is None or w_dist < best_dist:
            took, best_dist = w, w_dist
    return took, pending_young


def _overlapper(a: _Anchor, group: str, tick: _Tick) -> TrackWire | None:
    took: TrackWire | None = None
    best_iou = 0.0
    for w in tick.emitted:
        if _class_group(w.class_id) != group or not _old_enough(w, tick.ts_ns):
            continue
        w_iou = _iou((w.x1, w.y1, w.x2, w.y2), a.bbox)
        if w_iou >= _TAKEOVER_IOU and w_iou > best_iou:
            took, best_iou = w, w_iou
    return took


def _successor(a: _Anchor, tick: _Tick) -> tuple[TrackWire | None, bool]:
    """Takeover: the subject is one of the LIVE tracks — the baton goes to
    whoever IS them, not to the first who stands near. At an entrance Norfair
    spawns several short-lived tracks for one walking person, and the one
    overlapping the old spot is often the one about to die (live: the chain
    handed to a track that died in 6 s while the survivor walked on unnamed,
    splitting the visit). So: prefer the appearance match, and fall back to
    best-overlap only when no appearance evidence exists this tick."""
    group = _class_group(a.class_id)
    took, pending_young = _lookalike(a, group, tick)
    if took is None:
        took = _overlapper(a, group, tick)
    return took, pending_young


class AnchorHold:
    """Per-camera registry of appearance anchors. See module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        classes: tuple[int, ...],
        available: bool,
    ) -> None:
        # Thresholds are READ, not copied: the operator edits them in the UI
        # and they take effect on the next tick. Capturing them here is what
        # would make this a setting the pipeline ignores until a restart.
        self._s = settings
        # Not a threshold — the hold IS an appearance comparison, so without
        # OSNet it stays off no matter what the operator sets.
        self._available = available
        self._classes = frozenset(classes)
        # Corroboration applies to PERSONS only: a seated person yields
        # detector hits on ~75% of frames, so its absence is meaningful. A
        # parked car goes fully detector-blind for hours (measured — the very
        # reason it needs the hold), so absence of hits proves nothing there;
        # vehicle/pet releases rely on appearance + takeover (a departing car
        # always MOVES at high confidence).
        # (corroboration is read live — see `_corroboration_ns`)
        # camera_slug → {track_id: _Anchor}
        self._by_cam: dict[str, dict[int, _Anchor]] = {}
        # What a handover owes its successor: (misses, created_ns), keyed by
        # the taker's track id. A handover fires when a live track of the same
        # class stands on the anchor's spot — and on a corner that produces a
        # static false positive, that is the SAME phantom re-detected under a
        # new id. Building the successor's anchor from scratch reset the
        # three-strike release countdown and the age cap with it, so the chain
        # never ended: measured on patio, `anchor miss (1/3)` at 20:26:38 and a
        # handover one second later, over an empty terrace, holding a presence
        # that turned the radio on.
        #
        # Carrying both means three CONSECUTIVE misses end the presence however
        # many ids the phantom cycles through, while a real subject whose
        # detection merely flickers still matches on appearance, never misses,
        # and carries nothing.
        self._carried: dict[str, dict[int, tuple[int, int]]] = {}
        # Counters drained into per-camera telemetry each minute, so this
        # mechanism leaves a record that survives the container being
        # recreated. Everything it does was previously log-only, and a deploy
        # takes the logs with it.
        self._stats: dict[str, dict[str, int]] = {}
        self._cap_logged_ns: dict[str, int] = {}

    # --- live-resolved thresholds ---------------------------------------

    @property
    def _hold_dist(self) -> float:
        return self._s.f("anchor_hold_dist") if self._available else 0.0

    @property
    def _miss_limit(self) -> int:
        return self._s.i("anchor_miss_limit")

    @property
    def _check_interval_ns(self) -> int:
        return int(self._s.f("anchor_check_interval_s") * 1e9)

    @property
    def _max_age_ns(self) -> int:
        return int(self._s.f("anchor_max_age_h") * 3600 * 1e9)

    @property
    def _max_age_nonperson_ns(self) -> int:
        # Vehicles/pets park for DAYS, not hours — separate sanity cap.
        return int(self._s.f("anchor_max_age_nonperson_h") * 3600 * 1e9)

    @property
    def _corroboration_ns(self) -> int:
        return int(self._s.f("anchor_corroboration_min") * 60 * 1e9)

    @property
    def enabled(self) -> bool:
        return self._hold_dist > 0 and bool(self._classes)

    def has(self, cam: str) -> bool:
        return bool(self._by_cam.get(cam))

    def _count(self, cam: str, key: str) -> None:
        self._stats.setdefault(cam, {})[key] = (
            self._stats.setdefault(cam, {}).get(key, 0) + 1
        )

    def drain_stats(self, cam: str) -> dict[str, int]:
        """Counts since the last drain, and reset. Called by the telemetry
        flush; a camera with no anchor activity returns an empty dict."""
        return self._stats.pop(cam, {})

    async def note(
        self,
        cam: str,
        wire: TrackWire,
        emb: np.ndarray | None,
        ts_ns: int,
        frame_w: int,
        frame_h: int,
        crop_embed: CropEmbed | None,
        *,
        arrived: bool = False,
    ) -> None:
        """Observe one EMITTED track this tick — creates the anchor on first
        sight and follows it thereafter. While the track is alive the anchor is
        dormant (sweep() skips live ids), so it costs nothing and is simply
        ready the moment the track breaks."""
        if not self.enabled or wire.class_id not in self._classes:
            return
        anchors = self._by_cam.setdefault(cam, {})
        tid = wire.track_id
        # Arm from the FIRST tick this track is emitted — not once it settles.
        #
        # This used to require `motion_state != ACTIVE`, and that gate was a
        # circular dependency: a track is only called `stationary` after it has
        # survived the stillness window (8 s), so the hold that exists to
        # survive a broken track could only arm from a track that had already
        # proved it would not break. Measured on the patio: person tracks
        # finalise at 4.5 s / 6.9 s / 14.0 s — two of three die inside the
        # window, and the log carries not one `anchor set`. The protection was
        # never once switched on for the camera it was written for.
        #
        # Arming early is free: `sweep()` skips any anchor whose track id is in
        # this tick's emissions ("real track alive → anchor dormant"), so an
        # anchor on a live track emits nothing at all. It just sits there,
        # following the subject, so that the instant the track dies there is
        # already a fresh appearance and a spot to hold. And a subject who
        # walks off is released by evidence, not by timing: corroboration
        # (person-only, 90 s of zero detections at the spot), the appearance
        # check, and takeover all still apply.
        # `arrived`, not the wire's `ever_moved`. Both ask "did this turn up
        # under its own power, or is it furniture the detector hallucinates a
        # person onto" — but `ever_moved` demands a FULL bbox diagonal of
        # travel because it also gates the phantom registry, where one false
        # mover kills a spot forever. A person who crosses to a chair and sits
        # gets a 5-7 s track that never banks that much, so the anchor kept
        # refusing the very subject it exists for. `arrived` asks the same
        # question at a third of the distance — still two orders of magnitude
        # above furniture, whose box repeats at identical coordinates for
        # minutes — and an anchor armed in error is bounded by appearance,
        # takeover and corroboration, which a dead registry spot is not.
        if not arrived or emb is None:
            return
        bbox = (wire.x1, wire.y1, wire.x2, wire.y2)
        if wire.class_id != _PERSON_CLASS_ID and frame_w > 0 and frame_h > 0:
            # Same helper the detector's gate uses — the two thresholds live in
            # the same units, so they must not drift on separately-maintained
            # arithmetic.
            pct = gate_size_pct(bbox[2] - bbox[0], bbox[3] - bbox[1], frame_w, frame_h)
            if pct < _NONPERSON_ANCHOR_MIN_PCT:
                return  # phantom-scale vehicle/pet — not worth holding
        if frame_w > 0 and frame_h > 0:
            mx, my = frame_w * _EDGE_MARGIN_FRAC, frame_h * _EDGE_MARGIN_FRAC
            if (
                bbox[0] <= mx
                or bbox[1] <= my
                or bbox[2] >= frame_w - mx
                or bbox[3] >= frame_h - my
            ):
                # Settled AT the frame edge = entering/leaving, not sitting —
                # and a partial edge crop's release margin is too thin (live:
                # door read 0.325 vs the 0.30 gate). No anchor here.
                return
        cur = anchors.get(tid)
        if cur is None:
            if len(anchors) >= _MAX_PER_CAMERA:
                # A full registry refuses the newcomer; it never evicts. The
                # newcomer is a live track, carrying its own presence right
                # now. Every anchor on a live track is re-confirmed each tick,
                # so any "weakest" ranking picks one whose track has already
                # died — the one presence nothing else is carrying. Measured
                # 12.09 with nine people on the patio: 2990 evictions in eight
                # hours, the same track re-armed 389 times, and the radio
                # silenced by the eviction of whoever sat still.
                self._count(cam, "refused")
                if ts_ns - self._cap_logged_ns.get(cam, 0) >= _CAP_LOG_INTERVAL_NS:
                    self._cap_logged_ns[cam] = ts_ns
                    log.warning(
                        "anchor registry full cam=%s (%d) — track=%s not armed",
                        cam, _MAX_PER_CAMERA, tid,
                    )
                return
            ctx_bbox = (
                _context_bbox(bbox, frame_w, frame_h)
                if frame_w > 0 and frame_h > 0
                else None
            )
            ctx_emb = await crop_embed(ctx_bbox) if (ctx_bbox and crop_embed) else None
            carried_misses, carried_created = self._carried.get(cam, {}).pop(
                tid, (0, 0)
            )
            anchors[tid] = _Anchor(
                bbox=bbox,
                emb=emb,
                class_id=wire.class_id,
                class_name=wire.class_name,
                confidence=wire.confidence,
                created_ns=carried_created or ts_ns,
                state_since_ns=wire.state_since_ns or ts_ns,
                identity_gid=wire.identity_gid,
                identity_name=wire.identity_name,
                ctx_bbox=ctx_bbox,
                ctx_emb=ctx_emb,
                corroborated_ns=ts_ns,
                last_verified_ns=ts_ns,
                misses=carried_misses,
            )
            log.info(
                "anchor set cam=%s track=%s class=%s bbox=(%.0f,%.0f,%.0f,%.0f) "
                "ctx=%s%s",
                cam, tid, wire.class_name, *bbox,
                "yes" if ctx_emb is not None else "no",
                f" (carries {carried_misses}/{self._miss_limit} misses)"
                if carried_misses else "",
            )
        else:
            # Track alive: follow it, refresh the appearance, keep the strongest
            # score. Detections on the live track are corroboration by
            # definition.
            moved_far = _iou(bbox, cur.bbox) < _CTX_REFRESH_IOU
            cur.bbox = bbox
            n = cur.emb * (1.0 - _EMA_ALPHA) + emb * _EMA_ALPHA
            norm = float(np.linalg.norm(n))
            if norm > 0:
                cur.emb = n / norm
            cur.confidence = max(cur.confidence, wire.confidence)
            cur.misses = 0
            cur.corroborated_ns = ts_ns
            # Identity can land after the anchor was armed (the stamp needs a
            # reference match); never unset it once known.
            if wire.identity_gid:
                cur.identity_gid = wire.identity_gid
                cur.identity_name = wire.identity_name
            # The context patch is what tells "he left" apart from "the whole
            # scene changed" (IR flip). Now that an anchor can be armed while
            # the subject is still walking, its neighbourhood moves with it —
            # a patch left behind at the doorway would be comparing furniture
            # the subject is no longer anywhere near. Re-derive it when the
            # subject has genuinely moved, not on every tick (each one costs an
            # OSNet crop embed).
            if moved_far and frame_w > 0 and frame_h > 0 and crop_embed:
                # `_context_bbox` returns None when neither neighbour fits —
                # a box wider than a third of the frame and centred has no
                # room either side. The two sites that CREATE an anchor guard
                # for that; these two, which re-derive it, did not, and handed
                # None to a function whose first statement unpacks it. The
                # exception left the anchor-hold call and was swallowed by the
                # handler's blanket except, discarding the whole tick for that
                # camera — every tick, for as long as the box stayed that wide.
                cur.ctx_bbox = _context_bbox(bbox, frame_w, frame_h)
                cur.ctx_emb = (
                    await crop_embed(cur.ctx_bbox) if cur.ctx_bbox is not None else None
                )
            # "Parked since" should mean what it says: adopt the real state
            # clock once the subject actually settles, rather than reporting
            # the moment they walked in.
            if wire.motion_state != MOTION_ACTIVE and wire.state_since_ns:
                cur.state_since_ns = wire.state_since_ns

    async def sweep(
        self,
        cam: str,
        emitted: list[TrackWire],
        crop_embed: CropEmbed | None,
        ts_ns: int,
        candidate_dets: list[tuple[int, tuple[float, float, float, float]]] | None = None,
        frame_w: int = 0,
        frame_h: int = 0,
        emb_by_tid: dict[int, np.ndarray] | None = None,
        is_ignored: Callable[[tuple[float, float, float, float]], bool] | None = None,
    ) -> tuple[list[TrackWire], dict[int, tuple[int, str, str]]]:
        """Produce synthetic hold wires for anchors whose track is gone.

        `emitted` is this tick's real output (used for dormancy + takeover).
        `crop_embed` embeds a bbox crop of the CURRENT frame, or None when the
        frame is unavailable this tick — then the verdict is simply carried
        (a missing frame is not evidence of an empty seat). `candidate_dets`
        are THIS tick's published detections of the anchored classes, with
        their class id — corroboration that something of the subject's kind is
        still at the spot, and the pool the re-seat searches when it is not."""
        anchors = self._by_cam.get(cam)
        if not anchors:
            return [], {}
        tick = _Tick(
            cam, emitted, crop_embed, ts_ns, candidate_dets, frame_w, frame_h,
            emb_by_tid or {}, anchors,
        )
        live_ids = {w.track_id for w in emitted}
        out: list[TrackWire] = []
        # new_track_id → (predecessor_id, identity_gid, identity_name)
        continuations: dict[int, tuple[int, str, str]] = {}
        for tid in list(anchors):
            a = anchors[tid]
            # Operator-declared dead ground kills anchors RETROACTIVELY. The
            # per-tick emission gate silences the live track the moment an
            # ignore zone lands on it — and that very silence used to flip its
            # anchor into holding: the operator drew a zone to kill a phantom
            # and thereby handed it to the one mechanism that never consulted
            # zones (live: the patio fan, "motorcycle", held at dist 0.008
            # right after the zone was created to suppress it).
            if is_ignored is not None and is_ignored(a.bbox):
                log.info(
                    "anchor released cam=%s track=%s (inside an ignore zone)",
                    cam, tid,
                )
                del anchors[tid]
                continue
            if _corroborated(a, candidate_dets):
                a.corroborated_ns = ts_ns
            if tid in live_ids:
                a.misses = 0
                a.holding = False
                continue  # real track alive → anchor dormant
            took, pending_young = _successor(a, tick)
            if took is not None:
                continuations[took.track_id] = self._hand_over(cam, tid, a, took)
                del anchors[tid]
            elif await self._holds(tick, tid, a, pending_young):
                out.append(self._hold_wire(a, tid, ts_ns))
            else:
                del anchors[tid]
        if out:
            self._count(cam, "held")
        return out, continuations

    def _hand_over(
        self, cam: str, tid: int, a: _Anchor, took: TrackWire
    ) -> tuple[int, str, str]:
        """Hand over what the anchor knows instead of stepping aside in
        silence. It has been watching this exact spot and just found a live
        subject of the same class standing on it, so the two ids are one
        presence — the seam is a fact here, not a guess for re-ID to
        reconstruct downstream."""
        self._carried.setdefault(cam, {})[took.track_id] = (a.misses, a.created_ns)
        log.info(
            "anchor handover cam=%s track=%s → %s%s (same subject, "
            "presence continues)",
            cam, tid, took.track_id,
            f" as {a.identity_name}" if a.identity_name else "",
        )
        return tid, a.identity_gid, a.identity_name

    async def _holds(self, tick: _Tick, tid: int, a: _Anchor, pending_young: bool) -> bool:
        """Whether an anchor with no live track and no taker keeps holding on
        this tick; False releases it."""
        age_cap = (
            self._max_age_ns
            if a.class_id == _PERSON_CLASS_ID
            else self._max_age_nonperson_ns
        )
        if tick.ts_ns - a.created_ns > age_cap:
            log.warning(
                "anchor released cam=%s track=%s (age cap %.1fh — sanity valve)",
                tick.cam, tid, age_cap / 3.6e12,
            )
            return False
        if (
            a.class_id == _PERSON_CLASS_ID
            and self._corroboration_ns > 0
            and tick.ts_ns - a.corroborated_ns > self._corroboration_ns
        ):
            return await self._found_elsewhere(tick, tid, a)
        if tick.ts_ns - a.last_check_ns < self._check_interval_ns:
            return True
        a.last_check_ns = tick.ts_ns
        return await self._check_spot(tick, tid, a, pending_young)

    async def _found_elsewhere(self, tick: _Tick, tid: int, a: _Anchor) -> bool:
        """A real seated person yields detector hits on most frames; this long
        with ZERO person detections AT THE SPOT means the spot is empty — even
        if the appearance gate never fired (thin margin, or a re-baseline that
        landed on an empty seat).

        But an empty SPOT is not an empty CAMERA, and this rule used to
        conflate them. The anchor's box is frozen where the subject last sat,
        so somebody who moves one chair along stops corroborating it while
        remaining in plain view — measured on the patio: the appearance check
        passed at every tick for the full ninety seconds (dist 0.206) while
        the detector published more than one person box per frame, and the
        hold was killed anyway with a log line claiming the spot was empty.
        Presence died with the subject sitting there.

        So before believing it, look where the subject actually is."""
        if await self._reseat(tick, tid, a):
            a.corroborated_ns = tick.ts_ns
            return True
        log.info(
            "anchor released cam=%s track=%s (no detector corroboration "
            "for %.0fs, and nobody of its kind is anywhere on this "
            "camera)", tick.cam, tid, self._corroboration_ns / 1e9,
        )
        self._count(tick.cam, "released")
        self._count(tick.cam, "uncorroborated")
        return False

    async def _check_spot(
        self, tick: _Tick, tid: int, a: _Anchor, pending_young: bool
    ) -> bool:
        crop_embed = tick.crop_embed
        # Late context capture: the frame can be missing on the tick the
        # anchor was created (SHM lookup miss), and without a retry that
        # anchor spent its whole life with no global-change discrimination
        # (observed live: `ctx=no`).
        if a.ctx_emb is None and a.ctx_bbox is not None and crop_embed:
            a.ctx_emb = await crop_embed(a.ctx_bbox)
        cur = await crop_embed(a.bbox) if crop_embed is not None else None
        if cur is None:
            return True  # frame unavailable → carry the verdict, no miss counted
        dist = float(1.0 - np.dot(a.emb, cur))
        if dist < self._hold_dist:
            self._verified(tick, tid, a, cur, dist)
            return True
        if await self._rebaselined(tick, tid, a, cur, dist):
            return True
        if pending_young:
            # The subject's successor is seconds from qualifying — carry the
            # hold instead of letting the release clock outrun the mayfly
            # window.
            return True
        # The spot is empty — but "not at his spot" is not "gone from the
        # camera". Before counting the miss, look for the SUBJECT among this
        # tick's published detections: the baton is the appearance vector, and
        # it can be handed to a new position, not just a new track id. This is
        # what was missing when a track died mid-walk between two seats — the
        # anchor sat at the old seat, read it empty, and gave up while the
        # subject was ten steps away.
        if await self._reseat(tick, tid, a):
            return True
        a.misses += 1
        log.info(
            "anchor miss cam=%s track=%s dist=%.3f (%d/%d)",
            tick.cam, tid, dist, a.misses, self._miss_limit,
        )
        if a.misses < self._miss_limit:
            return True
        log.info("anchor released cam=%s track=%s (spot reads empty)", tick.cam, tid)
        self._count(tick.cam, "released")
        return False

    def _verified(self, tick: _Tick, tid: int, a: _Anchor, cur: np.ndarray, dist: float) -> None:
        a.misses = 0
        a.last_verified_ns = tick.ts_ns
        n = a.emb * (1.0 - _EMA_ALPHA) + cur * _EMA_ALPHA
        norm = float(np.linalg.norm(n))
        if norm > 0:
            a.emb = n / norm
        if not a.holding:
            a.holding = True
            log.info(
                "anchor holding cam=%s track=%s (dist=%.3f) — presence "
                "carried by appearance", tick.cam, tid, dist,
            )

    async def _rebaselined(
        self, tick: _Tick, tid: int, a: _Anchor, cur: np.ndarray, dist: float
    ) -> bool:
        """Spot mismatch. Person left — or the whole scene changed (IR
        cut-filter flip: measured 0.51 on an occupied crop). The CONTEXT patch
        tells them apart: a departure leaves the neighbouring furniture
        alone."""
        if a.ctx_emb is None or a.ctx_bbox is None or not tick.crop_embed:
            return False
        ctx_cur = await tick.crop_embed(a.ctx_bbox)
        if ctx_cur is None:
            return False
        ctx_dist = float(1.0 - np.dot(a.ctx_emb, ctx_cur))
        if ctx_dist < self._hold_dist:
            return False
        a.emb = cur
        a.ctx_emb = ctx_cur
        a.misses = 0
        log.info(
            "anchor re-baselined cam=%s track=%s (global scene change: "
            "spot=%.3f ctx=%.3f — IR flip or similar; corroboration window "
            "bounds a wrong re-baseline)", tick.cam, tid, dist, ctx_dist,
        )
        return True

    @staticmethod
    def _hold_wire(a: _Anchor, tid: int, ts_ns: int) -> TrackWire:
        """The synthetic wire for one held anchor, at its CURRENT bbox — after
        a re-seat that is the subject's new position, so zone occupancy (the
        heating zone under the new chair) follows them without a live track."""
        return TrackWire(
            track_id=tid,
            x1=a.bbox[0],
            y1=a.bbox[1],
            x2=a.bbox[2],
            y2=a.bbox[3],
            class_id=a.class_id,
            class_name=a.class_name,
            confidence=a.confidence,
            motion_state=MOTION_PARKED,
            state_since_ns=a.state_since_ns,
            ever_moved=True,
            # A hold keeps the presence ALIVE; what counts as EVIDENCE depends
            # on the class, because the failure modes differ:
            #
            # PERSON — evidence is detector corroboration ONLY. A seated
            # person yields detector hits on most frames, so evidence tracks
            # real presence tightly; and at dusk/night the appearance check
            # FALSE-PASSES on an empty spot (measured: a doorway kept reading
            # <0.30 for the full 90 s corroboration bound after the subject
            # left — separations collapse in low light), so counting those
            # passes as evidence stretched the visit's end ~15 s past the real
            # exit. A wrong look must not write history.
            #
            # NON-PERSON — appearance verification counts too: a parked car
            # goes fully detector-blind for hours (the reason the hold exists),
            # so corroboration alone would freeze its visit at the last
            # detection hours before it actually drives off.
            last_matched_ns=(
                a.corroborated_ns
                if a.class_id == _PERSON_CLASS_ID
                else max(a.corroborated_ns, a.last_verified_ns)
            ),
            identity_gid=a.identity_gid,
            identity_name=a.identity_name,
        )

    async def _reseat(self, tick: _Tick, tid: int, a: _Anchor) -> bool:
        """Move the anchor to the subject's new position, if the subject is
        findable on this camera right now.

        The candidates are this tick's published detections of the anchor's
        class group that nobody owns: not overlapping a live track (a live
        track owns its subject — that seam is the takeover's job), not
        overlapping another anchor, and not touching the frame edge (an edge
        box is someone LEAVING — following it would hold a presence that is
        about to be gone, and release-on-exit is exactly what the operator
        asked for). Each candidate costs an OSNet crop embed, so only the
        largest few are tried; the appearance gate is the same `hold_dist`
        the spot check uses — measured on this camera: same person ~0.14,
        empty/other ~0.52, so the gate that verifies a seat also verifies a
        move."""
        crop_embed, frame_w, frame_h = tick.crop_embed, tick.frame_w, tick.frame_h
        if not tick.candidate_dets or crop_embed is None:
            return False
        group = _class_group(a.class_id)
        mx = frame_w * _EDGE_MARGIN_FRAC if frame_w > 0 else 0.0
        my = frame_h * _EDGE_MARGIN_FRAC if frame_h > 0 else 0.0
        cands: list[tuple[float, float, float, float]] = []
        for cls, db in tick.candidate_dets:
            if _class_group(cls) != group:
                continue
            if _iou(db, a.bbox) >= _CORR_IOU:
                continue  # the spot itself — appearance already judged it
            if frame_w > 0 and (
                db[0] <= mx or db[1] <= my or db[2] >= frame_w - mx or db[3] >= frame_h - my
            ):
                continue
            if any(
                _iou(db, (w.x1, w.y1, w.x2, w.y2)) >= _TAKEOVER_IOU for w in tick.emitted
            ):
                continue
            if any(o is not a and _iou(db, o.bbox) >= _TAKEOVER_IOU for o in tick.anchors.values()):
                continue
            cands.append(db)
        # Largest first: the nearest, most reliable crops.
        cands.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        best: tuple[float, tuple[float, float, float, float], np.ndarray] | None = None
        for db in cands[:_RESEAT_MAX_CANDIDATES]:
            e = await crop_embed(db)
            if e is None:
                continue
            dist = float(1.0 - np.dot(a.emb, e))
            if dist < self._hold_dist and (best is None or dist < best[0]):
                best = (dist, db, e)
        if best is None:
            return False
        dist, db, e = best
        old = a.bbox
        a.bbox = db
        # Heavy blend toward the new view — apparent scale and angle change
        # with the move, and the next spot checks compare against THIS look.
        n = a.emb * 0.5 + e * 0.5
        norm = float(np.linalg.norm(n))
        if norm > 0:
            a.emb = n / norm
        a.misses = 0
        a.corroborated_ns = tick.ts_ns
        a.last_verified_ns = tick.ts_ns
        if frame_w > 0 and frame_h > 0:
            a.ctx_bbox = _context_bbox(db, frame_w, frame_h)
            a.ctx_emb = (
                await crop_embed(a.ctx_bbox)
                if a.ctx_bbox is not None and crop_embed
                else None
            )
        log.info(
            "anchor re-seated cam=%s track=%s dist=%.3f (%.0f,%.0f)→(%.0f,%.0f) — "
            "subject moved, presence follows",
            tick.cam, tid, dist, old[0], old[1], db[0], db[1],
        )
        return True

    def forget_camera(self, cam: str) -> None:
        self._carried.pop(cam, None)
        self._cap_logged_ns.pop(cam, None)
        if self._by_cam.pop(cam, None):
            log.info("anchors cleared for camera=%s", cam)


__all__ = ["AnchorHold"]
