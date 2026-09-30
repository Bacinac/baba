"""Import reference photos from an Immich library.

One of two external sources of face references, beside OPUS · Library
(`_opus.py`); an installation configures whichever it has. The rationale for
pulling pixels (not vectors) out of Immich, and why the per-face bounding box
matters, lives in `baba_api.immich`. This module is the BABA-side half:
crop → verify → hand to the shared enrollment path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import numpy as np
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.auth import AuthUser, current_user, require_admin
from baba_api.crypto import encrypt_secret
from baba_api.immich import (
    IMMICH_SETTINGS_KEY,
    ImmichClient,
    ImmichError,
    ImmichFaceBox,
    load_config,
)
from baba_api.routes_identities._base import identities_router
from baba_api.routes_identities._reference_photos import (
    _MAX_PHOTOS_PER_REQUEST,
    _persist_reference_photos,
)

log = logging.getLogger(__name__)

# How much context to include around Immich's face box. ArcFace alignment wants
# the whole head plus a little margin, and our own detector has to be able to
# re-find the face inside the crop — a box-tight crop starves YuNet of the
# surrounding context its priors expect. 1.8 keeps the target face at ~55% of
# the crop's linear size, which also keeps it comfortably the LARGEST face in
# frame when a neighbour intrudes at the edge (see _verify_locked_on_target).
_FACE_CROP_MARGIN = 1.8

# Reject Immich faces smaller than this in preview pixels. Below ~40px the
# preview downscale has already destroyed the detail ArcFace needs, and the
# resulting embedding is a confident-looking vector describing mush — worse
# than no reference at all, because MIN-over-references means one bad vector
# can pull in a false match forever.
_MIN_PREVIEW_FACE_PX = 40

# IoU between the face OUR detector locked onto and the box Immich pointed at.
# Below this we assume we grabbed a different face. 0.3 is loose on purpose:
# the two detectors legitimately disagree on chin/hairline extents, so a tight
# threshold would reject good crops, while a genuinely different face in a
# group shot lands near 0.
_MIN_TARGET_IOU = 0.3


class _ImmichSettingsIn(BaseModel):
    base_url: str = Field(..., min_length=1, description="e.g. http://192.0.2.102:2283")
    # Optional once a key is stored: the field can never be prefilled (we do
    # not echo the key back), so demanding it meant the operator had to dig the
    # key out of Immich again just to correct a typo in the address.
    api_key: str | None = None


@identities_router.get("/immich/status")
async def immich_status(request: Request, user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Is Immich configured, reachable, and does the key work? The api_key is
    never echoed back — only whether one is set."""
    pool = request.app.state.pool
    cfg = await load_config(pool, request.app.state.secret_key)
    if cfg is None:
        return {"configured": False, "reachable": False, "base_url": None, "version": None}
    try:
        async with ImmichClient(cfg) as c:
            version = await c.ping()
    except ImmichError as e:
        return {
            "configured": True,
            "reachable": False,
            "base_url": cfg.base_url,
            "version": None,
            "error": str(e),
        }
    return {
        "configured": True,
        "reachable": True,
        "base_url": cfg.base_url,
        "version": version,
    }


@identities_router.put("/immich/settings")
async def immich_set_settings(
    payload: _ImmichSettingsIn,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Store the Immich connection. Validated against the live server before
    it's saved — a key that doesn't work should fail here, at the moment the
    operator can still read the error, not silently at import time.

    Connecting an external credentialled service is an admin action. The stored
    key is only ever reused when the address is unchanged: a key is scoped to
    the server that issued it, so pointing at a different Immich demands its own
    key rather than silently shipping the old one to a new host."""
    from baba_api.immich import ImmichConfig

    pool = request.app.state.pool
    base_url = payload.base_url.strip().rstrip("/")
    key = (payload.api_key or "").strip()
    if not key:
        existing = await load_config(pool, request.app.state.secret_key)
        if existing is None:
            raise HTTPException(400, "api_key is required for the first connection")
        if existing.base_url != base_url:
            raise HTTPException(400, "a new server address needs its own api_key")
        key = existing.api_key
    cfg = ImmichConfig(base_url=base_url, api_key=key)
    try:
        async with ImmichClient(cfg) as c:
            version = await c.ping()
    except ImmichError as e:
        raise HTTPException(400, str(e)) from e

    enc = encrypt_secret(cfg.api_key, request.app.state.secret_key).decode("ascii")
    await pool.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        IMMICH_SETTINGS_KEY,
        json.dumps({"base_url": cfg.base_url, "api_key_encrypted": enc}),
    )
    log.info("immich settings saved: %s (v%s) by %s", cfg.base_url, version, user.username)
    return {"configured": True, "reachable": True, "base_url": cfg.base_url, "version": version}


async def _client(request: Request) -> ImmichClient:
    cfg = await load_config(request.app.state.pool, request.app.state.secret_key)
    if cfg is None:
        raise HTTPException(
            503,
            "Immich is not configured. Add the server URL and an API key "
            "(Immich → Account Settings → API Keys) via PUT /immich/settings.",
        )
    return ImmichClient(cfg)


@identities_router.get("/immich/people")
async def immich_people(request: Request, user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Named people in the Immich library, for the operator to pick an import
    source from."""
    client = await _client(request)
    try:
        async with client as c:
            people = await c.list_people()
    except ImmichError as e:
        raise HTTPException(502, str(e)) from e
    return {
        "people": [{"id": p.id, "name": p.name} for p in people],
        "total": len(people),
    }


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _crop_around_face(
    preview_rgb: np.ndarray,
    box: ImmichFaceBox,
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """Cut a margin'd crop around Immich's face box.

    Immich reports the box in the pixel space of the image its ML ran on, which
    is not necessarily the preview we downloaded — hence the rescale by the
    ratio of actual preview size to `box.image_width/height`. Returns the crop
    plus the target box expressed in CROP coordinates, so the caller can check
    which face our detector went for. None when the face is too small to be
    worth embedding or the geometry is degenerate.
    """
    ph, pw = preview_rgb.shape[:2]
    if box.image_width <= 0 or box.image_height <= 0:
        return None
    sx = pw / box.image_width
    sy = ph / box.image_height
    x1, y1, x2, y2 = box.x1 * sx, box.y1 * sy, box.x2 * sx, box.y2 * sy
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    if min(bw, bh) < _MIN_PREVIEW_FACE_PX:
        return None

    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    half_w = bw * _FACE_CROP_MARGIN / 2.0
    half_h = bh * _FACE_CROP_MARGIN / 2.0
    ox1 = int(max(0, round(cx - half_w)))
    oy1 = int(max(0, round(cy - half_h)))
    ox2 = int(min(pw, round(cx + half_w)))
    oy2 = int(min(ph, round(cy + half_h)))
    if ox2 - ox1 < 32 or oy2 - oy1 < 32:
        return None

    crop = preview_rgb[oy1:oy2, ox1:ox2]
    target_in_crop = (x1 - ox1, y1 - oy1, x2 - ox1, y2 - oy1)
    return np.ascontiguousarray(crop), target_in_crop


def _verify_locked_on_target(
    face_stack: Any,
    crop: np.ndarray,
    target_in_crop: tuple[float, float, float, float],
) -> str | None:
    """Confirm our detector picks the face Immich named. Returns None when it
    does, or a human-readable reason when it doesn't.

    This is the correctness linchpin of the whole import. `FaceStack.
    embed_from_crop` takes the LARGEST face it finds and says nothing about
    which one that was; on a group photo — the normal case in a family library
    — that can be the person standing next to our target. Enrolling a stranger
    under the operator's name would be silent and permanent, and would then
    match that stranger on camera forever.

    We pay one extra detect() (~3 ms CPU on YuNet, against ~80 ms for the
    ArcFace embed that follows) to make the choice explicit. Running detect()
    here and again inside embed_from_crop is deterministic on the same pixels,
    so the face we verify is the face we embed.
    """
    det = face_stack.detector.detect(crop)
    if det is None:
        return "BABA's detector found no face in Immich's face box"
    iou = _iou(det.bbox, target_in_crop)
    if iou < _MIN_TARGET_IOU:
        return (
            f"BABA's detector locked onto a different face than Immich's "
            f"(IoU {iou:.2f}) — likely a group photo"
        )
    return None


class _FromImmichIn(BaseModel):
    person_id: str = Field(..., min_length=1, description="Immich person uuid")
    limit: int = Field(
        default=32,
        ge=1,
        le=_MAX_PHOTOS_PER_REQUEST,
        description="Max photos to enroll from this person's library",
    )


@identities_router.post("/identities/{global_id}/reference-photos/from-immich")
async def reference_photos_from_immich(
    global_id: UUID,
    payload: _FromImmichIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Enroll an identity from a named person's photos in Immich.

    Immich located and named the faces; we take the pixels of the face it
    pointed at, re-embed them with BABA's own active face model (its vectors
    are not comparable to ours — see `baba_api.immich`), and store them as
    reference photos. The result is a wide, varied face reference set for a
    person who may never have been captured on camera at all, which is what
    MIN-over-references needs to have any chance on a hard CCTV crop.

    Already-imported assets are skipped, so re-running after adding photos to
    Immich only enrolls the new ones.
    """
    face_stack = getattr(request.app.state, "face_stack", None)
    if face_stack is None:
        raise HTTPException(
            503,
            "face recognition is not loaded — nothing to embed an Immich import with.",
        )

    pool = request.app.state.pool
    client = await _client(request)
    skipped: list[str] = []

    try:
        async with client as c:
            person = await c.get_person(payload.person_id)
            if person is None:
                raise HTTPException(
                    404,
                    f"Immich has no named person {payload.person_id!r}. "
                    "Unnamed face clusters are not importable — name the "
                    "person in Immich first.",
                )
            person_name = person.name

            # Assets we already pulled for this identity. Skipping them here
            # keeps a re-import from re-downloading and re-embedding the whole
            # library to reach a no-op; the unique index is the backstop.
            known = {
                r["origin_ref"]
                for r in await pool.fetch(
                    "SELECT origin_ref FROM identity_reference_photos "
                    "WHERE global_id = $1 AND source = 'immich' AND origin_ref IS NOT NULL",
                    global_id,
                )
            }

            # Over-fetch: some assets will lose their face to the size filter
            # or the group-photo guard, and we'd rather fill the operator's
            # budget than return 6 photos out of a requested 32.
            asset_ids = await c.asset_ids_for_person(payload.person_id, limit=payload.limit * 4)
            fresh = [a for a in asset_ids if a not in known]
            if known:
                skipped.append(f"{len(asset_ids) - len(fresh)} asset(s) already imported")
            if not fresh:
                raise HTTPException(
                    404,
                    f"no new photos for {person_name!r} in Immich ({len(known)} already imported)",
                )

            crops: list[np.ndarray] = []
            labels: list[str] = []
            used_assets: list[str | None] = []

            for asset_id in fresh:
                if len(crops) >= payload.limit:
                    break
                try:
                    faces = await c.faces_for_asset(asset_id)
                except ImmichError as e:
                    skipped.append(f"asset {asset_id[:8]}: {e}")
                    continue
                mine = [f for f in faces if f.person_id == payload.person_id]
                if not mine:
                    # Immich's search said this asset features the person but
                    # the face rows disagree — stale index. Not our problem to
                    # fix, just don't guess.
                    skipped.append(f"asset {asset_id[:8]}: no face box for this person")
                    continue
                # Biggest instance of this person in the asset. A person can
                # appear once; when Immich has split a face, the larger box is
                # the better-resolved one.
                box = max(mine, key=lambda f: f.width * f.height)

                try:
                    buf = await c.download_preview(asset_id)
                except ImmichError as e:
                    skipped.append(f"asset {asset_id[:8]}: {e}")
                    continue

                from baba_api.imaging import decode_rgb_capped

                preview = decode_rgb_capped(buf)
                if preview is None:
                    skipped.append(f"asset {asset_id[:8]}: undecodable preview")
                    continue

                cut = _crop_around_face(preview, box)
                if cut is None:
                    skipped.append(
                        f"asset {asset_id[:8]}: face too small in preview "
                        f"(<{_MIN_PREVIEW_FACE_PX}px)"
                    )
                    continue
                crop, target_in_crop = cut

                reason = await asyncio.to_thread(
                    _verify_locked_on_target, face_stack, crop, target_in_crop
                )
                if reason is not None:
                    skipped.append(f"asset {asset_id[:8]}: {reason}")
                    continue

                from baba_core import cap_long_edge

                crops.append(cap_long_edge(crop))
                labels.append(f"immich {asset_id[:8]}")
                used_assets.append(asset_id)
    except ImmichError as e:
        raise HTTPException(502, str(e)) from e

    if not crops:
        raise HTTPException(
            400,
            f"no usable faces for {person_name!r} — nothing enrolled. Reasons: {skipped}",
        )

    result = await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="immich",
        candidate_labels=labels,
        face_only=True,
        origin_refs=used_assets,
    )
    result["immich_person"] = person_name
    # Log every skip reason, not just the count. An import is a bulk operation
    # whose main diagnostic value IS the skip list — it says whether the photos
    # were lost to the group-photo guard, the size floor, or de-dup, which is
    # what decides whether to raise the limit or loosen the crop margin. The
    # reasons used to live only in the HTTP response, so any later action that
    # overwrote the result line (clicking auto-pick right after an import, say)
    # destroyed them permanently. One line per import, at the granularity that
    # makes the run reconstructable after the fact.
    log.info(
        "immich import: gid=%s person=%r used=%d skipped=%d by %s%s",
        global_id,
        person_name,
        result["used"],
        len(result["skipped"]),
        user.username,
        "".join(f"\n    skip: {s}" for s in result["skipped"]),
    )
    return result
