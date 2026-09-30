"""Identity graph ops: merge and split."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from baba_api.auth import AuthUser, current_user
from baba_api.routes_identities._base import (
    _recompute_label_aggregates,
    identities_router,
)

log = logging.getLogger(__name__)


class _MergeIn(BaseModel):
    into: UUID


@identities_router.post("/identities/{global_id}/merge")
async def merge_identity(
    global_id: UUID,
    payload: _MergeIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Fold this identity into another: every track currently carrying
    `global_id` is reassigned to `payload.into`. Used to fix the obvious
    cases the cosine-distance gate missed — same person in a hoodie vs
    without, same car across very different lighting, etc.

    Idempotent: merging A→A is a no-op (no rows updated). Merging into a
    non-existent target is rejected because that would orphan the rows.
    `track_embedding_samples` doesn't reference global_id directly — it
    points at the per-track uuid which doesn't change — so nothing else
    needs touching."""
    if global_id == payload.into:
        return {"merged": 0, "into": str(payload.into)}
    pool = request.app.state.pool
    # Target is valid if it has tracks OR a label row. A photo-enrolled
    # identity with 0 sightings (e.g. "Marko" added by reference photos
    # before being observed) is a legitimate merge target — folding an
    # observed cluster into it is exactly how you teach it who it is.
    target_exists = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM tracks WHERE global_id = $1) "
        "OR EXISTS (SELECT 1 FROM identity_labels WHERE global_id = $1)",
        payload.into,
    )
    if not target_exists:
        raise HTTPException(404, f"target identity {payload.into} not found")
    async with pool.acquire() as conn, conn.transaction():
        result = await conn.execute(
            "UPDATE tracks SET global_id = $1 WHERE global_id = $2",
            payload.into,
            global_id,
        )
        # asyncpg's execute returns the command tag like "UPDATE 7".
        merged = int(result.rsplit(" ", 1)[-1]) if result.startswith("UPDATE") else 0
        # A merge is total: the source identity ceases to exist, so its
        # enrolled reference photos move to the survivor too. Without
        # this they were orphaned on a dead gid — the survivor lost the
        # operator's curated crops and the source lingered in the
        # gallery as a ghost (0 tracks but still a label + photos).
        refs_result = await conn.execute(
            "UPDATE identity_reference_photos SET global_id = $1 WHERE global_id = $2",
            payload.into,
            global_id,
        )
        refs_moved = int(refs_result.rsplit(" ", 1)[-1]) if refs_result.startswith("UPDATE") else 0
        # The link to the library person survives too, when the survivor has
        # none of its own: the source row is about to be deleted with it.
        await conn.execute(
            """
            UPDATE identity_labels t
               SET opus_person_id = s.opus_person_id
              FROM identity_labels s
             WHERE t.global_id = $1 AND s.global_id = $2
               AND t.opus_person_id IS NULL AND s.opus_person_id IS NOT NULL
            """,
            payload.into,
            global_id,
        )
        await conn.execute(
            "UPDATE identity_labels SET opus_person_id = NULL WHERE global_id = $1",
            global_id,
        )
        # The place registry stores the occupant by gid; the merge moves it
        # explicitly, here, where the operator's action is — no trigger. Any
        # open episode of the survivor is demoted first by the same
        # one-car-one-place rule the read-join applies — and its read STAYS
        # on the row, exactly as the reconciler's demote keeps it: a freed
        # read is re-taken by the next reconcile pass, which would reverse
        # this very merge within thirty seconds.
        await conn.execute(
            """
            UPDATE place_occupancy
               SET global_id = NULL, evidence = 'unknown'
             WHERE released_at IS NULL AND global_id = $1
               AND EXISTS (SELECT 1 FROM place_occupancy o2
                            WHERE o2.released_at IS NULL AND o2.global_id = $2)
            """,
            payload.into,
            global_id,
        )
        # ALL episodes, closed history included — the absorbed gid ceases to
        # exist, and a closed parking row pointing at it would render
        # nameless forever.
        await conn.execute(
            "UPDATE place_occupancy SET global_id = $1 WHERE global_id = $2",
            payload.into,
            global_id,
        )
        await conn.execute(
            "UPDATE plate_reads SET global_id = $1 WHERE global_id = $2",
            payload.into,
            global_id,
        )
        # Presence too, which nothing moved until 05.09 — the absorbed gid
        # ceased to exist and its boundary history stayed pointing at it.
        # Where both are present on the same camera they are one presence, so
        # the survivor's open row is widened to cover the absorbed one and the
        # absorbed row goes: `presence_episodes` allows one open row per
        # identity and camera, and nothing may be left claiming the second.
        await conn.execute(
            """
            UPDATE presence_episodes s
               SET present_since = LEAST(s.present_since, a.present_since),
                   last_confirmed_at = GREATEST(s.last_confirmed_at,
                                                a.last_confirmed_at)
              FROM presence_episodes a
             WHERE s.global_id = $1 AND s.departed_at IS NULL
               AND a.global_id = $2 AND a.departed_at IS NULL
               AND a.camera_id = s.camera_id
            """,
            payload.into,
            global_id,
        )
        await conn.execute(
            """
            DELETE FROM presence_episodes a
             WHERE a.global_id = $2 AND a.departed_at IS NULL
               AND EXISTS (SELECT 1 FROM presence_episodes s
                            WHERE s.global_id = $1 AND s.departed_at IS NULL
                              AND s.camera_id = a.camera_id)
            """,
            payload.into,
            global_id,
        )
        await conn.execute(
            "UPDATE presence_episodes SET global_id = $1 WHERE global_id = $2",
            payload.into,
            global_id,
        )
        # Drop the now-empty source label so the absorbed identity
        # disappears from the gallery instead of lingering at 0 sightings.
        await conn.execute(
            "DELETE FROM identity_labels WHERE global_id = $1",
            global_id,
        )
        # Recompute the survivor's canonical aggregates from the merged
        # reference-photo set (no-op if it has none).
        await _recompute_label_aggregates(conn, payload.into)
        # Audit log: payload.gid is the *surviving* identity so the
        # per-identity history endpoint surfaces this row for `into`.
        # `from` records what got absorbed.
        await conn.execute(
            "INSERT INTO identity_audit (user_id, op, payload) VALUES ($1, 'merge', $2)",
            user.id,
            json.dumps(
                {
                    "gid": str(payload.into),
                    "from": str(global_id),
                    "into": str(payload.into),
                    "count": merged,
                    "refs_moved": refs_moved,
                }
            ),
        )
    log.info(
        "identity merge: %s → %s (%d tracks, %d refs, by %s)",
        global_id,
        payload.into,
        merged,
        refs_moved,
        user.username,
    )
    return {"merged": merged, "into": str(payload.into)}


class _SplitIn(BaseModel):
    track_id: UUID


@identities_router.post("/identities/{global_id}/split")
async def split_identity(
    global_id: UUID,
    payload: _SplitIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Break one track out of this identity and turn it into its own.
    The single track's `global_id` is reset to its own `id` (every track
    is a one-track identity until it merges with someone). Useful when
    re-ID grouped two different people who happened to look similar."""
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT global_id FROM tracks WHERE id = $1",
            payload.track_id,
        )
        if row is None:
            raise HTTPException(404, f"track {payload.track_id} not found")
        if row["global_id"] != global_id:
            raise HTTPException(
                400,
                f"track {payload.track_id} belongs to identity {row['global_id']}, not {global_id}",
            )
        await conn.execute(
            "UPDATE tracks SET global_id = id WHERE id = $1",
            payload.track_id,
        )
        # Presence stays with the source. An episode is a person's boundary
        # over a stretch of time on one camera, not a property of the track
        # being pulled out, and the person was still there for the rest of it.
        # Splitting one would need a second answer to what the episode was.
        # gid in payload points at the *source* identity so its
        # history shows "track X was split out".
        await conn.execute(
            "INSERT INTO identity_audit (user_id, op, payload) VALUES ($1, 'split', $2)",
            user.id,
            json.dumps(
                {
                    "gid": str(global_id),
                    "from_gid": str(global_id),
                    "track_id": str(payload.track_id),
                    "new_gid": str(payload.track_id),
                }
            ),
        )
    log.info(
        "identity split: track %s out of %s (by %s)", payload.track_id, global_id, user.username
    )
    return {"split_track_id": str(payload.track_id), "new_global_id": str(payload.track_id)}


# --- labels & reference photos -------------------------------------------
