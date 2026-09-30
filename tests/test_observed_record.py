"""What a fresh start is allowed to erase.

The whole value of this list is what is NOT on it. A purge that reaches one
table too far takes an operator's cameras, their zone geometry, the reference
photographs behind every name, or the settings the system was tuned with — none
of which can be recovered from a backup that does not exist. So the split is
pinned here rather than left to whoever next edits the tuple.
"""

from baba_core.observed_record import (
    OBSERVED_HEADLINE_TABLE,
    OBSERVED_MEDIA_DIRS,
    OBSERVED_TABLES,
)

# Everything an operator built rather than everything the system saw. Nothing
# in here survives being wiped: there is no second copy anywhere.
CONFIGURED = {
    "cameras",
    "zones",
    "scene_regions",
    "scene_region_prototypes",
    "scene_region_status",
    "identity_labels",
    "identity_reference_photos",
    "users",
    "user_recovery_codes",
    "app_settings",
    "ai_settings",
    "face_recognition_settings",
    "recording_settings",
    "camera_detection_rules",
    "detector_global_rules",
    "camera_setting_profiles",
    "camera_profile_override",
    "camera_class_remap",
    "notification_channels",
    "notification_rules",
    "schema_versions",
    "admin_audit",
    "identity_audit",
}


def test_nothing_an_operator_built_is_on_the_list():
    assert not (set(OBSERVED_TABLES) & CONFIGURED)


def test_the_video_is_the_other_half_of_a_purge():
    """`recordings` and its files answer to `purge_scope = 'recordings'`, which
    exists on its own so the disk can be reclaimed without losing the log of
    what happened."""
    assert "recordings" not in OBSERVED_TABLES
    assert "segments" not in OBSERVED_MEDIA_DIRS
    assert "clips" not in OBSERVED_MEDIA_DIRS


def test_the_operators_own_pixels_are_not_observed_media():
    """Reference photographs and scene references live on the same media tree as
    the crops cut from tracks, and only the directory name separates them."""
    assert "reference_photos" not in OBSERVED_MEDIA_DIRS
    assert "scene_crops" not in OBSERVED_MEDIA_DIRS


def test_the_list_is_a_set_and_the_headline_is_on_it():
    assert len(set(OBSERVED_TABLES)) == len(OBSERVED_TABLES)
    assert len(set(OBSERVED_MEDIA_DIRS)) == len(OBSERVED_MEDIA_DIRS)
    assert OBSERVED_HEADLINE_TABLE in OBSERVED_TABLES


def test_a_purge_reaches_the_table_not_the_view():
    """`tracks` is a view over `tracks_all` since 100_*, and a view cannot be
    truncated — naming it here would fail the purge at the moment an operator
    asked for a fresh record, with everything already deleted around it."""
    assert "tracks_all" in OBSERVED_TABLES
    assert "tracks" not in OBSERVED_TABLES
