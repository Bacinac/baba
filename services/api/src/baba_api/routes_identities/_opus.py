"""Enrol people from OPUS · Library, the household's photo catalogue.

Why pixels and not vectors, and why the library's face box has to be honoured,
is in `baba_api.opus`. This module is the BABA-side half: pick the faces →
cut → verify → hand to the shared enrolment path. Two doors in:

  * `POST /identities/from-opus` — a person in the library becomes an identity
    here, named as the library names them, with their references enrolled in
    the same call. The way somebody is first added.
  * `POST /identities/{gid}/reference-photos/from-opus` — more references for
    an identity that exists. Once an identity is linked to a library person
    the person need not be chosen again.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from baba_core import cap_long_edge
from baba_core.native import run_native
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from baba_api.auth import AuthUser, current_user, require_admin
from baba_api.crypto import encrypt_secret
from baba_api.imaging import decode_rgb_capped
from baba_api.opus import (
    OPUS_SETTINGS_KEY,
    OpusClient,
    OpusConfig,
    OpusError,
    OpusFace,
    load_config,
)
from baba_api.routes_identities._base import _label_row_to_dict, identities_router
from baba_api.routes_identities._reference_photos import (
    _MAX_PHOTOS_PER_REQUEST,
    _persist_reference_photos,
)

log = logging.getLogger(__name__)

# How much context to include around the library's face box. ArcFace
# alignment wants the whole head plus a little margin, and our own detector
# has to be able to re-find the face inside the crop — a box-tight crop
# starves it of the surrounding context its priors expect. 1.8 keeps the
# target face at ~55% of the crop's linear size, which also keeps it
# comfortably the LARGEST face in frame when a neighbour intrudes at the edge.
_FACE_CROP_MARGIN = 1.8

# Reject faces smaller than this in PREVIEW pixels. Below ~40px the preview
# downscale has destroyed the detail ArcFace needs, and the resulting
# embedding is a confident-looking vector describing mush — worse than no
# reference, because MIN-over-references means one bad vector can pull in a
# false match forever.
_MIN_PREVIEW_FACE_PX = 40

# IoU between the face OUR detector locked onto and the box the library drew.
# Below this we assume we grabbed a different face. 0.3 is loose on purpose:
# two detectors legitimately disagree on chin/hairline extents, so a tight
# threshold would reject good crops, while a genuinely different face in a
# group shot lands near 0.
_MIN_TARGET_IOU = 0.3

# What we ask the library for. A camera compares against how somebody looks
# NOW, not how they looked at their christening; five years is the window in
# which an adult is the same person to a face model. 120 px in the original is
# the library's own floor for a portrait, and comfortably above the 60 px a
# camera face needs to name anyone.
#
# Two faces a day, and the whole window's worth of days: a trip yields a
# hundred frames of one afternoon in the same sunglasses, and asked newest
# first the library handed back that afternoon and little else — 115 of 160
# candidates from two days, and de-dup then threw most of them away. The
# breadth a reference set needs is DAYS, and the library caps a day for us.
_YEARS_BACK = 5
_MIN_ORIGINAL_FACE_PX = 120
_FACES_PER_DAY = 2
_CANDIDATE_LIMIT = 500


class _OpusSettingsIn(BaseModel):
    base_url: str = Field(..., min_length=1, description="e.g. http://192.0.2.103:8095")
    # Optional once stored: the token is never echoed back, so demanding it
    # meant the operator had to dig it out of the library again just to
    # correct a typo in the address.
    token: str | None = None


async def _status(cfg: OpusConfig | None) -> dict[str, Any]:
    if cfg is None:
        return {"configured": False, "reachable": False, "base_url": None, "people": None}
    try:
        async with OpusClient(cfg) as c:
            people = await c.ping()
    except OpusError as e:
        return {
            "configured": True,
            "reachable": False,
            "base_url": cfg.base_url,
            "people": None,
            "error": str(e),
        }
    return {"configured": True, "reachable": True, "base_url": cfg.base_url, "people": people}


@identities_router.get("/reference-sources")
async def reference_sources(request: Request, user: AuthUser = Depends(current_user)) -> dict[str, bool]:
    """Which external libraries this installation has set up, read from the
    settings alone — no ping — so a page can show the import doors it has
    without knocking on every server each time it opens."""
    from baba_api.immich import load_config as load_immich

    pool = request.app.state.pool
    sk = request.app.state.secret_key
    return {
        "opus": await load_config(pool, sk) is not None,
        "immich": await load_immich(pool, sk) is not None,
    }


@identities_router.get("/opus/status")
async def opus_status(request: Request, user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Is the library configured, reachable, and does the token work? The
    token is never echoed back — only whether one is set."""
    return await _status(await load_config(request.app.state.pool, request.app.state.secret_key))


@identities_router.put("/opus/settings")
async def opus_set_settings(
    payload: _OpusSettingsIn,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> dict[str, Any]:
    """Store the library connection. Validated against the live server before
    it is saved — a token that does not work should fail here, while the
    operator can still read the error, not silently at import time.

    Connecting an external credentialled service is an admin action. The stored
    token is only reused when the address is unchanged: a token is scoped to the
    library that issued it, so a new address demands its own token rather than
    silently shipping the old one to a different host."""
    pool = request.app.state.pool
    sk = request.app.state.secret_key
    base_url = payload.base_url.strip().rstrip("/")
    token = (payload.token or "").strip()
    if not token:
        existing = await load_config(pool, sk)
        if existing is None:
            raise HTTPException(400, "token is required for the first connection")
        if existing.base_url != base_url:
            raise HTTPException(400, "a new library address needs its own token")
        token = existing.token
    cfg = OpusConfig(base_url=base_url, token=token)
    try:
        async with OpusClient(cfg) as c:
            people = await c.ping()
    except OpusError as e:
        raise HTTPException(400, str(e)) from e

    enc = encrypt_secret(cfg.token, sk).decode("ascii")
    await pool.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        OPUS_SETTINGS_KEY,
        json.dumps({"base_url": cfg.base_url, "token_encrypted": enc}),
    )
    log.info("opus settings saved: %s (%d people) by %s", cfg.base_url, people, user.username)
    return {"configured": True, "reachable": True, "base_url": cfg.base_url, "people": people}


async def _client(request: Request) -> OpusClient:
    cfg = await load_config(request.app.state.pool, request.app.state.secret_key)
    if cfg is None:
        raise HTTPException(
            503,
            "OPUS Library is not configured. Add its address and service token "
            "under Settings → Face recognition.",
        )
    return OpusClient(cfg)


def _same_name(p: Any, unlinked: dict[str, str]) -> str | None:
    """An unlinked person identity that goes by this person's name, full or
    given. The household's identities were typed in before the library was
    a source — "Ana", not "Ana Horvat" — and a second identity for the
    same person is the over-merge problem in reverse."""
    for candidate in (p.name, p.given_name):
        gid = unlinked.get(candidate.strip().casefold())
        if candidate.strip() and gid:
            return gid
    return None


def _person_dict(p: Any, linked: dict[int, str], unlinked: dict[str, str]) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "faces": p.faces,
        "years": [p.first_year, p.last_year],
        "cover": p.cover_face_id,
        # which identity here already stands for this person, so the picker
        # can say so instead of offering to create a second one
        "identity": linked.get(p.id),
        # an identity of the same name not yet linked: the picker offers to
        # link and enrol it rather than to create a twin
        "same_name": _same_name(p, unlinked),
    }


async def _identity_links(pool: Any) -> tuple[dict[int, str], dict[str, str]]:
    linked: dict[int, str] = {}
    unlinked: dict[str, str] = {}
    for r in await pool.fetch(
        "SELECT global_id, name, opus_person_id FROM identity_labels WHERE kind = 'person'"
    ):
        if r["opus_person_id"] is not None:
            linked[r["opus_person_id"]] = str(r["global_id"])
        elif r["name"]:
            unlinked.setdefault(r["name"].strip().casefold(), str(r["global_id"]))
    return linked, unlinked


@identities_router.get("/opus/people")
async def opus_people(request: Request, user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """Named people in the library, for the operator to pick from."""
    client = await _client(request)
    try:
        async with client as c:
            people = await c.list_people()
    except OpusError as e:
        raise HTTPException(502, str(e)) from e
    linked, unlinked = await _identity_links(request.app.state.pool)
    return {"people": [_person_dict(p, linked, unlinked) for p in people], "total": len(people)}


@identities_router.get("/opus/people/{person_id}/cover")
async def opus_person_cover(
    person_id: int, request: Request, user: AuthUser = Depends(current_user)
) -> Response:
    """The library's thumbnail of a person's best face, proxied: the browser
    holds no library token and must not."""
    client = await _client(request)
    try:
        async with client as c:
            person = await c.get_person(person_id)
            if person is None or person.cover_face_id is None:
                raise HTTPException(404, "no cover face for this person")
            data = await c.download_face_crop(person.cover_face_id)
    except OpusError as e:
        raise HTTPException(502, str(e)) from e
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


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
    preview_rgb: np.ndarray, face: OpusFace
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """Cut a margin'd crop around the library's box. The box is fractions of
    the frame, so it lands on the preview without knowing what size the
    library measured it at. Returns the crop plus the target box in CROP
    coordinates, so the caller can check which face our detector went for;
    None when the face is too small to be worth embedding."""
    ph, pw = preview_rgb.shape[:2]
    x1, y1 = face.x * pw, face.y * ph
    x2, y2 = (face.x + face.w) * pw, (face.y + face.h) * ph
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0 or min(bw, bh) < _MIN_PREVIEW_FACE_PX:
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
    return np.ascontiguousarray(crop), (x1 - ox1, y1 - oy1, x2 - ox1, y2 - oy1)


def _verify_locked_on_target(
    face_stack: Any, crop: np.ndarray, target_in_crop: tuple[float, float, float, float]
) -> str | None:
    """Confirm our detector picks the face the library named. This is the
    correctness linchpin of the import: `embed_from_crop` takes the LARGEST
    face and says nothing about which, and on a family photograph that can be
    the person standing next to our target. One extra detect() on the same
    pixels makes the choice explicit; the face we verify is the face we embed."""
    det = face_stack.detector.detect(crop)
    if det is None:
        return "BABA's detector found no face in the library's box"
    iou = _iou(det.bbox, target_in_crop)
    if iou < _MIN_TARGET_IOU:
        return (
            f"BABA's detector locked onto a different face than the library's "
            f"(IoU {iou:.2f}) — likely a group photo"
        )
    return None


def _spread_across_days(faces: list[OpusFace], limit: int) -> list[OpusFace]:
    """One face from each year in turn, and within a year one from each day
    in turn, newest first, until the budget is spent. Matching is
    MIN-over-references, and a third of one person's own portrait pairs lie
    further apart than the match threshold — so what a reference set needs
    is breadth across years and across days, never depth in one afternoon."""
    by_year: dict[int | None, dict[_dt.date | None, list[OpusFace]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for f in faces:
        year = f.taken_at.year if f.taken_at else None
        day = f.taken_at.date() if f.taken_at else None
        by_year[year][day].append(f)

    def newest_first(keys: list[Any]) -> list[Any]:
        known = sorted((k for k in keys if k is not None), reverse=True)
        return known + ([None] if None in keys else [])

    years = newest_first(list(by_year))
    days = {y: newest_first(list(by_year[y])) for y in years}
    cursor = dict.fromkeys(years, 0)
    picked: list[OpusFace] = []
    while len(picked) < limit and any(by_year[y][d] for y in years for d in days[y]):
        for y in years:
            for _ in range(len(days[y])):
                d = days[y][cursor[y] % len(days[y])]
                cursor[y] += 1
                if by_year[y][d]:
                    picked.append(by_year[y][d].pop(0))
                    break
            if len(picked) >= limit:
                break
    return picked


async def _enrol_from_library(
    request: Request,
    global_id: UUID,
    user: AuthUser,
    person_id: int,
    limit: int,
) -> dict[str, Any]:
    """The import proper. The caller has settled which identity and which
    library person; this fetches, cuts, verifies and persists."""
    face_stack = getattr(request.app.state, "face_stack", None)
    if face_stack is None:
        raise HTTPException(503, "face recognition is not loaded — nothing to embed an import with.")

    pool = request.app.state.pool
    client = await _client(request)
    skipped: list[str] = []
    since = _dt.date.today().replace(year=_dt.date.today().year - _YEARS_BACK)

    try:
        async with client as c:
            person_name, faces = await c.faces_for_person(
                person_id,
                since=since,
                min_px=_MIN_ORIGINAL_FACE_PX,
                per_day=_FACES_PER_DAY,
                limit=_CANDIDATE_LIMIT,
            )
            if not person_name:
                raise HTTPException(404, f"OPUS Library has no person {person_id}")

            known = {
                r["origin_ref"]
                for r in await pool.fetch(
                    "SELECT origin_ref FROM identity_reference_photos "
                    "WHERE global_id = $1 AND source = 'opus' AND origin_ref IS NOT NULL",
                    global_id,
                )
            }
            fresh = [f for f in faces if f"face:{f.id}" not in known]
            if known:
                skipped.append(f"{len(faces) - len(fresh)} face(s) already imported")
            if not fresh:
                raise HTTPException(
                    404,
                    f"no new faces for {person_name!r} in the last {_YEARS_BACK} years "
                    f"({len(known)} already imported)",
                )

            crops: list[np.ndarray] = []
            labels: list[str] = []
            refs: list[str | None] = []

            # Over-pick: some faces will lose to the size floor, the group-photo
            # guard or de-dup, and the budget should be filled from the next
            # year round rather than returned half empty.
            for face in _spread_across_days(fresh, limit * 3):
                if len(crops) >= limit:
                    break
                tag = f"face {face.id}"
                try:
                    buf = await c.download_preview(face.photo)
                except OpusError as e:
                    skipped.append(f"{tag}: {e}")
                    continue
                preview = decode_rgb_capped(buf)
                if preview is None:
                    skipped.append(f"{tag}: undecodable preview")
                    continue
                cut = _crop_around_face(preview, face)
                if cut is None:
                    skipped.append(f"{tag}: face too small in preview (<{_MIN_PREVIEW_FACE_PX}px)")
                    continue
                crop, target_in_crop = cut
                reason = await run_native(_verify_locked_on_target, face_stack, crop, target_in_crop)
                if reason is not None:
                    skipped.append(f"{tag}: {reason}")
                    continue
                crops.append(cap_long_edge(crop))
                labels.append(f"opus {tag}")
                refs.append(f"face:{face.id}")
    except OpusError as e:
        raise HTTPException(502, str(e)) from e

    if not crops:
        raise HTTPException(
            400, f"no usable faces for {person_name!r} — nothing enrolled. Reasons: {skipped}"
        )

    result = await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="opus",
        candidate_labels=labels,
        face_only=True,
        origin_refs=refs,
    )
    result["opus_person"] = person_name
    # Every skip reason, not just the count: an import is a bulk operation
    # whose diagnostic value IS the skip list — it says whether photos were
    # lost to the group-photo guard, the size floor or de-dup.
    log.info(
        "opus import: gid=%s person=%r used=%d skipped=%d by %s%s",
        global_id,
        person_name,
        result["used"],
        len(result["skipped"]),
        user.username,
        "".join(f"\n    skip: {s}" for s in result["skipped"]),
    )
    return result


class _FromOpusIn(BaseModel):
    # Omitted for an identity already linked to a library person.
    person_id: int | None = None
    limit: int = Field(default=32, ge=1, le=_MAX_PHOTOS_PER_REQUEST)


@identities_router.post("/identities/{global_id}/reference-photos/from-opus")
async def reference_photos_from_opus(
    global_id: UUID,
    payload: _FromOpusIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """More references for an existing identity from its library person.
    Choosing a person links the identity to them; a linked identity needs no
    choice. Already-imported faces are skipped, so a re-run after the library
    gained photographs enrols only the new ones."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT kind, opus_person_id FROM identity_labels WHERE global_id = $1", global_id
    )
    if row is None:
        raise HTTPException(404, f"identity {global_id} has no label — create it first")
    if row["kind"] != "person":
        raise HTTPException(400, "the library holds faces; only a person identity can enrol from it")
    person_id = payload.person_id if payload.person_id is not None else row["opus_person_id"]
    if person_id is None:
        raise HTTPException(400, "person_id is required for an identity not yet linked to the library")
    if row["opus_person_id"] not in (None, person_id):
        raise HTTPException(
            409,
            f"identity is linked to library person {row['opus_person_id']}; "
            f"unlink before enrolling from person {person_id}",
        )
    taken = await pool.fetchval(
        "SELECT global_id FROM identity_labels WHERE opus_person_id = $1 AND global_id <> $2",
        person_id,
        global_id,
    )
    if taken is not None:
        raise HTTPException(409, f"library person {person_id} already stands for identity {taken}")

    result = await _enrol_from_library(request, global_id, user, person_id, payload.limit)
    if row["opus_person_id"] is None:
        await pool.execute(
            "UPDATE identity_labels SET opus_person_id = $1 WHERE global_id = $2", person_id, global_id
        )
        result["label"]["opus_person_id"] = person_id
    return result


class _NewFromOpusIn(BaseModel):
    person_id: int
    limit: int = Field(default=32, ge=1, le=_MAX_PHOTOS_PER_REQUEST)


@identities_router.post("/identities/from-opus", status_code=201)
async def identity_from_opus(
    payload: _NewFromOpusIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """A library person becomes an identity here: named as the library names
    them, a person, a household member if they hold an account, and enrolled
    from their photographs in the same breath."""
    pool = request.app.state.pool
    taken = await pool.fetchval(
        "SELECT global_id FROM identity_labels WHERE opus_person_id = $1", payload.person_id
    )
    if taken is not None:
        raise HTTPException(409, f"library person {payload.person_id} already stands for identity {taken}")

    client = await _client(request)
    try:
        async with client as c:
            person = await c.get_person(payload.person_id)
            if person is None:
                raise HTTPException(404, f"OPUS Library has no person {payload.person_id}")
            household = await c.household_person_ids()
    except OpusError as e:
        raise HTTPException(502, str(e)) from e
    _, unlinked = await _identity_links(pool)
    twin = _same_name(person, unlinked)
    if twin is not None:
        raise HTTPException(
            409,
            f"an identity named like {person.name!r} already exists ({twin}); "
            "enrol from the library on that identity instead of creating a second one",
        )

    resident = person.id in household
    global_id = uuid4()
    row = await pool.fetchrow(
        """
        INSERT INTO identity_labels (global_id, name, kind, affiliation, resident,
                                     opus_person_id, created_by, source)
        VALUES ($1, $2, 'person', $3, $4, $5, $6, 'manual')
        RETURNING global_id, name, kind, tags, notes, plate, linked_person, affiliation, resident,
                  species, source, ai_described_at, reference_count, reference_embedding,
                  cover_photo_path, opus_person_id, created_by, created_at, updated_at
        """,
        global_id,
        person.name,
        "family" if resident else "unknown",
        resident,
        person.id,
        user.id,
    )
    log.info(
        "identity created from opus person %d %r as %s (resident=%s) by %s",
        person.id,
        person.name,
        global_id,
        resident,
        user.username,
    )
    try:
        result = await _enrol_from_library(request, global_id, user, person.id, payload.limit)
    except HTTPException as e:
        # The identity stands even when the enrolment finds nothing: the
        # operator can upload, or the library can gain photographs. Say so.
        return {
            "global_id": str(global_id),
            "label": _label_row_to_dict(row),
            "used": 0,
            "faces_detected": 0,
            "skipped": [e.detail if isinstance(e.detail, str) else str(e.detail)],
            "opus_person": person.name,
        }
    result["global_id"] = str(global_id)
    return result
