from __future__ import annotations

import os
from dataclasses import dataclass

from baba_core import dsn_from_env, go2rtc_auth_from_env


def _default_face_model_key() -> str:
    """The shipped face embedder key, used until the boot override reads the
    active model out of face_recognition_settings."""
    from baba_core.face_models import DEFAULT_FACE_MODEL

    return DEFAULT_FACE_MODEL


@dataclass(slots=True, frozen=True)
class EventManagerConfig:
    """Process config. Per-camera knobs live in the cameras table; thresholds
    that apply uniformly (track timeout, min track lifetime to be worth saving)
    are global and live here."""

    nats_url: str
    dsn: str

    # A track that hasn't been seen in this many ms is considered ended. Must
    # comfortably exceed the longest frame interval a camera can run at (1 s at
    # idle_fps=1 under the adaptive rate); the tracker's own persistence window
    # is BABA_TRACKER_LOST_SECONDS.
    track_timeout_ms: int

    # Don't bother saving tracks that lived for less than this — almost always
    # noise (a single-frame detection that came back differently next frame).
    min_track_lifetime_ms: int

    # Don't save tracks with fewer than this many observations. Same reason.
    min_observations: int

    # Static-object suppression: skip finalize if the bbox centre travelled
    # less than this fraction of the OBJECT'S OWN size (bbox diagonal) over the
    # track's lifetime. Object-relative (not frame-relative) so it's resolution-
    # and distance-independent: a parked car's jitter is a big % of a 320px
    # downscaled frame but a small fraction of the car itself, while a car that
    # drives through moves many car-lengths. Catches "parked car generates
    # endless tracks/clips" without eating real pedestrian/vehicle passes.
    # 0 disables. ~0.5 = "moved less than half its own size → static/parked".
    static_move_ratio: float

    # Thumbnails: where to write event JPEGs and how to fetch source frames.
    # media_path is the same path baba_core.StoragePaths.media resolves to.
    media_path: str
    go2rtc_url: str
    # Basic auth for that URL. The live-snapshot fallback is a plain HTTP
    # client, so without this it 401s — and a missing thumbnail is silent.
    go2rtc_auth: tuple[str, str]
    thumbnail_max_width: int

    # Identity policy (redesign 2026-06-18). Two distinct re-ID gates, used
    # in DIFFERENT reliability envelopes — face and body cosine distributions
    # are different shapes AND mean different things:
    #
    #   reid_face_cosine_threshold (face / TopoFR or AuraFace):
    #     ~0.10-0.30 same person ANY outfit, 0.50+ different person. This is
    #     the ONLY signal that may assign a CROSS-SESSION identity (inherit a
    #     prior identity's global_id, named or anonymous). Face is outfit- and
    #     day-invariant, so it can say WHO across visits. UI-tunable via
    #     face_recognition_settings.match_threshold (override applied at boot).
    #
    #   reid_body_cluster_threshold (body / OSNet):
    #     ~0.15-0.25 same person same outfit, 0.40+ different outfit/person.
    #     Body embeds APPEARANCE (outfit/silhouette), not identity, so it must
    #     NEVER attach to a named/enrolled identity — doing that produced the
    #     "everyone becomes the one person with live face captures" over-merge
    #     vortex. Instead body is used ONLY to cluster ANONYMOUS tracks to each
    #     other within a tight envelope where appearance == identity is safe:
    #     same camera + same day (reid_body_cluster_window_hours). This
    #     consolidates "the same unnamed person crossing the patio 5× today"
    #     into one anonymous card without ever claiming who they are.
    reid_body_cluster_threshold: float  # anonymous body-cluster gate (~0.25)
    reid_face_cosine_threshold: float  # face cross-session assign gate (~0.40)
    # Key of the active face embedder (face_recognition_settings.model_key,
    # applied at boot + hot-reloaded). Face-embedding comparisons filter on it:
    # a vector computed under a different model lives in a different space, so
    # comparing across models puts wrong names on people. Defaults to the
    # shipped model so an install without the settings row still matches.
    face_embedding_model: str
    # Anonymous body clustering only considers candidate tracks finalized
    # within this many hours on the SAME camera — the window in which "same
    # outfit" holds, so OSNet body distance tracks identity. Wider than ~a day
    # risks merging different outfits; 0 disables anonymous body clustering.
    reid_body_cluster_window_hours: int
    # Face-anchored body chain. The ONE sanctioned path where body appearance
    # may reach a NAMED identity — and it is fenced tighter than anonymous
    # clustering precisely because the blast radius is larger (contaminating a
    # real person's record, not just merging two anonymous cards). An anonymous
    # track is promoted to a named identity when its body strongly matches a
    # recent FACE-VERIFIED track of that identity on the SAME camera:
    #   - the anchor MUST be face_verified=true — a real face confirmation,
    #     never another body chain, so chains can't compound into drift;
    #   - same camera + a short session window (not the 7-day face window),
    #     the envelope where "same outfit" holds so OSNet distance == identity;
    #   - a tight, independently-tunable body distance.
    # Motivating case (patio 2026-07-16): a 292 s shirtless session with a
    # visible tattoo sat at body distance 0.185 to THREE face-confirmed Marko
    # tracks minutes apart, yet stayed anonymous because body was forbidden
    # from ever touching a named id. Set either knob to 0 to disable.
    reid_face_anchor_body_threshold: float
    reid_face_anchor_window_hours: int
    # Same chain reaching ACROSS cameras: when you walk from a camera that just
    # face-confirmed you to one that never sees your face, your body carries the
    # name. Only the DISTANCE differs from same-camera — a different angle,
    # scale and light on one jacket spreads true and false matches alike, so
    # 0.32 sees nothing across cameras and 0.40 is too far (ground truth over
    # 7 days: errors jump 3 → 8 between them). The window is shared, because
    # there was never a reason for a crossing to expire sooner than a return to
    # the same camera — only an assumption that it happens within minutes, and
    # that assumption cost the branch every match it could have made. Set to 0
    # to disable cross-camera anchoring, leaving same-camera untouched.
    reid_face_anchor_xcam_threshold: float
    # How much closer the winning identity must be than the runner-up. Distance
    # alone cannot separate "this is Marko" from "Marko is the only person we hold
    # references for"; a claim needs a rival to beat. Removed a third of the
    # chain's errors for two true matches (same guard, same reason as
    # reid_body_reference_margin). 0 disables the check.
    reid_face_anchor_margin: float
    # How good a face crop must be to count as a FACE confirmation, so "face"
    # (and the green badge) means an actual frontal face — not the top of a
    # head that happened to embed close to another poor crop. "Confident" = the
    # best face crop the track produced scored at least this (SCRFD face_score,
    # migration 060). Two uses, same number:
    #   * live (live_identity.py): a face must be confident to OVERRIDE a body
    #     guess — a marginal face can't unseat a strong body match.
    #   * finalize track-to-track: a below-floor crop does NOT face-verify via a
    #     neighbour-face match; the track still gets its identity, but from the
    #     BODY anchor (honest "body" provenance) instead of a weak-face claim.
    #     Reference matches are exempt — landing under threshold against a clean
    #     enrolled portrait already requires a good crop.
    # Measured on patio: real Marko reference matches score 0.72-0.89; two of his
    # own ear/back-of-head crops matched each other at face-dist 0.653 while
    # sitting 0.92 from every portrait — 0.70 keeps the real ones and reclasses
    # those chains as body. Set to 0 to disable the floor (accept any crop).
    reid_confident_face_score: float
    # A track's identity is decided by its BEST-MATCHING face, not its highest
    # face_score one. A track produces several face crops of near-identical
    # score but very different match quality (patio 2026-07-16: three samples
    # scored 0.666/0.683/0.695, distances 0.891/0.428/0.804 — the top-score
    # crop was the WORST match, so the single-best-score pick missed a clean
    # 0.428 hit and the whole session, then its cross-camera cascade, went
    # unrecognised). So match over ALL of the track's face samples scoring at
    # least this and take the minimum distance, the same MIN-over-refs logic we
    # already apply on the reference side. The floor keeps a garbage crop that
    # coincidentally embeds close from stealing a match; 0.5 admits real-but-
    # imperfect faces (the 0.683 above) while excluding ears/shoulders. 0 = use
    # every face sample; a value at/above the detector's real-face boundary
    # (~0.77) collapses back toward single-best behaviour.
    reid_face_sample_min_score: float
    # Only look at tracks finalized in the last N days for FACE re-ID. Bounds
    # the candidate set so HNSW search stays fast and identities don't bleed
    # across reboots of the camera fleet weeks apart.
    reid_window_days: int
    # How often the re-ID resweep runs. re-ID at finalize is one-shot; a face
    # that lands after finalize (the embedder writes face_embedding async) or a
    # reference enrolled later would never link. The resweep re-matches recent
    # anonymous person faces against named identities' curated face evidence
    # and links any within reid_face_cosine_threshold -- the call finalize
    # would have made. 0 disables.
    reid_resweep_interval_s: int

    # Body-reference match for NON-PERSON identities (pets, vehicles). Unlike
    # people — whose identity is face-only because outfits change and body
    # over-merges — a pet or vehicle HAS no face, so the only way to ever name
    # them is body appearance against reference photos the operator explicitly
    # enrolled. Enrolling a reference IS the opt-in that says "trust body for
    # THIS subject", so this path is exempt from the no-body-for-named rule, but
    # fenced hard: match a track's body embedding via MIN-over-the-identity's-
    # reference-photos, restricted to the pet pool (pet↔dog/cat), and adopt the
    # nearest identity ONLY when it is under the threshold AND clearly closer
    # than the runner-up by `..._margin` — a lone tight match with a distant
    # runner-up is confident; two comparably-close identities is ambiguous and
    # stays anonymous. PETS ONLY: vehicles were dropped from this path after
    # measurement showed a stranger's car closer to the enrolled reference than
    # the owner's own (0.128 vs 0.170) — a vehicle is named by its plate or
    # not at all. Threshold at 0 disables it. Runs in finalize AND the resweep
    # (so photos enrolled after a track finalized still link it).
    reid_body_reference_pet_threshold: float
    reid_body_reference_margin: float

    # Per-observation embeddings get noisy fast — a busy 8-camera scene fills
    # ~250k rows/day. Anything older than this gets reaped by the background
    # sweeper. The canonical "best" embedding still lives on `tracks.embedding`
    # so cross-camera re-ID and the Identities UI keep working after cleanup.
    samples_retention_days: int
    # How often the sweeper runs. Cleanup is a cheap UPDATE/DELETE so the
    # interval doesn't need to be tight; hourly keeps DB size predictable
    # without spiking I/O.
    samples_cleanup_interval_s: int

    # zone_dwell event fires after a track has been inside a zone for
    # this many ms without leaving. Useful for "person loitering in
    # restricted area for 30+s" rules. Default 30000 (30s). 0 disables.
    dwell_threshold_ms: int

    @classmethod
    def from_env(cls) -> EventManagerConfig:
        return cls(
            nats_url=os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
            dsn=dsn_from_env(),
            track_timeout_ms=int(os.environ.get("BABA_EVENT_TRACK_TIMEOUT_MS", "5000")),
            min_track_lifetime_ms=int(os.environ.get("BABA_EVENT_MIN_TRACK_LIFETIME_MS", "1500")),
            min_observations=int(os.environ.get("BABA_EVENT_MIN_OBSERVATIONS", "5")),
            static_move_ratio=float(os.environ.get("BABA_EVENT_STATIC_MOVE_RATIO", "0.5")),
            media_path=os.environ.get("BABA_MEDIA_PATH", "/media"),
            go2rtc_url=os.environ.get("BABA_GO2RTC_URL", "http://host.docker.internal:1984"),
            go2rtc_auth=go2rtc_auth_from_env(),
            thumbnail_max_width=int(os.environ.get("BABA_THUMBNAIL_MAX_WIDTH", "640")),
            reid_body_cluster_threshold=float(
                os.environ.get("BABA_EVENT_REID_BODY_CLUSTER_THRESHOLD", "0.25")
            ),
            reid_face_cosine_threshold=float(
                os.environ.get("BABA_EVENT_REID_FACE_COSINE_THRESHOLD", "0.40")
            ),
            face_embedding_model=_default_face_model_key(),
            reid_body_cluster_window_hours=int(
                os.environ.get("BABA_EVENT_REID_BODY_CLUSTER_WINDOW_HOURS", "16")
            ),
            reid_face_anchor_body_threshold=float(
                os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_BODY_THRESHOLD", "0.25")
            ),
            reid_face_anchor_window_hours=int(
                os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_WINDOW_HOURS", "6")
            ),
            reid_face_anchor_xcam_threshold=float(
                os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_XCAM_THRESHOLD", "0.35")
            ),
            reid_face_anchor_margin=float(
                os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_MARGIN", "0.05")
            ),
            reid_confident_face_score=float(
                os.environ.get("BABA_EVENT_REID_CONFIDENT_FACE_SCORE", "0.70")
            ),
            reid_face_sample_min_score=float(
                os.environ.get("BABA_EVENT_REID_FACE_SAMPLE_MIN_SCORE", "0.5")
            ),
            reid_window_days=int(os.environ.get("BABA_EVENT_REID_WINDOW_DAYS", "7")),
            reid_resweep_interval_s=int(
                os.environ.get("BABA_EVENT_REID_RESWEEP_INTERVAL_S", "900")
            ),
            reid_body_reference_pet_threshold=float(
                os.environ.get("BABA_EVENT_REID_BODY_REFERENCE_PET_THRESHOLD", "0.30")
            ),
            reid_body_reference_margin=float(
                os.environ.get("BABA_EVENT_REID_BODY_REFERENCE_MARGIN", "0.08")
            ),
            samples_retention_days=int(os.environ.get("BABA_EVENT_SAMPLES_RETENTION_DAYS", "30")),
            samples_cleanup_interval_s=int(
                os.environ.get("BABA_EVENT_SAMPLES_CLEANUP_INTERVAL_S", "3600")
            ),
            dwell_threshold_ms=int(os.environ.get("BABA_EVENT_DWELL_MS", "30000")),
        )
