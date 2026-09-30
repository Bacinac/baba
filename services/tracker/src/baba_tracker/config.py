from __future__ import annotations

import os
from dataclasses import dataclass

from baba_core import dsn_from_env


@dataclass(slots=True, frozen=True)
class TrackerConfig:
    nats_url: str
    # Postgres DSN — per-camera stillness settings + enabled flags load from
    # the cameras table (live via cameras_changed NOTIFY).
    dsn: str
    # Seconds a track survives without a matching detection before it is
    # dropped (Norfair hit_counter decay). Expressed in TIME, not frames:
    # with adaptive per-camera frame rates (cameras.idle_fps) a frame count
    # would mean 15 s at idle and 2 s at full rate — the tracker converts
    # seconds → nominal frames internally and replays skipped ticks so decay
    # is wall-clock-correct at any rate.
    lost_track_seconds: float
    # NOMINAL tick rate for Norfair's frame-based counters — the internal
    # scale that seconds-based knobs are converted to, NOT an assumption
    # about the actual per-camera processing rate (that varies per camera
    # and, with adaptive fps, over time). Only affects counter granularity.
    frame_rate: int
    # Only track these COCO class ids (default: person, bicycle, car, motorcycle,
    # bus, truck — typical NVR objects). Set to empty to track all.
    class_id_filter: tuple[int, ...]
    # Motion-state thresholds. The tracker promotes a track from `active`
    # to `stationary` when its bbox centre has stayed within an
    # OBJECT-RELATIVE stillness radius for `static_window_ms`, and from
    # `stationary` to `parked` after `park_threshold_ms` of continued
    # stillness. A `2× radius` displacement bumps it back to `active`
    # (hysteresis so a single noisy frame doesn't flap the state).
    # The radius is `static_move_ratio × bbox_diagonal` (floored at
    # `static_move_min_px`): a fixed pixel threshold is far too tight for
    # a 400px car and far too loose for a 60px distant one, and it shifts
    # with each camera's resolution/downscale. Scaling to the object's own
    # size makes "is it still?" resolution-independent — the same principle
    # the detector's static-object suppression uses. Embedder honours these
    # to skip work on parked objects.
    static_move_ratio: float
    static_move_min_px: float
    static_window_ms: int
    park_threshold_ms: int
    # OSNet x0_25 ReID model path; empty disables inline ReID and the
    # tracker falls back to IoU-only association (Norfair without
    # `reid_distance_function`). See scripts/fetch_tracker_models.sh
    # for the download. ~1 MB ONNX, 256×128 input, 512-dim output.
    reid_model_path: str
    # ReID similarity gate. Norfair calls `reid_distance_function(matched,
    # unmatched)` and merges when the returned distance is below this.
    # For L2-normalised OSNet outputs the cosine distance lands at
    # ~0.10–0.25 for the same subject across short occlusions, 0.35+
    # for a different subject. 0.30 is the conservative default.
    reid_distance_threshold: float
    # Seconds a "dead" track is kept around waiting for a ReID re-match
    # before being discarded for good. Big enough to bridge a person
    # walking behind a parked car or a tree.
    reid_lost_seconds: float
    # When True, a missing/broken OSNet model is a fatal startup error rather
    # than a silent downgrade to IoU-only association. On a production
    # (nvidia/intel) build, a `/models` remount that drops the ONNX would
    # otherwise silently lose appearance ReID — ID swaps through occlusions,
    # the exact thing OSNet exists to prevent — while the container still
    # reports healthy.
    require_reid: bool
    # Appearance-anchored presence hold (anchor_hold.py): a settled person's
    # track survives on the LOOK of their spot when detections die. Distance
    # gate validated from recordings (person-vs-empty ≈ 0.52, noise ≤ 0.14 —
    # 0.30 splits with wide margins). 0 disables. Requires ReID (OSNet).
    anchor_hold_dist: float
    anchor_miss_limit: int
    anchor_check_interval_s: float
    anchor_max_age_h: float
    # Corroboration bound: release an anchor after this many MINUTES with ZERO
    # person detections overlapping it. This is the PRIMARY liveness signal,
    # not a backstop — appearance separation collapses in the dark (measured
    # 0.52 sunlit vs 0.234 on a small night crop, under the 0.30 gate), so at
    # night only detector activity discriminates "still there" from "empty".
    # A seated person at ~2 fps is corroborated every ~0.7 s, so the default
    # 1.5 min never touches a real sitter while bounding a phantom to 90 s.
    # PERSON-ONLY: a parked car goes fully detector-blind for hours, so
    # absence of hits proves nothing for vehicles/pets (anchor_hold skips it).
    anchor_corroboration_min: float
    # Vehicles/pets park for DAYS — separate sanity age cap (person keeps
    # anchor_max_age_h). Validated on the west Mazda across 18 h incl. the
    # night transition: adjacent-in-time drift 0.031 (EMA tracks it), car vs
    # empty spot 0.53-0.57.
    anchor_max_age_nonperson_h: float
    # EMISSION staleness cutoff, decoupled from the association windows. A
    # Norfair track that lost its subject keeps living internally (lost/reid
    # windows — association memory, id continuity on re-match) but is only
    # EMITTED while its last MATCHED detection is at most this old. Without
    # the cutoff, a camera with a wide lost window (patio: 100 s, the old
    # pre-anchor crutch for seated people) kept publishing a stale predicted
    # box for up to 100 s while a NEW track already followed the person —
    # observed as two overlapping Activity entries (57 s overlap) for one
    # visit. Presence past this cutoff is the appearance anchor's job —
    # verified against the pixels, never blind coasting.
    emit_stale_s: float
    # Identity-at-source (identity_stamp.py): name NON-PERSON tracks by OSNet
    # body vs enrolled reference photos, right in the tracker, so the name
    # rides the TrackWire from ~the first second. Same rule + numbers as the
    # event-manager's finalize body-reference match (MIN-over-refs, threshold
    # + runner-up margin). 0 disables it. Persons are NEVER body-named
    # (face-only, downstream); vehicles neither (plate-only — appearance put a
    # stranger's car closer to the reference than the owner's own).
    identity_pet_threshold: float
    identity_margin: float
    # COCO classes eligible for anchoring. Person by default; pets are a
    # plausible extension (a sleeping dog), vehicles already have the
    # scene-state parking latch.
    anchor_classes: tuple[int, ...]
    # A camera that sends no detection batch for this long (disabled, removed,
    # offline) has its state evicted; it is rebuilt if the camera comes back.
    # The detector publishes even 0-detection frames, so a live empty scene is
    # never evicted.
    camera_idle_s: int

    @classmethod
    def from_env(cls) -> TrackerConfig:
        # No BABA_CAMERAS: the tracker subscribes to the NATS wildcard
        # `baba.detections.*`; the camera set is authoritative in Postgres.
        classes_raw = os.environ.get("BABA_TRACKER_CLASSES", "0,1,2,3,5,7")
        class_id_filter = (
            tuple(int(c.strip()) for c in classes_raw.split(",") if c.strip())
            if classes_raw
            else ()
        )

        return cls(
            nats_url=os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
            dsn=dsn_from_env(),
            lost_track_seconds=float(os.environ.get("BABA_TRACKER_LOST_SECONDS", "10")),
            frame_rate=int(os.environ.get("BABA_TRACKER_FPS", "15")),
            class_id_filter=class_id_filter,
            static_move_ratio=float(os.environ.get("BABA_TRACKER_STATIC_MOVE_RATIO", "0.08")),
            static_move_min_px=float(os.environ.get("BABA_TRACKER_STATIC_MOVE_MIN_PX", "4")),
            static_window_ms=int(os.environ.get("BABA_TRACKER_STATIC_WINDOW_MS", "8000")),
            park_threshold_ms=int(os.environ.get("BABA_TRACKER_PARK_THRESHOLD_MS", "60000")),
            reid_model_path=os.environ.get(
                "BABA_TRACKER_REID_MODEL", "/models/osnet_x0_25_msmt17.onnx"
            ),
            reid_distance_threshold=float(
                os.environ.get("BABA_TRACKER_REID_DISTANCE_THRESHOLD", "0.30")
            ),
            reid_lost_seconds=float(os.environ.get("BABA_TRACKER_REID_LOST_SECONDS", "20")),
            require_reid=_require_reid_default(),
            anchor_hold_dist=float(os.environ.get("BABA_TRACKER_ANCHOR_HOLD_DIST", "0.30")),
            anchor_miss_limit=int(os.environ.get("BABA_TRACKER_ANCHOR_MISS_LIMIT", "3")),
            anchor_check_interval_s=float(
                os.environ.get("BABA_TRACKER_ANCHOR_CHECK_INTERVAL_S", "2.0")
            ),
            anchor_max_age_h=float(os.environ.get("BABA_TRACKER_ANCHOR_MAX_AGE_H", "12")),
            anchor_corroboration_min=float(
                os.environ.get("BABA_TRACKER_ANCHOR_CORROBORATION_MIN", "1.5")
            ),
            anchor_max_age_nonperson_h=float(
                os.environ.get("BABA_TRACKER_ANCHOR_MAX_AGE_NONPERSON_H", "168")
            ),
            anchor_classes=tuple(
                int(c.strip())
                for c in os.environ.get(
                    "BABA_TRACKER_ANCHOR_CLASSES", "0,2,3,5,7"
                ).split(",")
                if c.strip()
            ),
            identity_pet_threshold=float(
                os.environ.get("BABA_TRACKER_IDENTITY_PET_THRESHOLD", "0.30")
            ),
            identity_margin=float(
                os.environ.get("BABA_TRACKER_IDENTITY_MARGIN", "0.08")
            ),
            emit_stale_s=float(os.environ.get("BABA_TRACKER_EMIT_STALE_S", "10")),
            camera_idle_s=int(os.environ.get("BABA_TRACKER_CAMERA_IDLE_S", "120")),
        )


def _require_reid_default() -> bool:
    """Whether a missing OSNet ReID model is fatal (no IoU-only fallback).

    Explicit `BABA_REQUIRE_REID` wins; otherwise derive from the build variant
    — nvidia/intel are production builds where appearance ReID is expected, so
    a silently dropped model should surface, not degrade.
    """
    v = os.environ.get("BABA_REQUIRE_REID", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return os.environ.get("BABA_VARIANT", "cpu").strip().lower() in ("nvidia", "intel")
