"""Shared router + cross-domain helpers for the identities endpoints."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import numpy as np
from baba_core import parse_vector as _parse_pg_vector
from baba_core import vector_literal as _vector_literal
from fastapi import APIRouter

log = logging.getLogger(__name__)

identities_router = APIRouter()

# pgvector <-> numpy bridging lives in baba_core.pgvector. Aliased to the
# private names the identity modules already import so nothing else changes.
# (The old local _parse_pg_vector used the removed np.fromstring.)
__all__ = ["_parse_pg_vector", "_vector_literal", "identities_router"]


def _label_row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "global_id": str(r["global_id"]),
        "name": r["name"],
        "kind": r["kind"],
        "tags": list(r["tags"] or []),
        "notes": r["notes"],
        "plate": r.get("plate", None),
        "linked_person": str(r["linked_person"]) if r.get("linked_person") else None,
        "opus_person_id": r.get("opus_person_id", None),
        "source": r.get("source", "manual"),
        "ai_described_at": (
            r["ai_described_at"].isoformat()
            if "ai_described_at" in r and r["ai_described_at"] is not None
            else None
        ),
        "reference_count": r["reference_count"],
        "has_reference_embedding": r["reference_embedding"] is not None,
        "cover_photo_path": r.get("cover_photo_path", None),
        "created_by": str(r["created_by"]) if r["created_by"] is not None else None,
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


# Subdirs under media root that the operator is allowed to point a
# cover photo at. Anything else (a `../../etc/passwd`, an absolute
# path, a file outside `media/`) is rejected — the column is then
# served back by routes_thumbnails.* which has its own subdir guard,
# but defence-in-depth at write time keeps junk out of the DB.


async def _recompute_label_aggregates(conn, global_id: UUID) -> None:
    """Recompute identity_labels.reference_embedding +
    .face_embedding + .reference_count from the surviving rows in
    identity_reference_photos. Called after every insert/delete.

    Done in SQL on the pgvector side using the AVG aggregate — `pgvector`
    ships `AVG(vector)` natively. We L2-normalise the result in Python
    because PostgreSQL has no built-in vector norm function and we'd
    rather not pull in pg_norm. The averaged-and-normalised vector is
    what the re-ID kNN compares against."""
    row = await conn.fetchrow(
        """
        SELECT
            AVG(body_embedding)::vector AS body_avg,
            -- Only the active embedder's faces: an average across two
            -- models' spaces is a point in neither.
            AVG(face_embedding) FILTER (
                WHERE face_embedding IS NOT NULL
                  AND face_embedding_model = (SELECT model_key FROM face_recognition_settings)
            )::vector AS face_avg,
            (SELECT model_key FROM face_recognition_settings) AS face_model,
            COUNT(*) AS n
        FROM identity_reference_photos WHERE global_id = $1
        """,
        global_id,
    )
    n = int(row["n"] or 0)
    if n == 0:
        # No surviving photos — clear all aggregates so the kNN pool
        # forgets this identity. Label name + tags stay intact so the
        # operator's chosen name doesn't vanish.
        await conn.execute(
            """
            UPDATE identity_labels
            SET reference_embedding = NULL,
                face_embedding = NULL,
                face_embedding_model = NULL,
                reference_count = 0
            WHERE global_id = $1
            """,
            global_id,
        )
        return

    def _normalize_pg_vector(v: Any) -> str | None:
        # Parse + L2-norm + reformat. AVG(vector) over normalised inputs
        # doesn't stay on the unit sphere — we re-normalise so the
        # canonical centroid behaves correctly under cosine distance.
        if v is None:
            return None
        nums = _parse_pg_vector(v)
        norm = float(np.linalg.norm(nums))
        if norm > 0:
            nums = nums / norm
        return _vector_literal(nums)

    body_lit = _normalize_pg_vector(row["body_avg"])
    face_lit = _normalize_pg_vector(row["face_avg"])
    await conn.execute(
        """
        UPDATE identity_labels
        SET reference_embedding  = $1::vector,
            face_embedding       = $2::vector,
            face_embedding_model = CASE WHEN $2::vector IS NULL THEN NULL ELSE $5 END,
            reference_count      = $3
        WHERE global_id = $4
        """,
        body_lit,
        face_lit,
        n,
        global_id,
        row["face_model"],
    )
