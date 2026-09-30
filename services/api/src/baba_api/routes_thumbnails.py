from __future__ import annotations

import logging
import re
from pathlib import Path

from baba_core.paths import (
    CROPS,
    FACE_CROPS,
    PLATE_CROPS,
    REFERENCE_PHOTOS,
    THUMBNAILS,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

thumbnails_router = APIRouter()

# Path-component sanitization: only allow uuid-style filenames plus .jpg.
# This prevents any kind of `..` or absolute-path tricks regardless of
# what FastAPI/Starlette does.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(jpe?g|png|webp)$")


def _serve_media_subdir(subdir: str, name: str, request: Request) -> FileResponse:
    """Common path-validated lookup under {media_path}/{subdir}/{name}.
    Subdir is a static string from the route, not user input — name is
    user input and gets the strict allow-list check."""
    if not _SAFE_NAME.match(name):
        raise HTTPException(400, "invalid filename")
    media_root = Path(request.app.state.config.media_path) / subdir
    full = (media_root / name).resolve()
    # Belt + suspenders: ensure resolved path stays under media_root.
    try:
        full.relative_to(media_root.resolve())
    except ValueError:
        raise HTTPException(400, "invalid path") from None
    if not full.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(full, media_type="image/jpeg")


@thumbnails_router.get("/thumbnails/{name}")
async def get_thumbnail(name: str, request: Request) -> FileResponse:
    return _serve_media_subdir(THUMBNAILS, name, request)


@thumbnails_router.get("/crops/{name}")
async def get_crop(name: str, request: Request) -> FileResponse:
    """Per-identity focused 224x224 JPEG written by the embedder. Same
    safety + path-traversal protection as thumbnails. Lives under
    /media/crops/<sample_uuid>.jpg."""
    return _serve_media_subdir(CROPS, name, request)


@thumbnails_router.get("/reference_photos/{name}")
async def get_reference_photo(name: str, request: Request) -> FileResponse:
    """User-uploaded reference photo for an enrolled identity. Lives
    under /media/reference_photos/<photo_uuid>.jpg."""
    return _serve_media_subdir(REFERENCE_PHOTOS, name, request)


@thumbnails_router.get("/scene_crops/{name}")
async def get_scene_crop(name: str, request: Request) -> FileResponse:
    """Reference crop for a scene-state prototype, written by the
    state-evaluator under /media/scene_crops/<prototype_uuid>.jpg."""
    return _serve_media_subdir("scene_crops", name, request)


@thumbnails_router.get("/plate_crops/{name}")
async def get_plate_crop(name: str, request: Request) -> FileResponse:
    """The plate a read was decided on. `plate_reads.crop_path` has been
    written as `plate_crops/<uuid>.jpg` since plate reading existed and the
    sightings feed puts it on the row as the tile for a departure no track
    witnessed — with no route here to serve it, so every one of those tiles
    was a broken image."""
    return _serve_media_subdir(PLATE_CROPS, name, request)


@thumbnails_router.get("/face_crops/{name}")
async def get_face_crop(name: str, request: Request) -> FileResponse:
    """Aligned 112x112 face crop written by the embedder alongside a track
    sample, under /media/face_crops/<sample_uuid>.jpg. Referenced by
    tracks.face_crop_path / track_embedding_samples.face_crop_path."""
    return _serve_media_subdir(FACE_CROPS, name, request)
