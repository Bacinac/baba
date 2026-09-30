"""Bulk delete an identity's sightings."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel

from baba_api.auth import AuthUser, current_user
from baba_api.routes_identities._base import (
    identities_router,
)

log = logging.getLogger(__name__)


class _DeleteSightingsIn(BaseModel):
    track_ids: list[UUID]


@identities_router.post("/identities/{global_id}/delete-sightings")
async def delete_sightings(
    global_id: UUID,
    payload: _DeleteSightingsIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, int]:
    """Hard-delete specific sighting tracks of this identity — the row plus
    every JPEG (crop / thumbnail / face crop + per-observation sample crops).
    Used to prune redundant near-duplicate sightings that won't promote to
    references. Only tracks that actually belong to `global_id` are touched."""
    if not payload.track_ids:
        return {"deleted": 0}
    pool = request.app.state.pool
    media_root = request.app.state.config.media_path
    track_rows: list[Any] = []
    sample_rows: list[Any] = []
    async with pool.acquire() as conn, conn.transaction():
        track_rows = await conn.fetch(
            "SELECT id, thumbnail_path, crop_path, face_crop_path "
            "FROM tracks WHERE global_id = $1 AND id = ANY($2)",
            global_id,
            payload.track_ids,
        )
        owned = [r["id"] for r in track_rows]
        if owned:
            sample_rows = await conn.fetch(
                "SELECT crop_path, face_crop_path FROM track_embedding_samples "
                "WHERE track_id = ANY($1)",
                owned,
            )
            await conn.execute(
                "DELETE FROM track_embedding_samples WHERE track_id = ANY($1)",
                owned,
            )
            await conn.execute("DELETE FROM events WHERE track_id = ANY($1)", owned)
            await conn.execute("DELETE FROM tracks WHERE id = ANY($1)", owned)
            # No identity_audit row: 'delete_sightings' isn't an allowed op
            # and the per-identity history timeline was removed anyway. The
            # log.info below is the forensic record.
    if not track_rows:
        return {"deleted": 0}

    def _unlink(rel: str | None) -> None:
        if not rel:
            return
        try:
            (media_root / rel).unlink(missing_ok=True)
        except Exception:
            log.exception("failed to unlink %s", rel)

    for r in track_rows:
        _unlink(r["thumbnail_path"])
        _unlink(r["crop_path"])
        _unlink(r["face_crop_path"])
    for r in sample_rows:
        _unlink(r["crop_path"])
        _unlink(r["face_crop_path"])
    log.info("deleted %d sighting(s) from gid=%s by %s", len(track_rows), global_id, user.username)
    return {"deleted": len(track_rows)}


# --- reference photo enrollment -----------------------------------------

# Multipart upload caps. Per-photo and per-batch limits keep a runaway
# client from OOMing the api process; crops are downscaled to MAX_CROP_EDGE
# anyway so the original size doesn't matter much past ~1024px on the long edge.
