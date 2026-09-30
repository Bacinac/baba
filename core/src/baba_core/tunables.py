"""The catalogue of operator-tunable pipeline values.

One table, read by everyone: the tracker and the event-manager resolve their
thresholds through it, the api renders the settings form from it, and the UI
gets the label and the range without a second list to keep in step. The
DEFAULT is not here — the deployment owns that, and each service publishes
what its env resolved to (`app_settings.pipeline_defaults`) so the form can
show it. Adding a knob means adding one row here — no plumbing in the
service, no field in an API model, no input in a template.

Ranges are not decoration. Several of these invert a gate rather than merely
loosening it when pushed far enough: a re-ID distance at or above 1.0 makes
every pair with no embedding a MATCH instead of a rejection, and an anchor hold
distance of 0 silently disables the hold. Env never validated any of this.

Grouped by the question the operator is actually asking, not by which service
happens to read the value.
"""

from __future__ import annotations

from baba_core.pipeline_settings import Bound

__all__ = ["GROUPS", "REID_KEY", "REID_TUNABLES", "TRACKING_KEY", "TRACKING_TUNABLES"]

# `app_settings` rows. Tracking already existed (migration 053) and carries the
# per-camera-overridable motion values plus the phantom registry thresholds;
# the anchor and identity knobs join it because they are the same subsystem.
TRACKING_KEY = "tracking_defaults"
REID_KEY = "reid_defaults"


# --- tracker ---------------------------------------------------------------

TRACKING_TUNABLES: dict[str, Bound] = {
    # Appearance anchor: how a subject that stops moving keeps its identity.
    "anchor_hold_dist": Bound(0.0, 1.0, "tun_anchor_hold_dist"),
    "anchor_miss_limit": Bound(1, 30, "tun_anchor_miss_limit"),
    "anchor_check_interval_s": Bound(0.2, 30.0, "tun_anchor_check_interval"),
    "anchor_max_age_h": Bound(1, 168, "tun_anchor_max_age_h"),
    "anchor_max_age_nonperson_h": Bound(1, 1440, "tun_anchor_max_age_nonperson_h"),
    "anchor_corroboration_min": Bound(0.0, 60.0, "tun_anchor_corroboration_min"),
    # Identity stamped at the tracker for pets. Vehicles are plate-only:
    # appearance put a stranger's car closer to the reference than the
    # owner's own (0.128 vs 0.170), so there is no threshold to tune.
    "identity_pet_threshold": Bound(0.0, 0.99, "tun_identity_pet"),
    "identity_margin": Bound(0.0, 0.5, "tun_identity_margin"),
    # Track association and lifetime.
    "reid_distance_threshold": Bound(0.05, 0.99, "tun_reid_distance"),
    "static_move_min_px": Bound(0, 200, "tun_static_move_min_px"),
    "static_window_ms": Bound(500, 120_000, "tun_static_window_ms"),
    "emit_stale_s": Bound(1, 120, "tun_emit_stale_s"),
    # Static-phantom registry (already lived in this row).
    "phantom_min_births": Bound(3, 10_000, "tun_phantom_min_births"),
    "phantom_min_span_s": Bound(60, 2_592_000, "tun_phantom_min_span_s"),
    # A detection at/above this is a real subject and is never suppressed by
    # the registry, whatever spot its foot lands in. See phantom_match.
    "phantom_real_subject_conf": Bound(0.0, 1.0, "tun_phantom_real_conf"),
}


# --- event-manager ---------------------------------------------------------

REID_TUNABLES: dict[str, Bound] = {
    # Face is the strong signal; body is the fallback that must never reach a
    # named identity on its own (see the face-anchored body chain).
    "reid_body_cluster_threshold": Bound(0.0, 0.99, "tun_reid_body_cluster"),
    "reid_body_cluster_window_hours": Bound(1, 8760, "tun_reid_body_window_h"),
    "reid_face_anchor_body_threshold": Bound(0.0, 0.99, "tun_reid_face_anchor_body"),
    "reid_face_anchor_window_hours": Bound(0, 168, "tun_reid_face_anchor_window_h"),
    "reid_face_anchor_xcam_threshold": Bound(0.0, 0.99, "tun_reid_face_anchor_xcam"),
    "reid_face_anchor_margin": Bound(0.0, 0.5, "tun_reid_face_anchor_margin"),
    "reid_confident_face_score": Bound(0.0, 1.0, "tun_reid_confident_face"),
    "reid_face_sample_min_score": Bound(0.0, 1.0, "tun_reid_face_sample_min"),
    "reid_window_days": Bound(1, 3650, "tun_reid_window_days"),
    # Reference photos (pets enrolled from stills).
    "reid_body_reference_pet_threshold": Bound(0.0, 0.99, "tun_reid_ref_pet"),
    "reid_body_reference_margin": Bound(0.0, 0.5, "tun_reid_ref_margin"),
    # What is worth recording as a visit at all.
    "min_track_lifetime_ms": Bound(0, 60_000, "tun_min_track_lifetime_ms"),
    "min_observations": Bound(1, 500, "tun_min_observations"),
}


# The face MATCH threshold is deliberately absent: it lives in
# `face_recognition_settings` beside the model it belongs to, because a
# threshold is only meaningful for a given embedder. Two homes for one number
# is the thing this module exists to prevent.

GROUPS: dict[str, dict[str, Bound]] = {
    TRACKING_KEY: TRACKING_TUNABLES,
    REID_KEY: REID_TUNABLES,
}
