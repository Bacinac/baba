"""What BABA has observed, named once so a purge and its estimate agree.

The split this draws is between what the system was CONFIGURED to watch with
and what it then SAW. Cameras, zones, scene regions and their reference crops,
identities and their photographs, users, settings and rules are the former:
they are what an operator built, and a fresh start keeps every one of them.
Events, tracks, the crops cut from them, plates, places, presence, telemetry
and the heat maps derived from all of it are the latter.

Two things deliberately stay out of the list. `identity_audit` records the
operator's own decisions about identities that survive, so it is provenance for
what is kept rather than part of what was seen. `scene_region_status` is what
each region believes RIGHT NOW — current state, not history — and wiping it
would only make the evaluator re-derive what it already knows.

`recordings` is absent because the video is the other half of a purge and can
be erased on its own; see `recording_settings.purge_scope`.
"""

from __future__ import annotations

from baba_core.paths import CROPS, FACE_CROPS, PLATE_CROPS, THUMBNAILS

# Truncated together in one statement, so the foreign keys between them never
# have to be ordered by hand.
OBSERVED_TABLES: tuple[str, ...] = (
    "notification_deliveries",
    "place_occupancy",
    "plate_reads",
    "plate_zone_pass_reads",
    "track_embedding_samples",
    "track_birth_spots",
    "presence_episodes",
    "events",
    # The table, not the `tracks` view over it: a view cannot be truncated,
    # and a fresh start means the hidden rows go too.
    "tracks_all",
    "camera_telemetry",
    "telemetry_incidents",
    "heatmap_daily",
    "camera_setting_changes",
    "face_recompute_jobs",
)

# Media that belongs to those rows and to nothing else. `reference_photos` and
# `scene_crops` are the operator's, and `segments`/`clips` are the video half.
OBSERVED_MEDIA_DIRS: tuple[str, ...] = (
    THUMBNAILS,
    CROPS,
    FACE_CROPS,
    PLATE_CROPS,
)

# The one table whose size is worth reporting back before the operator commits
# to this: it is the row count they recognise as "the Activity feed".
OBSERVED_HEADLINE_TABLE = "events"
