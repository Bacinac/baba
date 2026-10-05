"""Face recognition settings + reference re-compute orchestration.

Routes:
  GET  /face-recognition/models          — list registered models with metadata
  GET  /face-recognition/settings        — current selection (singleton row)
  PUT  /face-recognition/settings        — switch model and/or threshold
  POST /face-recognition/recompute       — kick off async reference recompute
  GET  /face-recognition/recompute/{id}  — poll job progress

The Settings UI talks to these. The embedder picks a changed model up over
LISTEN; a recompute is a background task in this process that loads its own
face stack, and the job row is the UI's polling endpoint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

import asyncpg
import cv2
from baba_core import (
    FACE_DETECTORS,
    FACE_MODELS,
    get_face_detector,
    get_face_model,
    make_face_stack_for_model,
    resolve_detector_path,
    resolve_model_path,
    vector_literal,
)
from baba_core.inference import run_inference
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.face_recompute_queue import queue_selection_recompute

from .routes_identities._base import _recompute_label_aggregates
from .routes_identities._reference_photos import (
    _MIN_REFERENCE_FACE_PX,
    _face_box_px,
    _norm_face_bbox,
)

log = logging.getLogger(__name__)

face_recognition_router = APIRouter(prefix="/face-recognition", tags=["face-recognition"])

_MODELS_DIR = Path("/models")  # mounted into the api container the same as elsewhere

# What these store is the aligned 112x112 face the vector was drawn from, not a
# photo with a face in it: a detector asked to find a face inside one misses on
# most of them, and a recompute that asked would erase the reference.
_ALIGNED_FACE_SOURCES = frozenset({"from-face-samples"})


# --- request/response models ------------------------------------------------


class FaceModelOut(BaseModel):
    key: str
    name: str
    description: str
    license: str
    tier: str  # 'default' | 'byom' | 'commercial'
    bundled_with_baba: bool
    file_present: bool  # whether the ONNX file is on disk right now
    model_filename: str  # relative to /models/
    input_size: int
    embedding_dim: int


class FaceDetectorOut(BaseModel):
    key: str
    name: str
    description: str
    license: str
    tier: str  # 'default' | 'byom'
    bundled_with_baba: bool
    file_present: bool
    model_filename: str


class FaceRecognitionSettingsOut(BaseModel):
    model_key: str
    detector_key: str
    match_threshold: float
    active_model_key: str
    active_detector_key: str
    activation_status: Literal["active", "pending", "error"]
    activation_error: str | None
    recompute_job_id: str | None
    updated_at: str
    updated_by_user: str | None
    # Convenience for UI: reference photos tagged with this model
    # vs total reference photos that exist. The gap tells the user
    # "you need to recompute".
    references_in_active_model: int
    references_total: int
    # A reference photo with NO face is not stale — it is a pet, a vehicle, or
    # a shot where no face was found. Recompute cannot give it one; those are
    # matched by body embedding and work exactly as intended. Counting them as
    # "embedded under a different model" produced a warning that could never be
    # cleared and came back with every pet photo added.
    references_no_face: int
    # Genuinely stale: HAS a face embedding, from a different model (or from
    # before the model was recorded). These are what recompute fixes.
    references_stale: int


class FaceRecognitionSettingsIn(BaseModel):
    model_key: str = Field(..., min_length=1, max_length=64)
    detector_key: str = Field(default="yunet", min_length=1, max_length=64)
    match_threshold: float = Field(..., ge=0.0, le=2.0)


class RecomputeStartOut(BaseModel):
    job_id: str


class RecomputeStatusOut(BaseModel):
    job_id: str
    model_key: str
    status: str  # 'running' | 'done' | 'failed' | 'cancelled'
    total: int
    processed: int
    succeeded: int
    no_face: int
    missing_file: int
    started_at: str
    finished_at: str | None
    error_message: str | None


# --- model listing ----------------------------------------------------------


@face_recognition_router.get("/detectors", response_model=list[FaceDetectorOut])
async def list_face_detectors() -> list[FaceDetectorOut]:
    """Return every detector registered in baba_core.face_detectors with a
    `file_present` flag so the UI can grey out BYOM options whose weights
    the operator hasn't downloaded yet."""
    out: list[FaceDetectorOut] = []
    for spec in FACE_DETECTORS.values():
        path = resolve_detector_path(spec, _MODELS_DIR)
        out.append(
            FaceDetectorOut(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                license=spec.license,
                tier=spec.tier,
                bundled_with_baba=spec.bundled_with_baba,
                file_present=path.exists(),
                model_filename=spec.model_filename,
            )
        )
    return out


@face_recognition_router.get("/models", response_model=list[FaceModelOut])
async def list_face_models() -> list[FaceModelOut]:
    """Return every model registered in baba_core.face_models with a
    `file_present` flag so the UI can grey out BYOM options whose
    weights the operator hasn't downloaded yet."""
    out: list[FaceModelOut] = []
    for spec in FACE_MODELS.values():
        path = resolve_model_path(spec, _MODELS_DIR)
        out.append(
            FaceModelOut(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                license=spec.license,
                tier=spec.tier,
                bundled_with_baba=spec.bundled_with_baba,
                file_present=path.exists(),
                model_filename=spec.model_filename,
                input_size=spec.input_size,
                embedding_dim=spec.embedding_dim,
            )
        )
    return out


# --- settings get/put -------------------------------------------------------


async def _load_settings(pool: asyncpg.Pool) -> FaceRecognitionSettingsOut:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.*, "
            "       u.username AS updated_by_user "
            "FROM face_recognition_settings s "
            "LEFT JOIN users u ON u.id = s.updated_by "
            "WHERE s.id = 1"
        )
        if row is None:
            raise HTTPException(500, "face_recognition_settings singleton missing")
        # Reference coverage: how many reference photos exist vs. how many
        # are tagged with the currently active model. Mismatch = stale
        # embeddings → needs recompute.
        counts = await conn.fetchrow(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (
                       WHERE face_embedding IS NOT NULL AND face_embedding_model = $1
                   ) AS active,
                   count(*) FILTER (WHERE face_embedding IS NULL) AS no_face,
                   count(*) FILTER (
                       WHERE face_embedding IS NOT NULL
                         AND face_embedding_model IS DISTINCT FROM $1
                   ) AS stale
            FROM identity_reference_photos
            """,
            row["active_model_key"],
        )
        job_id = await conn.fetchval(
            "SELECT id FROM face_recompute_jobs WHERE model_key = $1 AND detector_key = $2 "
            "ORDER BY started_at DESC LIMIT 1", row["model_key"], row["detector_key"],
        )
    return FaceRecognitionSettingsOut(
        model_key=row["model_key"],
        detector_key=row["detector_key"],
        match_threshold=float(row["match_threshold"]),
        active_model_key=row["active_model_key"],
        active_detector_key=row["active_detector_key"],
        activation_status=("error" if row["api_error"] or row["embedder_error"] else
                           "active" if row["active_revision"] == row["revision"] else "pending"),
        activation_error=row["api_error"] or row["embedder_error"],
        recompute_job_id=str(job_id) if job_id is not None else None,
        updated_at=row["updated_at"].isoformat(),
        updated_by_user=row["updated_by_user"],
        references_in_active_model=int(counts["active"] or 0),
        references_total=int(counts["total"] or 0),
        references_no_face=int(counts["no_face"] or 0),
        references_stale=int(counts["stale"] or 0),
    )


@face_recognition_router.get("/settings", response_model=FaceRecognitionSettingsOut)
async def get_face_recognition_settings(request: Request) -> FaceRecognitionSettingsOut:
    return await _load_settings(request.app.state.pool)


@face_recognition_router.put("/settings", response_model=FaceRecognitionSettingsOut)
async def put_face_recognition_settings(
    body: FaceRecognitionSettingsIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> FaceRecognitionSettingsOut:
    # Validate the requested embedder model exists in the registry.
    try:
        spec = get_face_model(body.model_key)
    except KeyError as e:
        raise HTTPException(400, str(e)) from e

    # Validate detector.
    try:
        det_spec = get_face_detector(body.detector_key)
    except KeyError as e:
        raise HTTPException(400, str(e)) from e

    # For BYOM models/detectors the file must actually exist on disk —
    # otherwise we'd flip the active selection to something the embedder
    # can't load and quietly fall back, which is confusing in the UI.
    if not spec.bundled_with_baba:
        path = resolve_model_path(spec, _MODELS_DIR)
        if not path.exists():
            raise HTTPException(
                400,
                f"{spec.name} is BYOM and its weights file is not present at "
                f"{path}. Place the file there first, then save.",
            )
    if not det_spec.bundled_with_baba:
        dpath = resolve_detector_path(det_spec, _MODELS_DIR)
        if not dpath.exists():
            raise HTTPException(
                400,
                f"{det_spec.name} is BYOM and its weights file is not present at "
                f"{dpath}. Place the file there first, then save.",
            )

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        previous = await conn.fetchrow("SELECT * FROM face_recognition_settings WHERE id = 1 FOR UPDATE")
        selection = await conn.fetchrow(
            "UPDATE face_recognition_settings "
            "SET model_key = $1, detector_key = $2, match_threshold = $3, "
            "    updated_at = now(), updated_by = $4, revision = revision + 1, "
            "    api_revision = NULL, embedder_revision = NULL, "
            "    api_error = NULL, embedder_error = NULL WHERE id = 1 RETURNING *",
            body.model_key,
            body.detector_key,
            body.match_threshold,
            user.id,
        )
        await queue_selection_recompute(conn, previous, selection, user.id)

    await write_audit(
        pool,
        user=user,
        resource_type="face_recognition",
        op="update",
        payload={
            "after": {
                "model_key": body.model_key,
                "detector_key": body.detector_key,
                "match_threshold": body.match_threshold,
            },
        },
    )
    return await _load_settings(pool)


# --- async recompute orchestration -----------------------------------------


@face_recognition_router.post("/recompute", response_model=RecomputeStartOut)
async def start_recompute(
    request: Request,
    user: AuthUser = Depends(current_user),
) -> RecomputeStartOut:
    """Queue reference recomputation for the confirmed active face pair."""
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        settings = await conn.fetchrow(
            "SELECT active_model_key, active_detector_key, revision, active_revision, api_error, embedder_error "
            "FROM face_recognition_settings WHERE id = 1 FOR UPDATE"
        )
        if settings is None:
            raise HTTPException(500, "face_recognition_settings singleton missing")
        if (settings["revision"] != settings["active_revision"]
                or settings["api_error"] or settings["embedder_error"]):
            raise HTTPException(409, "face model activation is not complete")
        active_model = settings["active_model_key"]
        active_detector = settings["active_detector_key"]
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM face_recompute_jobs WHERE status IN ('pending', 'running'))"):
            raise HTTPException(409, "a recompute job is already queued or running")
        try:
            row = await conn.fetchrow(
                "INSERT INTO face_recompute_jobs (model_key, detector_key, settings_revision, started_by, status) "
                "VALUES ($1, $2, $3, $4, 'pending') RETURNING id",
                active_model,
                active_detector,
                settings["revision"],
                user.id,
            )
        except asyncpg.exceptions.UniqueViolationError:
            raise HTTPException(409, "a recompute job is already running") from None
        job_id = str(row["id"])

    return RecomputeStartOut(job_id=job_id)


@face_recognition_router.get("/recompute/{job_id}", response_model=RecomputeStatusOut)
async def get_recompute_status(job_id: UUID, request: Request) -> RecomputeStatusOut:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, model_key, status, total, processed, succeeded, "
            "       no_face, missing_file, started_at, finished_at, error_message "
            "FROM face_recompute_jobs WHERE id = $1",
            job_id,
        )
    if row is None:
        raise HTTPException(404, "recompute job not found")
    return RecomputeStatusOut(
        job_id=str(row["id"]),
        model_key=row["model_key"],
        status=row["status"],
        total=row["total"],
        processed=row["processed"],
        succeeded=row["succeeded"],
        no_face=row["no_face"],
        missing_file=row["missing_file"],
        started_at=row["started_at"].isoformat(),
        finished_at=row["finished_at"].isoformat() if row["finished_at"] else None,
        error_message=row["error_message"],
    )


def _reembed(stack, path: Path, aligned: bool):
    """The photo's face under the loaded stack: (vector, bbox, shape), None
    when it holds no face big enough to identify, "missing" when unreadable.
    An aligned face has no bbox of its own; the whole photo is the face."""
    img = cv2.imread(str(path))
    if img is None:
        return "missing"
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    if aligned:
        return stack.embedder.embed(rgb), None, rgb.shape
    hit = stack.embed_from_crop(rgb)
    if hit is None:
        return None
    vec, bbox, _score = hit
    # A face under the enrolment floor is not a face here either: recompute
    # would otherwise re-admit vectors that enrolment refused, and
    # MIN-over-references makes those one-way — they can only let a stranger in.
    px = _face_box_px(bbox)
    if px is not None and px < _MIN_REFERENCE_FACE_PX:
        return None
    return vec, bbox, rgb.shape


async def _store_reembedding(conn, rid: UUID, model_key: str, result) -> None:
    if result is None:
        await conn.execute(
            "UPDATE identity_reference_photos "
            "SET face_embedding = NULL, face_embedding_model = $1, "
            "face_px = NULL, face_bbox = NULL "
            "WHERE id = $2",
            model_key,
            rid,
        )
        return
    vec, bbox, shape = result
    if bbox is None:
        # face_px is the face's size in the frame it was cut from, which the
        # aligned crop no longer knows, so it is left as enrolment wrote it.
        await conn.execute(
            "UPDATE identity_reference_photos "
            "SET face_embedding = $1::vector, face_embedding_model = $2, "
            "face_bbox = $3 "
            "WHERE id = $4",
            vector_literal(vec),
            model_key,
            [0.0, 0.0, 1.0, 1.0],
            rid,
        )
        return
    await conn.execute(
        "UPDATE identity_reference_photos "
        "SET face_embedding = $1::vector, face_embedding_model = $2, "
        "face_px = $3, face_bbox = $4 "
        "WHERE id = $5",
        vector_literal(vec),
        model_key,
        _face_box_px(bbox),
        _norm_face_bbox(bbox, shape),
        rid,
    )


async def _recompute_selection_matches(conn, job_id: UUID, model_key: str, detector_key: str) -> bool:
    selected = await conn.fetchrow(
        "SELECT model_key, detector_key FROM face_recognition_settings WHERE id = 1 FOR SHARE"
    )
    if (selected["model_key"], selected["detector_key"]) == (model_key, detector_key):
        return True
    await conn.execute(
        "UPDATE face_recompute_jobs SET status = 'cancelled', finished_at = now(), "
        "error_message = 'superseded by a newer face selection' WHERE id = $1", job_id,
    )
    return False


async def _run_recompute(
    pool: asyncpg.Pool,
    job_id: UUID,
    model_key: str,
    detector_key: str,
) -> None:
    """Background task: re-embed every reference photo's face under the
    active stack, then recompute every identity's centroid the way enrolment
    does it."""
    try:
        async with pool.acquire() as conn, conn.transaction():
            if not await _recompute_selection_matches(conn, job_id, model_key, detector_key):
                return
        media_root = Path(os.environ.get("BABA_MEDIA_PATH", "/media"))
        yunet_path = Path(os.environ.get("BABA_FACE_DETECTOR_MODEL", "/models/face_yunet.onnx"))

        # Load + warmup is blocking (ONNX session init, possibly a TRT engine
        # build); keep it off the event loop so live WebRTC/SSE/requests don't
        # freeze for seconds while a recompute job spins up.
        stack, active_detector, active_key = await run_inference(
            make_face_stack_for_model,
            yunet_path=yunet_path,
            models_dir=_MODELS_DIR,
            model_key=model_key,
            detector_key=detector_key,
        )
        if stack is None or (active_detector, active_key) != (detector_key, model_key):
            await _mark_job_failed(
                pool,
                job_id,
                f"face stack failed to load for model={model_key} detector={detector_key}",
            )
            return

        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, photo_path, source FROM identity_reference_photos")
            await conn.execute(
                "UPDATE face_recompute_jobs SET total = $1, model_key = $2 WHERE id = $3",
                len(rows),
                active_key,
                job_id,
            )

        n_ok = n_no_face = n_missing = 0
        for n_processed, r in enumerate(rows, 1):
            path = media_root / r["photo_path"]
            aligned = r["source"] in _ALIGNED_FACE_SOURCES
            result = (
                await run_inference(_reembed, stack, path, aligned)
                if path.exists()
                else "missing"
            )
            async with pool.acquire() as conn, conn.transaction():
                if not await _recompute_selection_matches(conn, job_id, model_key, detector_key):
                    return
                if isinstance(result, str):
                    n_missing += 1
                else:
                    await _store_reembedding(conn, r["id"], active_key, result)
                    if result is None:
                        n_no_face += 1
                    else:
                        n_ok += 1
            if n_processed % 5 == 0 or n_processed == len(rows):
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE face_recompute_jobs SET processed = $1, "
                        "succeeded = $2, no_face = $3, missing_file = $4 WHERE id = $5",
                        n_processed,
                        n_ok,
                        n_no_face,
                        n_missing,
                        job_id,
                    )

        async with pool.acquire() as conn, conn.transaction():
            if not await _recompute_selection_matches(conn, job_id, model_key, detector_key):
                return
            for gid in await conn.fetch("SELECT global_id FROM identity_labels"):
                await _recompute_label_aggregates(conn, gid["global_id"])
            await conn.execute(
                "UPDATE face_recompute_jobs SET status = 'done', finished_at = now() WHERE id = $1",
                job_id,
            )
    except Exception as e:
        log.exception("face recompute job %s failed", job_id)
        await _mark_job_failed(pool, job_id, repr(e))


async def _mark_job_failed(pool: asyncpg.Pool, job_id: UUID, msg: str) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE face_recompute_jobs SET status = 'failed', "
                "finished_at = now(), error_message = $1 WHERE id = $2",
                msg[:2000],
                job_id,
            )
    except Exception:
        log.exception("could not mark job %s failed", job_id)
