"""The media tier's directory names are a contract between services.

The recorder writes `segments/<camera>/…` and the api serves it back; the
embedder writes `crops/…` and the identity routes decide from the prefix
whether a stored path may be served; the purge deletes whole directories by
name. Written in two places these drift once, and the drift shows up as a file
the writer swears it wrote and the reader cannot find.
"""

from pathlib import Path

from baba_core.observed_record import OBSERVED_MEDIA_DIRS
from baba_core.paths import MediaLayout, StoragePaths

LAYOUT = MediaLayout(Path("/media"))
KINDS = ("segments", "clips", "crops", "face_crops", "thumbnails",
         "plate_crops", "scene_crops", "reference_photos")


def test_every_kind_resolves_under_the_media_root():
    for kind in KINDS:
        assert getattr(LAYOUT, kind) == Path("/media") / kind


def test_the_stored_form_and_the_written_form_agree():
    """A path is written as a Path and stored as a string relative to the
    root. If those two ever disagree the file is written where nobody looks."""
    for kind in KINDS:
        rel = MediaLayout.rel(kind, "x.jpg")
        assert Path("/media") / rel == getattr(LAYOUT, kind) / "x.jpg"
    assert (Path("/media") / MediaLayout.rel_camera_segments("west") / "a.mp4"
            == LAYOUT.camera_segments("west") / "a.mp4")


def test_a_camera_prefix_ends_in_a_separator():
    """It is used as a SQL LIKE prefix and to length-trim stored paths, so a
    missing separator would match a camera whose slug merely starts the same."""
    assert MediaLayout.rel_camera_segments("west") == "segments/west/"


def test_the_purge_only_names_directories_the_layout_knows():
    """The purge deletes whole directories by name. One that the layout does
    not define is either data nobody wrote or a directory nobody meant."""
    for name in OBSERVED_MEDIA_DIRS:
        assert hasattr(LAYOUT, name), name


def test_the_tier_hands_out_its_own_layout():
    paths = StoragePaths(models=Path("/m"), media=Path("/media"), state=Path("/s"))
    assert paths.layout.segments == Path("/media/segments")
