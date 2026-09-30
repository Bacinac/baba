"""NATS wire formats shared by the pipeline services.

One canonical msgspec Struct per message type. These used to be mirrored
by hand in every consumer ("mirrors the tracker publisher") — a schema
change then had to be repeated in up to four files, and a missed copy
produced runtime AttributeErrors (the frame_width incident recorded in the
old event-manager `_wire.py`). All BABA images are built from this repo
and redeployed together, so a single shared module is safe and the copies
were pure risk.

Encoding is msgpack (map-form structs → decoders ignore unknown fields,
so adding a defaulted field stays backward-compatible within a rollout).

Subjects:
    baba.frames.<slug>       FrameMessage        (ingestor → detector)
    baba.detections.<slug>   DetectionsMessage   (detector → tracker, sse)
    baba.tracks.<slug>       TracksMessage       (tracker → embedder,
                                                  event-manager, sse)
    baba.activity.<slug>     ActivityMessage     (tracker → ingestor)
"""

from __future__ import annotations

import msgspec

SUBJECT_FRAMES_TEMPLATE = "baba.frames.{camera_id}"
SUBJECT_DETECTIONS_TEMPLATE = "baba.detections.{camera_id}"
SUBJECT_TRACKS_TEMPLATE = "baba.tracks.{camera_id}"
SUBJECT_ACTIVITY_TEMPLATE = "baba.activity.{camera_id}"
# Per-camera minute telemetry, {source} ∈ detector|tracker|ingestor. The
# event-manager's TelemetrySink persists these into camera_telemetry — the
# continuous "flight recorder" that scene analysis (and the operator's "why
# did the rate go up?" question) reads instead of reconstructing evidence
# from logs after the fact.
SUBJECT_TELEMETRY_TEMPLATE = "baba.telemetry.{source}.{camera_id}"
SUBJECT_TELEMETRY_WILDCARD = "baba.telemetry.>"
# Request/reply between services lives under `baba.rpc.>`, which no peer account
# is granted. A mirror reads `baba.state.*`; the scene-state RPC once sat there,
# and the mirror could read its requests and answer them before the evaluator.
#
# Request/reply: hand back the detector's rolling PRE-GATE detections for a
# camera and time window. The api uses it to replay an incident against both
# the live thresholds and the ones the AI proposes, so the operator decides
# from evidence rather than from a paragraph of reasoning.
SUBJECT_DETECTOR_TRACE = "baba.rpc.detector.trace"
# Request/reply to the state-evaluator, which alone reads the frame ring: store
# a scene-state prototype from the live frame or a recorded moment, or classify
# a region right now without committing.
SUBJECT_STATE_CAPTURE = "baba.rpc.state.capture"
SUBJECT_STATE_EVAL = "baba.rpc.state.eval"


class FrameMessage(msgspec.Struct, frozen=True):
    """Frame-ready notification. The pixels live in the per-camera POSIX
    shm ring (baba_core.frame_ring); consumers fetch by sequence number,
    so this message is pure metadata.

    `timestamp_ns` is publish wall-clock — useful for ordering and
    stale-frame drops. `pts_ns` is the source decoder's presentation
    timestamp, propagated end-to-end; 0 when the source has no PTS.
    Width/height stay in the message so a consumer that hasn't attached
    to the ring yet can sanity-check without touching shm."""

    camera_id: str
    sequence: int
    timestamp_ns: int
    width: int
    height: int
    pts_ns: int = 0


class DetectionWire(msgspec.Struct, frozen=True):
    x1: float
    y1: float
    x2: float
    y2: float
    class_id: int
    class_name: str
    confidence: float
    # Two-threshold (ByteTrack-style) tracking. True when this detection's
    # confidence cleared the per-camera BIRTH threshold (the old min_conf), so
    # the tracker MAY start a new track from it. Detections below birth but
    # above the maintain floor arrive with False: they can only KEEP an already
    # birth-qualified track alive (a seated person / parked car whose score
    # decayed), never spawn one. Default True so a detector that predates this
    # field stays fully birth-capable (backward-compatible map-encoded add).
    birth_eligible: bool = True


class DetectionsMessage(msgspec.Struct, frozen=True):
    camera_id: str
    sequence: int
    timestamp_ns: int
    # Frame dimensions the detection coords are expressed in. After ingestor
    # downscaling and detector letterbox-undo, bbox coords sit in this space.
    # Consumers (UI overlay, tracker) use these to map to display dimensions.
    frame_width: int
    frame_height: int
    detections: list[DetectionWire]
    # Source decoder PTS of the frame that produced these detections;
    # 0 when the source has no PTS.
    pts_ns: int = 0


class TrackWire(msgspec.Struct, frozen=True):
    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    class_id: int
    class_name: str
    confidence: float
    # Motion state from the tracker ("active" | "stationary" | "parked").
    # Embedder throttles stationary/parked tracks to a drift-check cadence;
    # event-manager emits object_parked/object_unparked on transitions.
    motion_state: str = "active"
    state_since_ns: int = 0
    # Provenance: this track (or the parked-ghost chain it was born from)
    # travelled at least its own bbox diagonal while alive — a REAL subject
    # that arrived on camera, not a static-clutter phantom flickering in
    # place. Event-manager keeps zone occupancy alive for parked tracks
    # carrying this flag, so a seated person's presence survives track
    # churn while phantom respawns stay silent.
    ever_moved: bool = False
    # When the subject FIRST appeared — the timestamp of the earliest detection
    # that formed this Norfair object, captured while it was still
    # INITIALISING (before it earned an id and cleared the birth gate). A
    # track's own start is several seconds late by construction: the birth gate
    # wants consecutive confident frames while the camera is still ramping up
    # from idle fps, so anchoring a clip on it drops the operator mid-scene,
    # already deep into the frame. This is the real entrance, no guessing and
    # no fixed pre-roll fudge. 0 when unknown (producer predates the field).
    first_seen_ns: int = 0
    # Last tick this track had EVIDENCE it is still there: a real detection
    # match, or (for an appearance-anchored hold) a confirmed look at its spot.
    # A track keeps being emitted for a while past its last match — Norfair
    # coasts on Kalman prediction until the emission staleness cutoff — and
    # counting those frames as presence stretched a visit ~10 s past the
    # subject's exit, so a clip kept rolling on an empty scene. Consumers
    # should end a visit HERE, not at the last emission. 0 when unknown.
    last_matched_ns: int = 0
    # Identity stamped AT THE SOURCE by the tracker (identity_stamp.py), for
    # NON-PERSON subjects only: the tracker already computes an OSNet embedding
    # per detection, and enrolled reference photos live in the same OSNet
    # space, so a pet/vehicle is named within ~a second of entering the frame
    # and the name rides the track for its whole life (anchor holds included).
    # Empty = unresolved or person (persons are named by FACE downstream —
    # body-naming people is the over-merge vortex, deliberately excluded).
    # gid is the identity_labels.global_id (stable key), name is display-only.
    identity_gid: str = ""
    identity_name: str = ""
    # This track is the SAME SUBJECT as a track that just ended, under a new
    # id. Set by the appearance hold when a live track takes over an anchor:
    # the hold has been watching that exact spot and just matched the new
    # arrival against the vector of the subject it was holding, so the two ids
    # are one presence — a fact the tracker PROVED, not a guess.
    #
    # Without it the seam is invisible. A person whose track breaks while they
    # sit reappears as a brand-new anonymous entity: Activity has to re-derive
    # the join through global_id re-ID (which can miss, especially with a back
    # turned to the camera), the live zone occupancy sees an arrival, and a
    # face-recognised name is lost until a face is caught again. 0 = no known
    # predecessor.
    continues_track_id: int = 0


class ClassWindowStats(msgspec.Struct, frozen=True):
    """One class's published-detection aggregate over a telemetry window."""

    n: int
    conf_p50: float
    conf_p95: float
    # Size = max(bbox_w/frame_w, bbox_h/frame_h) in %, same metric the
    # min_box_pct rule uses, so a threshold suggestion maps 1:1.
    size_p50: float
    size_p95: float
    # Of the `n` published, how many cleared the per-camera BIRTH threshold
    # (min_confidence) and could therefore START a track. `n - n_birth` are
    # maintain-only: they keep an existing track alive but can never spawn one
    # (see two-threshold tracking). WITHOUT this split the tuning evidence
    # lies: since the detector now publishes down to the maintain floor, `n`
    # and the conf quantiles include below-birth boxes, and `dropped.conf`
    # only counts below-MAINTAIN — so an over-tight birth threshold no longer
    # shows up as a drop spike. `n_birth == 0` while `n` is high IS the new
    # "silently blinded" fingerprint: the class is seen every frame but
    # nothing can ever start a track from it.
    n_birth: int = 0


class DroppedWindowStats(msgspec.Struct, frozen=True):
    """Detections the rules filtered in a window, by reason. A sustained
    spike here is the 'silently blinded' early-warning."""

    conf: int = 0
    size: int = 0
    disabled: int = 0


class DetectorTelemetry(msgspec.Struct, frozen=True):
    camera_id: str
    window_s: float
    published: dict[str, ClassWindowStats]
    dropped: dict[str, DroppedWindowStats]


class TrackerTelemetry(msgspec.Struct, frozen=True):
    camera_id: str
    window_s: float
    births: int
    # Class-stabilizer corrections (reported class != detected class) —
    # a flip-storm fingerprint.
    flips: int
    # Snapshot at flush time.
    n_active: int
    n_stationary: int
    n_parked: int
    # The appearance hold, which is what carries a seated person's presence
    # once their track dies. It logged everything and recorded nothing, so a
    # deploy — which recreates the container — erased the only account of the
    # mechanism the operator complains about. Additive with defaults: an older
    # consumer decodes a newer message unchanged.
    anchors_held: int = 0
    anchors_released: int = 0
    # Released on the corroboration bound specifically: the rule that ends a
    # hold the appearance check was still passing. It should now be rare, and
    # a camera where it is not is a camera to go and look at.
    anchors_released_uncorroborated: int = 0
    # Tracks refused an anchor because the camera's registry was full. Should
    # stay zero; anything else means the valve is sized below the scene.
    anchors_refused: int = 0


class IngestorTelemetry(msgspec.Struct, frozen=True):
    camera_id: str
    window_s: float
    active_s: float
    idle_s: float
    transitions: int
    frames_out: int
    frames_dropped_idle: int
    # Scene light, measured from the decoded frames themselves (~6 samples
    # per minute) — cameras only report their CONFIGURED day/night mode over
    # ONVIF/vendor APIs, not the actual current state, so we don't ask them.
    # luma_avg: mean Y (0-255) = scene brightness. chroma_dev_avg: mean |UV
    # - 128|; near zero = monochrome picture = IR-cut filter open (night IR
    # mode). ir_ratio: fraction of window samples judged IR. -1 = no frames
    # sampled this window.
    luma_avg: float = -1.0
    chroma_dev_avg: float = -1.0
    ir_ratio: float = -1.0


class ActivityMessage(msgspec.Struct, frozen=True):
    """Per-tick scene-activity verdict, tracker → ingestor.

    Drives the ingestor's adaptive frame rate (cameras.idle_fps): `active`
    means "something is worth watching at full rate" — any track that is not
    parked, or a freshly-spawned (still initializing) Norfair tracker, which
    fires on the very first detection of a new object so the ramp-up happens
    within one idle-frame interval. A scene holding only parked objects (or
    nothing at all) reports inactive and the ingestor decays to idle_fps
    after its hold window. The ingestor treats a STALE feed (tracker down,
    pipeline stalled) as active — degrading detection rate must never be the
    silent failure mode."""

    camera_id: str
    active: bool
    timestamp_ns: int
    # Diagnostics for logs/stats — why the verdict is what it is.
    n_tracks: int = 0
    n_initializing: int = 0
    n_parked: int = 0


class TracksMessage(msgspec.Struct, frozen=True):
    camera_id: str  # slug, not uuid
    sequence: int
    timestamp_ns: int
    tracks: list[TrackWire]
    pts_ns: int = 0
    # Picture dimensions the bbox coords are expressed in — needed by the
    # event-manager's zone-polygon tests to normalise points to [0, 1].
    frame_width: int = 0
    frame_height: int = 0
    # Tracker generation. Norfair numbers track ids from a low value, so after
    # the tracker restarts (or recreates a camera's Norfair instance on a
    # settings change) a brand-new subject inherits a track_id a previous
    # subject held. `epoch` changes with every such (re)creation and strictly
    # increases across restarts, so a consumer can tell a reused id apart from
    # a continuation instead of merging two different subjects. 0 = a producer
    # that predates the field (treat as "unknown / don't distinguish").
    epoch: int = 0
