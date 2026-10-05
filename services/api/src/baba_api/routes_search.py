"""Visual search endpoint.

Operator drops an image (a frame from another camera, a photo of a person
or vehicle, a screenshot of an enemy player's car) and the API embeds it
through the same body-appearance model the embedder service uses
(BABA_EMBEDDER_MODEL, OSNet by default), then runs a kNN over
`tracks.embedding` via pgvector's `<=>` cosine operator. The
HNSW index built in migration 002 makes this O(log n) instead of a
sequential scan of the entire track history.

Why not also kNN over face_embedding here: face crops require detecting +
aligning a face first (YuNet → ArcFace template warp) and most search
queries are body-shot (a photo of a stranger walking past). We can layer
face search on top in a separate endpoint once there's UX for it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import numpy as np
from baba_core import vector_literal
from baba_core.native import run_native
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

log = logging.getLogger(__name__)

search_router = APIRouter()


# Match the upload caps used by reference-photo enrollment so a single
# OOM strategy covers both pipelines.
_MAX_QUERY_BYTES = 8 * 1024 * 1024


class SearchHit(BaseModel):
    track_id: str
    global_id: str | None
    camera_id: str
    camera_name: str
    class_id: int
    class_name: str | None
    distance: float
    thumbnail_path: str | None
    crop_path: str | None
    label_name: str | None
    started_at: datetime
    ended_at: datetime | None


class SearchResult(BaseModel):
    hits: list[SearchHit]
    total_considered: int


def _decode_query_image(buf: bytes) -> np.ndarray | None:
    """Decode an uploaded query image to a native-aspect uint8 RGB crop (long
    edge <= MAX_CROP_EDGE) via the shared baba_core.cap_long_edge — the SAME
    prep the embedder (live tracks) and reference-photo enrollment use, so the
    query embedding lands in the track embedding space. Returns None on
    unrecognised payloads."""
    try:
        from baba_core import cap_long_edge
    except ImportError:
        log.exception("search: cap_long_edge import failed")
        return None
    from baba_api.imaging import decode_rgb_capped

    arr = decode_rgb_capped(buf)  # rejects decompression bombs before decode
    if arr is None:
        return None
    h, w = arr.shape[:2]
    if h < 32 or w < 32:
        return None
    return cap_long_edge(arr)


@search_router.post("/search/visual", response_model=SearchResult)
async def search_visual(
    request: Request,
    file: UploadFile = File(...),
    limit: int = Query(default=24, ge=1, le=100),
    class_id: int | None = Query(default=None),
    camera_id: UUID | None = Query(default=None),
    max_distance: float = Query(default=0.7, ge=0.0, le=2.0),
) -> SearchResult:
    """kNN over `tracks.embedding` against the uploaded image.

    `max_distance` is the cosine distance ceiling — pgvector's `<=>`
    returns values in [0, 2] where 0 is identical, 1 is orthogonal, 2 is
    opposite. The default 0.7 is loose enough to catch "same person in
    different clothes" but tight enough to filter random noise; tighten
    to 0.35 for strict "same outfit, same minute" matches.
    """
    backend = getattr(request.app.state, "embedder_backend", None)
    if backend is None:
        raise HTTPException(
            503,
            "visual search is disabled — BABA_EMBEDDER_MODEL is not "
            "pointing at a loadable ONNX file",
        )

    buf = await file.read()
    if len(buf) > _MAX_QUERY_BYTES:
        raise HTTPException(
            400,
            f"image too large ({len(buf)} bytes > {_MAX_QUERY_BYTES})",
        )
    crop = _decode_query_image(buf)
    if crop is None:
        raise HTTPException(400, "could not decode image (unsupported format or too small)")

    # backend.embed expects a list of uint8 RGB crops (any size — it resizes
    # to the model input itself); returns an L2-normalized (N, dim) float32 array.
    try:
        # Blocking ONNX forward — keep it off the event loop.
        vectors = await run_native(backend.embed, [crop])
    except Exception:
        log.exception("search: embedder failed on query image")
        raise HTTPException(500, "embedder failed to process query image") from None
    if vectors.shape[0] != 1:
        raise HTTPException(500, "embedder returned unexpected output shape")
    query_lit = vector_literal(vectors[0])

    pool = request.app.state.pool
    # ORDER BY embedding <=> $1 lets pgvector use the HNSW index. The
    # distance filter applied post-sort is cheap and lets the operator
    # tighten/loosen with the slider without re-querying differently.
    # We over-fetch slightly (limit * 2) so post-distance filtering
    # rarely returns a short page; if even that fails to fill, the
    # operator just sees fewer results — preferable to a longer query.
    sql = """
        SELECT
            t.id::text AS track_id,
            t.global_id::text AS global_id,
            t.camera_id::text AS camera_id,
            c.name AS camera_name,
            t.class_id,
            t.class_name,
            (t.embedding <=> $1::vector) AS distance,
            t.thumbnail_path,
            t.crop_path,
            l.name AS label_name,
            t.started_at,
            t.ended_at
        FROM tracks t
        JOIN cameras c ON c.id = t.camera_id
        LEFT JOIN identity_labels l ON l.global_id = t.global_id
        WHERE t.embedding IS NOT NULL
    """
    params: list[Any] = [query_lit]
    if class_id is not None:
        params.append(class_id)
        sql += f" AND t.class_id = ${len(params)}"
    if camera_id is not None:
        params.append(camera_id)
        sql += f" AND t.camera_id = ${len(params)}"
    params.append(limit * 2)
    sql += f" ORDER BY t.embedding <=> $1::vector LIMIT ${len(params)}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    hits: list[SearchHit] = []
    for r in rows:
        d = float(r["distance"])
        if d > max_distance:
            continue
        if len(hits) >= limit:
            break
        hits.append(
            SearchHit(
                track_id=r["track_id"],
                global_id=r["global_id"],
                camera_id=r["camera_id"],
                camera_name=r["camera_name"],
                class_id=int(r["class_id"]),
                class_name=r["class_name"],
                distance=d,
                thumbnail_path=r["thumbnail_path"],
                crop_path=r["crop_path"],
                label_name=r["label_name"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
            )
        )

    return SearchResult(hits=hits, total_considered=len(rows))
