"""Identity reference (enrolment) photos."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import cv2
import numpy as np
from baba_core.face import FRONTALITY_MAX
from baba_core.paths import REFERENCE_PHOTOS
from baba_core.retention import ENROLLED
from fastapi import Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from baba_api.auth import AuthUser, current_user
from baba_api.routes_identities._base import (
    _label_row_to_dict,
    _parse_pg_vector,
    _recompute_label_aggregates,
    _vector_literal,
    identities_router,
)

log = logging.getLogger(__name__)

_MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB / photo
# Per-request cap for upload / promote-from-tracks / auto-pick. Sized for
# BODY re-ID: people are re-identified mostly by appearance across many
# poses/cameras/outfits, so a generous, diverse reference set helps recall
# (references are the *persistent* anchor; tracks only match inside the
# 7-day re-ID window). Near-duplicate frames are dropped by the dedup in
# `_persist_reference_photos`, so a high cap doesn't bloat the set.
_MAX_PHOTOS_PER_REQUEST = 64


def _decode_and_prepare(buf: bytes) -> np.ndarray | None:
    """JPEG/PNG bytes → native-aspect uint8 RGB crop (long edge <=
    MAX_CROP_EDGE) ready for the embedder + face stack. Returns None for
    unrecognised / corrupted payloads so the caller can skip them without
    failing the whole batch. MUST match the live-track crop path (embedder
    uses the same baba_core.cap_long_edge) so reference embeddings land in the
    same space as track embeddings."""
    from baba_api.imaging import decode_rgb_capped

    arr = decode_rgb_capped(buf)  # rejects decompression bombs before decode
    if arr is None:
        return None
    h, w = arr.shape[:2]
    if h < 32 or w < 32:
        return None
    from baba_core import cap_long_edge

    return cap_long_edge(arr)


def _face_box_px(bbox: tuple[float, float, float, float] | None) -> float | None:
    """Shorter side of a detected face box, in pixels of the photo it was found
    in. The shorter side, not the diagonal or the area: a face seen at an angle
    stretches along one axis, and what limits recognition is the narrow one —
    that is the axis the 112x112 ArcFace template has to be filled from."""
    if bbox is None:
        return None
    return float(min(bbox[2] - bbox[0], bbox[3] - bbox[1]))


def _norm_face_bbox(
    bbox: tuple[float, float, float, float] | None, shape: tuple[int, ...]
) -> list[float] | None:
    """Face box as 0..1 of the photo it was found in, so the gallery can zoom
    to it without knowing the JPEG's pixel size."""
    if bbox is None:
        return None
    h, w = float(shape[0]), float(shape[1])
    if h <= 0 or w <= 0:
        return None
    return [
        max(0.0, min(1.0, bbox[0] / w)),
        max(0.0, min(1.0, bbox[1] / h)),
        max(0.0, min(1.0, bbox[2] / w)),
        max(0.0, min(1.0, bbox[3] / h)),
    ]


# A reference whose face is smaller than this is refused at enrolment.
#
# Matching is MIN-over-references, which makes reference quality asymmetric: a
# reference can pull a probe under the threshold, but never push one back over
# it. So a vector computed from a face too small to carry identity cannot help
# anyone — it can only ever let a stranger in.
#
# Measured on a live 30-reference identity: 25 references sit at 47-128 px
# inter-ocular distance, and 5 — auto-picked body crops, a torso with a head in
# the corner — sit at 13-31 px against the ~35 px the template consumes. The
# held-out enrolment experiment on camera faces put the same effect in
# numbers: with no size floor, cross-identity false accepts went 0/96 -> 9/96.
#
# 40 px on the SHORTER SIDE of the face box, which lands a little under the
# 35 px inter-ocular the template wants (eyes span roughly half a face box).
# Deliberately not higher: this is the floor for "cannot possibly help", not a
# quality target.
_MIN_REFERENCE_FACE_PX = 40.0


# Cosine distance below which a new reference photo counts as a near-duplicate
# of one already enrolled. One number, two very different vector spaces — read
# both before touching it.
#
# BODY (OSNet), the original calibration: started at 0.05, which only caught
# essentially-identical frames. Subjects that haunt the same spot (a pet on its
# mat, a parked car) cluster tighter than that, so re-running auto-pick on Lumi
# kept adding 5 of 8 crops that looked indistinguishable from existing refs.
# 0.10 catches "same pose / lighting / angle, different moment" while leaving
# genuine variety through: different pose >0.10, different lighting >0.15,
# IR/night >0.20.
#
# FACE (ArcFace), measured 2026-07-15 on a 44-reference identity, 946 pairs,
# after the first Immich imports: min 0.102, p25 0.282, median 0.345, p75
# 0.422, max 0.684. Two things follow. First, 0.10 lands exactly on the edge —
# the closest surviving pair sits at 0.102 and the rejects ran 0.032-0.099, so
# the threshold cuts a continuum, not a natural gap; it was never calibrated
# for this space, it just happens to hold. It holds for a real reason though:
# matching is MIN-over-references at 0.40, so a reference 0.09 from an existing
# one adds no coverage — any probe close enough to hit the new one hits the old
# one too. Second, and more surprising: the median gap between two references
# of the SAME person (0.345) nearly reaches the match threshold, and 31% of
# pairs exceed it outright. The recognizer would not match many of these photos
# to each other. That is not a defect — it is why MIN-over-references with a
# wide, varied set is the design, and why a lone portrait recognizes almost
# nothing.
#
# If you lower this for face space, measure recall — do not reason from the
# body-space numbers above.
_REFERENCE_DEDUP_COSINE = 0.10


def _fmt_dup_distance(d: float) -> str:
    """Format cosine distance for operator-facing skip messages.
    Negative zero (-0.0 from float roundoff on identical L2-normalised
    vectors) and tiny epsilons read as "identical" instead of the
    confusing literal float."""
    if d <= 1e-4:
        return "identical"
    return f"d={d:.3f}"


def _filter_near_duplicates(
    new_embeddings: list[np.ndarray] | np.ndarray,
    existing_refs: list[np.ndarray],
    labels: list[str],
) -> tuple[list[int], list[str]]:
    """Greedy de-dup against (a) existing reference photos for this
    identity and (b) earlier rows of this same batch. Returns the
    indices of `new_embeddings` to keep, plus a list of skip messages
    keyed by the operator-visible `labels`. Vectors are assumed to be
    L2-normalised, so cosine distance = 1 - dot product."""
    kept_idx: list[int] = []
    kept_embs: list[np.ndarray] = []
    skipped: list[str] = []
    for i in range(len(new_embeddings)):
        emb = new_embeddings[i]
        dup_label: str | None = None
        for _j, ref_emb in enumerate(existing_refs):
            d = 1.0 - float(np.dot(emb, ref_emb))
            if d < _REFERENCE_DEDUP_COSINE:
                dup_label = f"existing reference ({_fmt_dup_distance(d)})"
                break
        if dup_label is None:
            for kidx, kemb in zip(kept_idx, kept_embs, strict=True):
                d = 1.0 - float(np.dot(emb, kemb))
                if d < _REFERENCE_DEDUP_COSINE:
                    dup_label = f"already-selected {labels[kidx]} ({_fmt_dup_distance(d)})"
                    break
        if dup_label is not None:
            skipped.append(f"{labels[i]}: near-duplicate of {dup_label}")
        else:
            kept_idx.append(i)
            kept_embs.append(emb)
    return kept_idx, skipped


# Where reference photo originals land. Relative to media root so a
# remount doesn't break paths. Served by routes_thumbnails via the
# `_serve_media_subdir` helper.
_REFERENCE_PHOTOS_DIR_REL = REFERENCE_PHOTOS


async def _no_op_result(
    pool: Any,
    global_id: UUID,
    user: AuthUser,
    source: str,
    skipped: list[str],
) -> dict[str, Any]:
    """Nothing survived the filters — every candidate was a duplicate or
    faceless. Return the same shape a successful call does so the UI can
    report what happened without a special case. Aggregates didn't move, so
    the label is read as-is."""
    row = await pool.fetchrow(
        """
        SELECT global_id, name, kind, tags, notes, plate, source,
               ai_described_at, reference_count, reference_embedding,
               cover_photo_path,
               created_by, created_at, updated_at
        FROM identity_labels WHERE global_id = $1
        """,
        global_id,
    )
    log.info(
        "reference photos no-op: gid=%s source=%s nothing kept (%d skipped) by %s",
        global_id,
        source,
        len(skipped),
        user.username,
    )
    return {
        "label": _label_row_to_dict(row) if row else None,
        "used": 0,
        "faces_detected": 0,
        "skipped": skipped,
    }


@dataclass
class _Candidates:
    """The photos still in the running, every list positionally aligned with
    `crops`, so a filter drops a photo from all of them at once."""

    crops: list[np.ndarray]
    labels: list[str]
    origin_refs: list[str | None]
    body: list[np.ndarray | None]
    face: list[np.ndarray | None]
    face_px: list[float | None]
    face_bbox: list[list[float] | None]

    def keep(self, idx: Iterable[int]) -> _Candidates:
        idx = list(idx)
        return _Candidates(
            **{f.name: [getattr(self, f.name)[i] for i in idx] for f in fields(self)}
        )

    def drop_face(self, i: int) -> None:
        self.face[i] = None
        self.face_px[i] = None
        self.face_bbox[i] = None

    def small_faces(self) -> list[int]:
        """Too small counts as no usable face: storing it would add a vector
        that can only ever admit a stranger (MIN-over-references)."""
        return [
            i
            for i, (v, px) in enumerate(zip(self.face, self.face_px, strict=True))
            if v is not None and px is not None and px < _MIN_REFERENCE_FACE_PX
        ]


def _embed_faces(
    face_stack: Any, crops: list[np.ndarray]
) -> tuple[list[np.ndarray | None], list[float | None], list[list[float] | None]]:
    """YuNet + ArcFace per crop is blocking ONNX work; callers run this off
    the loop."""
    vs: list[np.ndarray | None] = []
    ps: list[float | None] = []
    bs: list[list[float] | None] = []
    for c in crops:
        r = face_stack.embed_from_crop(c)
        vs.append(r[0] if r is not None else None)
        ps.append(_face_box_px(r[1]) if r is not None else None)
        bs.append(_norm_face_bbox(r[1], c.shape) if r is not None else None)
    return vs, ps, bs


def _enrolment_face_stack(state: Any, face_only: bool) -> Any:
    """Fail loud: if the face stack is down because its GPU init FAILED (models
    are present but the accelerator broke on a require-GPU build), refuse the
    enrollment instead of silently storing a face-less reference that would
    never match by face. A genuinely absent face stack (no error) is the
    legitimate body-only mode and yields None."""
    face_stack_error = getattr(state, "face_stack_error", None)
    if face_stack_error:
        raise HTTPException(
            503,
            "face recognition is temporarily unavailable (model failed to "
            f"initialise on the accelerator: {face_stack_error}). Reference "
            "photos were not saved — retry once the GPU is healthy.",
        )
    face_stack = getattr(state, "face_stack", None)
    if face_only and face_stack is None:
        raise HTTPException(
            503,
            "face recognition is not loaded — a face-only import has nothing "
            "to embed with. Check that the face models are present under "
            "/models (services/embedder/scripts/fetch_face_models.sh).",
        )
    return face_stack


async def _face_only_candidates(
    face_stack: Any,
    c: _Candidates,
    pre_face_embeddings: list[np.ndarray] | None,
    pre_face_px: list[float | None] | None,
) -> tuple[_Candidates, list[str]]:
    """Face vectors first: they're both the payload AND the dedup key here,
    and crops without a face are dropped before anything else is spent on
    them.

    `pre_face_embeddings` is the face-side twin of `pre_embeddings`, and it is
    not just an optimisation. The pipeline path enrolls the ALIGNED 112x112
    face the embedder already produced, whose vector is already stored —
    re-running detect+embed on an aligned face means asking SCRFD to find a
    face in an image that is nothing but a face, cropped to the eyes-to-chin
    template. It often fails outright, and when it succeeds it returns a
    vector that drifts from the stored one (JPEG round-trip plus a second
    alignment on top of the first), which is exactly the dedup-boundary
    problem documented for the body path."""
    n = len(c.crops)
    if pre_face_embeddings is not None and len(pre_face_embeddings) == n:
        c.face = list(pre_face_embeddings)
        if pre_face_px is not None and len(pre_face_px) == n:
            c.face_px = list(pre_face_px)
        # These crops ARE the aligned face, so the box is the whole photo.
        c.face_bbox = [[0.0, 0.0, 1.0, 1.0] if v is not None else None for v in c.face]
    else:
        c.face, c.face_px, c.face_bbox = await asyncio.to_thread(
            _embed_faces, face_stack, c.crops
        )
    skipped = [
        f"{c.labels[i]}: no face found by BABA's detector"
        for i, v in enumerate(c.face)
        if v is None
    ]
    for i in c.small_faces():
        skipped.append(
            f"{c.labels[i]}: face too small to identify "
            f"({c.face_px[i]:.0f} px, need {_MIN_REFERENCE_FACE_PX:.0f})"
        )
        c.drop_face(i)
    return c.keep(i for i, v in enumerate(c.face) if v is not None), skipped


async def _body_vectors(
    backend: Any, crops: list[np.ndarray], pre_embeddings: list[np.ndarray] | None
) -> list[np.ndarray | None]:
    """Body embedding per photo, each L2-normalised already by the OSNet
    backend wrapper. Computed BEFORE dedup so we can compare against existing
    references, and BEFORE face embedding so we don't waste GPU time on crops
    we're about to drop. Skipped entirely when the caller already has
    authoritative embeddings (auto-pick path)."""
    if pre_embeddings is not None and len(pre_embeddings) == len(crops):
        return list(pre_embeddings)
    if backend is None:
        raise HTTPException(
            503,
            "reference-photo enrollment is disabled — BABA_EMBEDDER_MODEL "
            "is not pointing at a loadable ONNX file",
        )
    # ONNX inference is synchronous and can take hundreds of ms for a batch of
    # 64 — run it off the event loop so the whole API doesn't stall for the
    # duration (the embedder service does the same).
    return await asyncio.to_thread(backend.embed, crops)


async def _drop_near_duplicates(
    pool: Any, global_id: UUID, c: _Candidates, face_only: bool
) -> tuple[_Candidates, list[str]]:
    """De-dup against (a) photos already enrolled for this identity and (b)
    near-duplicates within this same batch. Two crops from adjacent frames of
    the same track contribute almost identical vectors — averaging them just
    narrows the centroid without widening recall, so dropping them keeps the
    photo budget spent on actual viewpoint variety. A face-only import de-dups
    on the face, because body vectors of face crops are all alike."""
    column = "face_embedding" if face_only else "body_embedding"
    vectors = [v for v in (c.face if face_only else c.body) if v is not None]
    existing_rows = await pool.fetch(
        f"SELECT {column} AS v FROM identity_reference_photos "  # noqa: S608
        f"WHERE global_id = $1 AND {column} IS NOT NULL",
        global_id,
    )
    existing_refs = [_parse_pg_vector(r["v"]) for r in existing_rows]
    kept_idx, skipped = _filter_near_duplicates(vectors, existing_refs, c.labels)
    return c.keep(kept_idx), skipped


async def _faces_on_bodies(
    face_stack: Any, c: _Candidates, person_identity: bool
) -> tuple[_Candidates, list[str]]:
    """Face embedding per body photo (null when no face detected). A body
    reference stays useful even when its face is unusable, but the face vector
    must not be stored, for the same MIN-over-references reason — this is what
    a torso crop with a head in the corner hits.

    A BODY reference for a PERSON is only enrolled when the same photo also
    yielded a usable face. Body appearance is not identity: it is outfit and
    build, which is exactly why naming people by body is forbidden everywhere
    else in this system (the over-merge vortex). A torso crop with no readable
    face is a photo of SOMEBODY — nothing in it proves it is this person, and
    once enrolled it widens the identity's body centroid toward whoever it
    actually was. A face in the same frame is the proof, so it is the
    condition. Pets and vehicles are exempt by nature: they have no face, body
    IS their only signal, and their references are enrolled deliberately per
    subject."""
    c.face, c.face_px, c.face_bbox = await asyncio.to_thread(
        _embed_faces, face_stack, c.crops
    )
    for i in c.small_faces():
        log.info(
            "reference photo %s: face %.0f px below the %.0f px floor — "
            "kept as a body reference, face vector dropped",
            c.labels[i],
            c.face_px[i],
            _MIN_REFERENCE_FACE_PX,
        )
        c.drop_face(i)
    if not person_identity:
        return c, []
    skipped = [
        f"{c.labels[i]}: no usable face in this photo, so it "
        "cannot be trusted as a body reference for a person"
        for i, v in enumerate(c.face)
        if v is None
    ]
    return c.keep(i for i, v in enumerate(c.face) if v is not None), skipped


def _write_reference_jpegs(media_root: Path, crops: list[np.ndarray]) -> list[tuple[int, UUID, str]]:
    """Persist originals to media/reference_photos/<photo_id>.jpg so the
    operator can review them and delete individual photos later. The
    native-aspect processed crop is what the embedder saw; full-res original
    is dropped because UI preview doesn't need it and storing it would
    duplicate the track-sample crop bytes for the from-tracks path. Returns
    (candidate index, photo id, relative path) for every file written."""
    (media_root / _REFERENCE_PHOTOS_DIR_REL).mkdir(parents=True, exist_ok=True)
    saved: list[tuple[int, UUID, str]] = []
    for i, crop in enumerate(crops):
        pid = uuid4()
        rel = f"{_REFERENCE_PHOTOS_DIR_REL}/{pid}.jpg"
        abs_path = media_root / rel
        try:
            written = cv2.imwrite(
                str(abs_path),
                cv2.cvtColor(crop, cv2.COLOR_RGB2BGR),
                [int(cv2.IMWRITE_JPEG_QUALITY), 90],
            )
        except cv2.error:
            log.exception("failed to write reference photo jpeg %s", abs_path)
            continue
        if not written:
            log.error("failed to write reference photo jpeg %s", abs_path)
            continue
        saved.append((i, pid, rel))
    return saved


async def _insert_reference_rows(
    conn: Any,
    global_id: UUID,
    user: AuthUser,
    source: str,
    c: _Candidates,
    saved: list[tuple[int, UUID, str]],
    face_model_key: str | None,
) -> Any:
    # Ensure a label row exists so the photos have a sensible parent. The
    # aggregate fields get filled below by recompute.
    existing = await conn.fetchrow(
        "SELECT 1 FROM identity_labels WHERE global_id = $1",
        global_id,
    )
    if existing is None:
        await conn.execute(
            """
                INSERT INTO identity_labels (global_id, name, created_by)
                VALUES ($1, $2, $3)
                """,
            global_id,
            f"Identitet {str(global_id)[:8]}",
            user.id,
        )
    # ON CONFLICT DO NOTHING makes a re-import idempotent against the partial
    # unique index on (global_id, origin_ref) — the pre-filter in the import
    # path already skips known records, but a concurrent import of the same
    # person would otherwise race past it and duplicate rows. The face vector
    # is stamped with the model that made it, so the coverage count on the
    # face-recognition card can tell a reference in the active model's space
    # from one that needs recomputing.
    for i, pid, rel in saved:
        body_v, face_v = c.body[i], c.face[i]
        await conn.execute(
            """
                INSERT INTO identity_reference_photos
                    (id, global_id, photo_path, body_embedding, face_embedding,
                     face_embedding_model, uploaded_by, source, origin_ref,
                     face_px, face_bbox)
                VALUES ($1, $2, $3, $4::vector, $5::vector, $6, $7, $8, $9, $10, $11)
                ON CONFLICT DO NOTHING
                """,
            pid,
            global_id,
            rel,
            _vector_literal(body_v) if body_v is not None else None,
            _vector_literal(face_v) if face_v is not None else None,
            face_model_key if face_v is not None else None,
            user.id,
            source,
            c.origin_refs[i],
            c.face_px[i],
            c.face_bbox[i],
        )
    # Recompute the canonical averaged embeddings from all surviving photos
    # (this upload's plus any previous ones).
    await _recompute_label_aggregates(conn, global_id)
    # Enrollment promotes this identity into the 90-day retention tier — bump
    # every existing track that hasn't already got a later deadline so the
    # pruner doesn't wipe them out at the next sweep. New tracks inherit the
    # longer tier automatically via the event-manager finalize path. Using
    # GREATEST avoids accidentally shortening any deadline that's already set
    # further out (e.g. if an operator manually extended one).
    await conn.execute(
        """
            UPDATE tracks
               SET retain_until = GREATEST(
                   COALESCE(retain_until, ended_at),
                   ended_at + $2::interval
               )
             WHERE global_id = $1
            """,
        global_id,
        ENROLLED,
    )
    return await conn.fetchrow(
        """
            SELECT global_id, name, kind, tags, notes, plate, source,
                   ai_described_at, reference_count, reference_embedding,
                   cover_photo_path,
                   created_by, created_at, updated_at
            FROM identity_labels WHERE global_id = $1
            """,
        global_id,
    )


async def _persist_reference_photos(
    request: Request,
    global_id: UUID,
    user: AuthUser,
    crops: list[np.ndarray],
    skipped: list[str],
    source: str,
    candidate_labels: list[str] | None = None,
    pre_embeddings: list[np.ndarray] | None = None,
    pre_face_embeddings: list[np.ndarray] | None = None,
    pre_face_px: list[float | None] | None = None,
    pre_face_model_key: str | None = None,
    face_only: bool = False,
    origin_refs: list[str | None] | None = None,
) -> dict[str, Any]:
    """Shared embed → de-dup → save jpeg → insert rows → recompute
    aggregates path. `crops` are native-aspect RGB uint8 ndarrays (long edge
    capped) prepared by `_decode_and_prepare`. `source` tags the row's
    provenance (`identity_reference_photos.source`) and the log line, so we
    can tell file uploads from track promotions from external imports.
    `candidate_labels` is the operator-visible identifier per crop used in
    skip messages (filename for uploads, "track <short>" for from-tracks);
    defaults to "crop N" when not provided.

    `pre_embeddings` lets a caller pass already-computed body vectors
    (one per crop). Auto-pick uses this to thread the stored
    `tracks.embedding` straight through — without it, re-embedding
    the same JPEG produces a vector that drifts by ~0.001 (JPEG
    round-trip), which crosses the dedup threshold
    boundary and gives different verdicts at pre-filter vs persist
    time. With it, the pre-filter's decision is authoritative.

    `face_only` switches the whole modality for callers whose crops are
    FACES rather than bodies (the library import). Two things change, and
    both are load-bearing:

      - No body vector is computed or stored. OSNet embeds a person's
        build+clothing from a 256×128 body crop; run over a head-and-
        shoulders crop it produces a vector describing "a face-shaped
        blob", which is meaningless. It would not be harmlessly ignored
        either: the merge picker's `near=` ranking (see
        routes_identities/_core.py) takes MIN over a candidate's
        reference-photo body vectors, so a portrait's junk vector would
        pollute that ranking, and it would drag the averaged body
        centroid off whatever the real body references say.

      - De-dup runs on the FACE vector. This is the subtle one: the
        default path de-dups on body, and body vectors of face crops are
        all mutually similar, so the existing filter would declare almost
        every imported photo a near-duplicate of the first one and throw
        the import away. Face-space de-dup is also what we actually want
        here — the point of importing is face variety.

    In `face_only` mode a crop with no detectable face is dropped: with
    body NULL it would persist a row that contributes to nothing.

    `origin_refs` carries the external record per crop (`face:<id>` for
    the library import), positionally aligned with `crops`, for import
    idempotency."""
    state = request.app.state
    pool = state.pool
    face_stack = _enrolment_face_stack(state, face_only)
    face_model_key = getattr(state, "face_model_key", None)
    if pre_face_embeddings is not None and pre_face_model_key != face_model_key:
        raise HTTPException(409, "face model changed during enrolment; retry the request")
    n = len(crops)
    c = _Candidates(
        crops=crops,
        labels=candidate_labels if candidate_labels is not None else [f"crop {i + 1}" for i in range(n)],
        origin_refs=origin_refs if origin_refs is not None else [None] * n,
        body=[None] * n,
        face=[None] * n,
        face_px=[None] * n,
        face_bbox=[None] * n,
    )

    if face_only:
        c, dropped = await _face_only_candidates(face_stack, c, pre_face_embeddings, pre_face_px)
        skipped = [*skipped, *dropped]
    else:
        # getattr, not attribute access: a face_only import never touches the
        # body embedder, so an instance whose OSNet failed to load can still
        # enroll from an external library.
        c.body = await _body_vectors(getattr(state, "embedder_backend", None), crops, pre_embeddings)
    if not c.crops:
        return await _no_op_result(pool, global_id, user, source, skipped)

    c, dropped = await _drop_near_duplicates(pool, global_id, c, face_only)
    skipped = [*skipped, *dropped]
    if not c.crops:
        return await _no_op_result(pool, global_id, user, source, skipped)

    kind_row = await pool.fetchrow(
        "SELECT kind FROM identity_labels WHERE global_id = $1", global_id
    )
    person_identity = (kind_row["kind"] if kind_row else None) == "person"
    if person_identity and not face_only and face_stack is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "face stack unavailable, so body references for a person cannot "
                "be checked for the face that would justify them"
            ),
        )
    if not face_only and face_stack is not None:
        c, dropped = await _faces_on_bodies(face_stack, c, person_identity)
        skipped = [*skipped, *dropped]
        if not c.crops:
            return await _no_op_result(pool, global_id, user, source, skipped)
    n_faces = sum(1 for v in c.face if v is not None)

    saved = await asyncio.to_thread(_write_reference_jpegs, state.config.media_path, c.crops)
    async with pool.acquire() as conn, conn.transaction():
        row = await _insert_reference_rows(
            conn, global_id, user, source, c, saved, face_model_key
        )

    log.info(
        "reference photos added: gid=%s source=%s saved=%d faces=%d skipped=%d by %s",
        global_id,
        source,
        len(saved),
        n_faces,
        len(skipped),
        user.username,
    )
    return {
        "label": _label_row_to_dict(row),
        "used": len(saved),
        "faces_detected": n_faces,
        "skipped": skipped,
    }


@identities_router.post("/identities/{global_id}/reference-photos")
async def upload_reference_photos(
    global_id: UUID,
    request: Request,
    files: list[UploadFile] = File(...),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Embed each uploaded image, average the per-photo vectors, and
    store the result in `identity_labels.reference_embedding`. If the
    identity has no label row yet, a stub label named after its short
    UUID is created so there's something to attach the embedding to —
    the user can rename it afterwards."""
    backend = getattr(request.app.state, "embedder_backend", None)
    if backend is None:
        raise HTTPException(
            503,
            "reference-photo enrollment is disabled — BABA_EMBEDDER_MODEL "
            "is not pointing at a loadable ONNX file",
        )
    if not files:
        raise HTTPException(400, "no files provided")
    if len(files) > _MAX_PHOTOS_PER_REQUEST:
        raise HTTPException(
            400,
            f"too many files ({len(files)} > {_MAX_PHOTOS_PER_REQUEST})",
        )

    crops: list[np.ndarray] = []
    skipped: list[str] = []
    for f in files:
        # Bound per-file read so a 4 GB POST doesn't pin the process.
        buf = await f.read(_MAX_PHOTO_BYTES + 1)
        if len(buf) > _MAX_PHOTO_BYTES:
            skipped.append(f"{f.filename}: too large")
            continue
        crop = _decode_and_prepare(buf)
        if crop is None:
            skipped.append(f"{f.filename}: undecodable / too small")
            continue
        crops.append(crop)

    if not crops:
        raise HTTPException(400, f"no usable images (skipped: {skipped})")

    return await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="upload",
    )


class _FromTracksIn(BaseModel):
    """Track IDs whose representative `crop_path` jpegs should be turned
    into reference photos for this identity. Each track contributes one
    photo (the same crop the gallery shows)."""

    track_ids: list[UUID] = Field(..., min_length=1, max_length=_MAX_PHOTOS_PER_REQUEST)


@identities_router.post("/identities/{global_id}/reference-photos/from-tracks")
async def reference_photos_from_tracks(
    global_id: UUID,
    payload: _FromTracksIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Promote existing track crops into reference photos. Reuses the
    same embed-and-store pipeline as multipart upload — we already saved
    the jpeg on the original detection, this just copies it into the
    reference set and computes the canonical embeddings off it.

    Operator picks tracks from the identity's gallery and clicks
    "promote"; the per-track `crop_path` jpeg is read off disk, prepared
    (native aspect, long edge capped) by `_decode_and_prepare`, embedded by
    OSNet (and the face stack if loaded), then inserted into
    `identity_reference_photos`. Saves the operator the download/upload
    dance when the system has already captured exactly the photos they
    want to enroll."""
    backend = getattr(request.app.state, "embedder_backend", None)
    if backend is None:
        raise HTTPException(
            503,
            "reference-photo enrollment is disabled — BABA_EMBEDDER_MODEL "
            "is not pointing at a loadable ONNX file",
        )

    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT id, crop_path FROM tracks
        WHERE id = ANY($1::uuid[])
          AND crop_path IS NOT NULL AND crop_path <> ''
        """,
        payload.track_ids,
    )
    if not rows:
        raise HTTPException(
            404,
            "none of the requested tracks have a crop_path on disk",
        )

    # Preserve the operator's chosen order so the "first track" comment
    # in the gallery matches the first reference photo. asyncpg returns
    # rows in DB order, not input order, so we re-key.
    by_id = {r["id"]: r["crop_path"] for r in rows}
    ordered = [(tid, by_id[tid]) for tid in payload.track_ids if tid in by_id]
    missing = [str(tid) for tid in payload.track_ids if tid not in by_id]

    media_root = request.app.state.config.media_path
    crops: list[np.ndarray] = []
    labels: list[str] = []
    skipped: list[str] = [f"{tid}: no crop_path on track" for tid in missing]
    for tid, rel in ordered:
        tid_short = str(tid)[:8]
        src = media_root / rel
        try:
            buf = src.read_bytes()
        except FileNotFoundError:
            skipped.append(f"track {tid_short}: crop file missing on disk")
            continue
        except OSError as e:
            skipped.append(f"track {tid_short}: read failed ({e.strerror or 'OSError'})")
            continue
        crop = _decode_and_prepare(buf)
        if crop is None:
            skipped.append(f"track {tid_short}: undecodable / too small")
            continue
        crops.append(crop)
        labels.append(f"track {tid_short}")

    if not crops:
        raise HTTPException(400, f"no usable crops (skipped: {skipped})")

    return await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="from-tracks",
        candidate_labels=labels,
    )


class _AutoSelectIn(BaseModel):
    """Server picks `count` track crops that maximise visual variety
    (farthest-point sampling over body embeddings) above the quality
    floor. `min_observations` drops single-frame flashes — a real
    subject is normally tracked for many consecutive frames; isolated
    blips are usually detector noise that contributes a noisy
    embedding."""

    count: int = Field(default=8, ge=1, le=_MAX_PHOTOS_PER_REQUEST)
    min_observations: int = Field(default=3, ge=1)


@identities_router.post("/identities/{global_id}/reference-photos/auto-select")
async def reference_photos_auto_select(
    global_id: UUID,
    payload: _AutoSelectIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Auto-build a reference set for this identity.

    Gathers every track tied to the identity (filtered by
    `min_confidence`), then runs greedy farthest-point sampling over
    the per-track body embeddings — seed = highest confidence, each
    subsequent pick maximises the minimum cosine distance to the
    already-chosen set. The result is a small, visually diverse set
    of crops that covers as many viewpoints as the captured tracks
    offer. The shared persist path then de-dupes against any existing
    reference photos so re-running this on an identity that already
    has a partial enrollment just tops it up with new variety.

    Use this to bootstrap pets / vehicles after they've accumulated
    enough tracks but the operator hasn't curated a reference set
    yet — much less clicking than checkbox-select-and-promote."""
    backend = getattr(request.app.state, "embedder_backend", None)
    if backend is None:
        raise HTTPException(
            503,
            "reference-photo enrollment is disabled — BABA_EMBEDDER_MODEL "
            "is not pointing at a loadable ONNX file",
        )

    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT id, crop_path, embedding::text AS emb_text, n_observations
        FROM tracks
        WHERE global_id = $1
          AND embedding IS NOT NULL
          AND crop_path IS NOT NULL AND crop_path <> ''
          AND n_observations >= $2
        """,
        global_id,
        payload.min_observations,
    )
    if not rows:
        raise HTTPException(
            404,
            f"no tracks for identity {global_id} with crop_path and "
            f"n_observations >= {payload.min_observations}",
        )

    # Parse all embeddings up front so the farthest-point loop is pure
    # numpy. Vectors come back from asyncpg as text; backend.embed() and
    # the embedder service both L2-normalise so dot product = cosine sim.
    candidates: list[dict[str, Any]] = []
    for r in rows:
        candidates.append(
            {
                "track_id": r["id"],
                "crop_path": r["crop_path"],
                "embedding": _parse_pg_vector(r["emb_text"]),
                "n_observations": int(r["n_observations"]),
            }
        )

    # Pre-filter against the existing reference set. Without this, a
    # re-run of auto-pick spends the whole `count` budget on tracks
    # that are already enrolled (the farthest-point algorithm picks
    # the same tracks since the pool didn't change), then the
    # downstream persist dedup throws them all out with confusing
    # "near-duplicate (d=0.000)" messages. Dropping them up front
    # gives the farthest-point a clean pool of genuinely-new variety,
    # and surfaces a clear "no new candidates" 200 when there are none.
    existing_rows = await pool.fetch(
        "SELECT body_embedding FROM identity_reference_photos "
        "WHERE global_id = $1 AND body_embedding IS NOT NULL",
        global_id,
    )
    existing_refs = [_parse_pg_vector(r["body_embedding"]) for r in existing_rows]
    if existing_refs:
        ref_mat = np.stack(existing_refs)  # (n_ref, d)
        filtered: list[dict[str, Any]] = []
        for c in candidates:
            # min distance to any existing reference
            d_min = float((1.0 - ref_mat @ c["embedding"]).min())
            if d_min >= _REFERENCE_DEDUP_COSINE:
                filtered.append(c)
        n_pruned = len(candidates) - len(filtered)
        candidates = filtered
        if not candidates:
            # Everything we could pick is already enrolled — return a
            # clean no-op response instead of forcing the operator to
            # parse N×"near-duplicate" lines.
            row = await pool.fetchrow(
                """
                SELECT global_id, name, kind, tags, notes, plate, source,
                       ai_described_at, reference_count, reference_embedding,
                       cover_photo_path,
                       created_by, created_at, updated_at
                FROM identity_labels WHERE global_id = $1
                """,
                global_id,
            )
            log.info(
                "auto-select: gid=%s nothing new (all %d tracks already enrolled) by %s",
                global_id,
                n_pruned,
                user.username,
            )
            return {
                "label": _label_row_to_dict(row) if row else None,
                "used": 0,
                "faces_detected": 0,
                "skipped": [
                    f"all {n_pruned} candidate track(s) already enrolled "
                    f"as reference photos — nothing new to add",
                ],
            }

    # Greedy farthest-point sampling: seed with the most-observed track
    # (longest stable detection — typically the cleanest crop of the
    # subject), then iteratively add the remaining candidate whose
    # nearest already-selected neighbour is the farthest — i.e. the one
    # that adds the most NEW visual information to the set. Stops at
    # `count` or when the pool runs out (small identities legitimately
    # have <count candidates).
    remaining = list(candidates)
    seed_idx = max(range(len(remaining)), key=lambda i: remaining[i]["n_observations"])
    selected = [remaining.pop(seed_idx)]
    target = min(payload.count, 1 + len(remaining))
    while len(selected) < target and remaining:
        sel_mat = np.stack([s["embedding"] for s in selected])  # (k, d)
        rem_mat = np.stack([c["embedding"] for c in remaining])  # (m, d)
        # (m, k) cosine distances assuming both sides are unit-norm.
        dists = 1.0 - rem_mat @ sel_mat.T
        min_dists = dists.min(axis=1)  # closest selected neighbour per candidate
        idx = int(np.argmax(min_dists))
        selected.append(remaining.pop(idx))

    # Load + decode the chosen crops. Skip silently into `skipped` if a
    # file is missing — happens if the media tier was pruned between
    # the track finalizing and this call. We also keep the stored
    # embedding parallel to each loaded crop so the persist path can
    # bypass re-embedding (see `pre_embeddings` in
    # `_persist_reference_photos`).
    media_root = request.app.state.config.media_path
    crops: list[np.ndarray] = []
    labels: list[str] = []
    embeddings: list[np.ndarray] = []
    skipped: list[str] = []
    for s in selected:
        tid_short = str(s["track_id"])[:8]
        src = media_root / s["crop_path"]
        try:
            buf = src.read_bytes()
        except FileNotFoundError:
            skipped.append(f"track {tid_short}: crop file missing on disk")
            continue
        except OSError as e:
            skipped.append(f"track {tid_short}: read failed ({e.strerror or 'OSError'})")
            continue
        crop = _decode_and_prepare(buf)
        if crop is None:
            skipped.append(f"track {tid_short}: undecodable / too small")
            continue
        crops.append(crop)
        labels.append(f"track {tid_short}")
        embeddings.append(s["embedding"])

    if not crops:
        raise HTTPException(400, f"no usable crops (skipped: {skipped})")

    return await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="auto-select",
        candidate_labels=labels,
        pre_embeddings=embeddings,
    )


class _FromFaceSamplesIn(BaseModel):
    """Enrol the identity's own CCTV faces as references.

    `min_face_score` is the SCRFD detection score, which is genuinely
    predictive of match quality on this deployment (measured over 7 days:
    samples below 0.60 match a named identity 17% of the time, 0.70-0.77 64%,
    above 0.77 74%). 0.77 is the boundary where a track went from never
    tagged to always tagged."""

    count: int = Field(default=8, ge=1, le=_MAX_PHOTOS_PER_REQUEST)
    min_face_score: float = Field(default=0.77, ge=0.0, le=1.0)
    # `min_face_px` is NOT cosmetic — it is what makes this endpoint safe.
    #
    # Held-out measurement (enrol from half an identity's face-verified
    # tracks, evaluate on the other half, 2026-07-25): enrolling CCTV faces
    # with no size floor raised recall a little AND raised cross-identity
    # false accepts from 0/96 to 9/96. Two low-resolution faces off the same
    # camera resemble EACH OTHER more than either resembles its subject, so a
    # small-face reference becomes an attractor for anyone at that distance —
    # and MIN-over-references means one such vector can only ever cause a
    # false accept, never prevent one.
    #
    # At a 60 px floor the added false accepts go to 0/96 while the recall
    # gain survives (median distance 0.494 -> 0.334 on the held-out half).
    # Below 60 the enrollment is actively harmful; that is why the floor
    # defaults on rather than off.
    min_face_px: float = Field(default=60.0, ge=0.0)


@identities_router.post("/identities/{global_id}/reference-photos/from-face-samples")
async def reference_photos_from_face_samples(
    global_id: UUID,
    payload: _FromFaceSamplesIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Enrol this identity's best CCTV faces as reference photos.

    Every face reference on this deployment comes from an imported portrait
    library, so the reference side lives in a different visual domain than
    anything the cameras produce — the recognition that does succeed does so
    despite a portrait-vs-surveillance gap on every comparison. This closes it
    with the system's own evidence: the faces it already detected, aligned and
    embedded, on tracks whose identity was confirmed BY FACE.

    Three things make this cheap and safe where the existing `from-tracks`
    promotion is neither:

      - It reads `track_embedding_samples`, not `tracks.crop_path`. The latter
        is the 512-capped q85 gallery BODY jpeg, so promoting it means asking
        the detector to find the face again in a re-compressed, downscaled
        image — which is why 6 of 7 existing from-track references ended up
        with `face_embedding IS NULL`. The sample already carries the aligned
        112x112 face and its vector.
      - It threads that stored vector through as `pre_face_embeddings`, so the
        enrolled reference is bit-identical to what the matcher saw when it
        recognised the track. Re-embedding would drift it across the dedup
        boundary.
      - It only considers tracks with `face_verified`, i.e. ones a face match
        already confirmed. Body-clustered or operator-named tracks are
        excluded on purpose: enrolling a mis-assigned face would poison every
        future comparison, and MIN-over-references means one bad vector can
        only ever cause false accepts, never prevent them.

    Best sample per track (`DISTINCT ON`) rather than the global top N, so a
    single long track can't fill the whole budget with near-identical frames;
    the shared persist path then de-dups what remains."""
    face_stack = getattr(request.app.state, "face_stack", None)
    face_model_key = getattr(request.app.state, "face_model_key", None)
    if face_stack is None or not face_model_key:
        raise HTTPException(
            503,
            "face recognition is not loaded — there is no embedder space to "
            "enrol stored face vectors into",
        )

    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (s.track_id)
               s.track_id, s.face_crop_path, s.face_embedding, s.face_score, s.face_px
        FROM track_embedding_samples s
        JOIN tracks t ON t.id = s.track_id
        WHERE t.global_id = $1
          AND t.face_verified
          AND s.face_embedding IS NOT NULL
          AND s.face_crop_path IS NOT NULL AND s.face_crop_path <> ''
          AND s.face_score >= $2
          AND s.face_embedding_model = $3
          AND COALESCE(s.face_px, 0) >= $4
          -- Everywhere else these only demote; here they exclude. What comes
          -- out of this query is offered as CURATED EVIDENCE, and a name may
          -- be given from nothing else — so a beam of the awning must not
          -- reach the operator's approve button wearing a thumbnail, and
          -- neither must an ear, which the residual alone lets past. Nothing
          -- real is lost: of the enrolled portraits not one sits above the
          -- residual, and 161 of 162 survive their own alignment.
          AND COALESCE(s.face_frontality, 0) <= $5
          AND s.face_realigned IS NOT FALSE
        ORDER BY s.track_id,
                 CASE WHEN s.face_realigned THEN 0 ELSE 1 END,
                 s.face_score DESC
        """,
        global_id,
        payload.min_face_score,
        face_model_key,
        payload.min_face_px,
        FRONTALITY_MAX,
    )
    if not rows:
        raise HTTPException(
            404,
            "no stored face samples qualify — this identity has no "
            f"face-verified track carrying a {face_model_key} vector at "
            f"score >= {payload.min_face_score} and face >= "
            f"{payload.min_face_px:.0f} px",
        )

    # Best faces first, then cap: DISTINCT ON already guarantees one per track.
    ranked = sorted(rows, key=lambda r: r["face_score"], reverse=True)[: payload.count]

    media_root = request.app.state.config.media_path
    crops: list[np.ndarray] = []
    vecs: list[np.ndarray] = []
    pxs: list[float | None] = []
    labels: list[str] = []
    skipped: list[str] = []
    for r in ranked:
        track_short = str(r["track_id"])[:8]
        label = f"track {track_short} (score {r['face_score']:.2f}, {r['face_px'] or 0:.0f} px)"
        try:
            buf = (media_root / r["face_crop_path"]).read_bytes()
        except FileNotFoundError:
            skipped.append(f"{label}: aligned face crop missing on disk")
            continue
        except OSError as e:
            skipped.append(f"{label}: read failed ({e.strerror or 'OSError'})")
            continue
        crop = _decode_and_prepare(buf)
        if crop is None:
            skipped.append(f"{label}: undecodable / too small")
            continue
        # parse_vector yields a zero-length array (never None) on an
        # unparseable/empty column — a 0-d vector would sail through dedup and
        # persist as a reference that can never match.
        vec = _parse_pg_vector(r["face_embedding"])
        if vec.size == 0:
            skipped.append(f"{label}: stored face vector unreadable")
            continue
        crops.append(crop)
        vecs.append(vec)
        # The face size that matters is the one in the ORIGINAL frame, not in
        # the 112x112 aligned crop we are storing — that one is always 112.
        pxs.append(float(r["face_px"]) if r["face_px"] is not None else None)
        labels.append(label)

    if not crops:
        raise HTTPException(400, f"no usable face samples (skipped: {skipped})")

    return await _persist_reference_photos(
        request,
        global_id,
        user,
        crops,
        skipped,
        source="from-face-samples",
        candidate_labels=labels,
        pre_face_embeddings=vecs,
        pre_face_px=pxs,
        pre_face_model_key=face_model_key,
        face_only=True,
    )


@identities_router.get("/identities/{global_id}/reference-photos")
async def list_reference_photos(
    global_id: UUID,
    request: Request,
) -> list[dict[str, Any]]:
    """List individual reference photos for this identity, newest first.
    Each row tells the UI whether a face was detected in that photo
    (used to render a 'face ✓' / 'face —' badge so the operator knows
    which photos contributed to the face_embedding average)."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT id, photo_path, face_bbox, source,
               face_embedding IS NOT NULL AS has_face,
               body_embedding IS NOT NULL AS has_body, face_px,
               uploaded_at, uploaded_by
        FROM identity_reference_photos
        WHERE global_id = $1
        ORDER BY uploaded_at DESC
        """,
        global_id,
    )
    return [
        {
            "id": str(r["id"]),
            "photo_path": r["photo_path"] or None,
            "has_face": bool(r["has_face"]),
            "has_body": bool(r["has_body"]),
            "source": r["source"],
            "face_px": float(r["face_px"]) if r["face_px"] is not None else None,
            "face_bbox": [float(v) for v in r["face_bbox"]] if r["face_bbox"] else None,
            "uploaded_at": r["uploaded_at"].isoformat(),
            "uploaded_by": str(r["uploaded_by"]) if r["uploaded_by"] else None,
        }
        for r in rows
    ]


@identities_router.delete("/identities/{global_id}/reference-photos/faceless")
async def delete_faceless_reference_photos(
    global_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Drop this identity's reference photos that carry no face.

    Why they're worth removing: face is the ONLY cross-session identity signal
    in this system (the event-manager's body cluster explicitly excludes
    enrolled identities), so a reference photo with `face_embedding IS NULL`
    buys no recognition. It still counts toward `reference_count` and still
    fills a slot in the gallery, which makes the enrollment look stronger than
    it is — a 49-photo identity carrying 31 faces reads as 49 photos' worth of
    evidence.

    One real thing IS lost, which is why this is an explicit operator action
    rather than something the upload path does silently: the merge picker's
    `near=` ranking (routes_identities/_core.py) seeds from the identity's
    newest TRACK embedding and falls back to a reference photo's
    `body_embedding` only when the identity has no tracks at all. Purge a
    photo-only, zero-sighting identity and it loses the one thing that puts it
    in the picker. For any identity with sightings — the normal case — there is
    no cost.

    Route ordering note: this sits before the `/{photo_id}` DELETE in the same
    router, so "faceless" is matched as a literal rather than parsed as a uuid.
    """
    pool = request.app.state.pool
    media_root = request.app.state.config.media_path
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """
            DELETE FROM identity_reference_photos
            WHERE global_id = $1 AND face_embedding IS NULL
            RETURNING photo_path
            """,
            global_id,
        )
        if rows:
            await _recompute_label_aggregates(conn, global_id)
        row = await conn.fetchrow(
            """
            SELECT global_id, name, kind, tags, notes, plate, source,
                   ai_described_at, reference_count, reference_embedding,
                   cover_photo_path,
                   created_by, created_at, updated_at
            FROM identity_labels WHERE global_id = $1
            """,
            global_id,
        )
    # Unlink AFTER the DB transaction commits — an orphan JPEG is a far cheaper
    # failure than a half-deleted row, same reasoning as the single delete.
    for r in rows:
        rel = r["photo_path"]
        if not rel:
            continue
        try:
            (media_root / rel).unlink(missing_ok=True)
        except Exception:
            log.exception("failed to unlink reference photo file %s", rel)
    log.info(
        "faceless reference photos purged: gid=%s removed=%d by %s",
        global_id,
        len(rows),
        user.username,
    )
    return {"removed": len(rows), "label": _label_row_to_dict(row) if row else None}


@identities_router.delete(
    "/identities/{global_id}/reference-photos/{photo_id}",
    status_code=204,
)
async def delete_reference_photo(
    global_id: UUID,
    photo_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    """Remove one reference photo. The JPEG on disk is unlinked, then the
    label's averaged embeddings are recomputed from the remaining
    photos (or cleared entirely if this was the last one)."""
    pool = request.app.state.pool
    media_root = request.app.state.config.media_path
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
                DELETE FROM identity_reference_photos
                WHERE id = $1 AND global_id = $2
                RETURNING photo_path
                """,
            photo_id,
            global_id,
        )
        if row is None:
            raise HTTPException(404, "reference photo not found")
        await _recompute_label_aggregates(conn, global_id)
    # Unlink AFTER the DB transaction commits — if we crashed between
    # delete and unlink we'd at worst leave an orphan JPEG, never a
    # half-deleted row.
    rel = row["photo_path"]
    if rel:
        try:
            (media_root / rel).unlink(missing_ok=True)
        except Exception:
            log.exception("failed to unlink reference photo file %s", rel)
    log.info("reference photo deleted: gid=%s photo=%s by %s", global_id, photo_id, user.username)


# --- AI-assisted description --------------------------------------------

# JSON-only prompt: VLM must emit a parseable object so the UI can drop
# it straight into the label form. Two examples in the system prompt
# (one person, one cat) anchor the output schema; without them models
# occasionally wrap the JSON in prose or use slightly different field names.
