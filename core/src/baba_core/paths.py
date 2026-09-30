"""Central path/storage configuration.

Two storage tiers:
- **Fast tier** (SSD/NVMe): models, embeddings cache, postgres data, app state.
  Small, hot, latency-sensitive.
- **Slow tier** (HDD/array): media — video segments, event clips. Large, mostly
  sequential I/O. May live on a separate disk/pool than the rest of the stack.

All paths are env-driven so deployment can place them anywhere without code
changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class StoragePaths:
    """Resolved storage paths for the running process.

    Construct via `StoragePaths.from_env()`. Inside Docker containers we expect
    the env vars to point at mount points the compose file sets up; outside
    Docker (rare — dev only) defaults under /mnt/docker/baba.
    """

    models: Path
    media: Path
    state: Path

    @classmethod
    def from_env(cls) -> StoragePaths:
        return cls(
            models=Path(os.environ.get("BABA_MODELS_PATH", "/mnt/docker/baba/models")),
            media=Path(os.environ.get("BABA_MEDIA_PATH", "/mnt/docker/baba/media")),
            state=Path(os.environ.get("BABA_STATE_PATH", "/mnt/docker/baba/state")),
        )

    @property
    def model_cache(self) -> Path:
        """Where backends keep compiled native artifacts (TRT engines, OV IR…)."""
        return self.models / "cache"

    @property
    def layout(self) -> MediaLayout:
        """Where each kind of recorded artefact lives under the media tier."""
        return MediaLayout(self.media)


# The names under the media tier. They are a contract between services rather
# than any one service's private detail: the recorder writes segments and the
# api serves them back, the event-manager writes thumbnails and the identity
# routes decide from the prefix whether a stored path is one it may serve.
# Written in two places they drift once, and the drift shows up as a file the
# writer swears it wrote and the reader cannot find.
SEGMENTS = "segments"
CLIPS = "clips"
CROPS = "crops"
FACE_CROPS = "face_crops"
THUMBNAILS = "thumbnails"
PLATE_CROPS = "plate_crops"
SCENE_CROPS = "scene_crops"
REFERENCE_PHOTOS = "reference_photos"


@dataclass(slots=True, frozen=True)
class MediaLayout:
    """The media tier's directories, from whichever root the caller holds.

    Separate from `StoragePaths` because most services are handed a media root
    and have no business knowing about the models or state tiers — but they
    must agree with everyone else about what lives where under it.

    Both forms are here on purpose. `Path` for whoever writes or reads the
    file, and `rel_*` for what goes into the database, which stores paths
    relative to the media root so the tier can be moved without a migration.
    """

    root: Path

    @property
    def segments(self) -> Path:
        """Rolling continuous-recording segments (slow tier — large)."""
        return self.root / SEGMENTS

    @property
    def clips(self) -> Path:
        """Event and activity clips cut out of the segments."""
        return self.root / CLIPS

    @property
    def crops(self) -> Path:
        """Track crops the embedder keeps for re-identification and review."""
        return self.root / CROPS

    @property
    def face_crops(self) -> Path:
        """The aligned face a sample was embedded from."""
        return self.root / FACE_CROPS

    @property
    def thumbnails(self) -> Path:
        """One still per track, for the activity list."""
        return self.root / THUMBNAILS

    @property
    def plate_crops(self) -> Path:
        """The plate a read was decided on, kept as its evidence."""
        return self.root / PLATE_CROPS

    @property
    def reference_photos(self) -> Path:
        """Originals the operator enrolled an identity from."""
        return self.root / REFERENCE_PHOTOS

    @property
    def scene_crops(self) -> Path:
        """Scene-region stills: the references a region was taught from."""
        return self.root / SCENE_CROPS

    def camera_segments(self, slug: str) -> Path:
        return self.segments / slug

    @staticmethod
    def rel(kind: str, *parts: str) -> str:
        """A stored path, relative to the media root."""
        return "/".join((kind, *parts))

    @staticmethod
    def rel_camera_segments(slug: str) -> str:
        """The prefix every segment of one camera is stored under."""
        return f"{SEGMENTS}/{slug}/"
