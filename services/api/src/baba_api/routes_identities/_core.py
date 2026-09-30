"""Identity core CRUD: list, detail, label, cover-photo, delete."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from baba_core import group_for
from baba_core.paths import CROPS, FACE_CROPS, REFERENCE_PHOTOS, THUMBNAILS
from baba_core.plates import match_gallery
from baba_core.retention import LABELLED
from fastapi import Depends, HTTPException, Query, Request
from home_core.tasks import spawn
from pydantic import BaseModel, Field

from baba_api.auth import AuthUser, current_user
from baba_api.routes_identities._base import (
    _label_row_to_dict,
    identities_router,
)
from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)


@identities_router.get("/identities")
async def list_identities(
    request: Request,
    class_id: int | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    near: UUID | None = Query(default=None),
    labeled: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=128),
    has_image: bool | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List identities. Default order is most-recently-seen first.

    Pass `near=<global_id>` to switch to kNN order: candidates are sorted
    by the minimum cosine distance between any of their tracks and the
    reference identity's canonical (most-recent) embedding. The reference
    identity itself is excluded. Used by the merge picker to surface
    likely-same-identity candidates first instead of forcing the user to
    scroll a chronological list.

    `nearest_dist` is included in the response when `near` is set so the
    UI can show the score alongside each candidate.
    """
    pool = request.app.state.pool

    # The driver set is the union of (gids that have tracks) ∪ (gids
    # that have a label). Without the label side, a freshly-labelled
    # identity that hasn't been sighted yet — i.e. operator added a
    # name + reference photos but no track has finalized against it —
    # would silently disappear from the gallery (the old query was
    # FROM tracks, so 0 tracks ⇒ 0 rows). The reference photos make
    # those entries visible AND act as the kNN match seed.
    flt = SqlFilter()

    if since is not None:
        flt.add("t.ended_at >= ?", since)
    # labeled / unlabeled filter, class filter, search, and has-image
    # all evaluate after GROUP BY because they depend on aggregates
    # (label MAX, COUNT, COALESCE'd class). Goes into a separate
    # list because everything else above operates on per-row WHERE.
    having_parts: list[str] = []
    # Class filter: for a LABELED identity the operator's taxonomy (kind)
    # decides where it files — the detector class is how it was SEEN, not what
    # it IS (the robot mower reads as "dog" but is a device, which files with
    # machines under "Vozila", never under "Ljubimci"). Unlabeled identities
    # (and labels whose kind doesn't map — ''/custom) fall back to the most
    # recent track class. Also keeps a label-only "Octavia" visible under
    # "Vozila" before its first sighting.
    if class_id is not None:
        gids_ph = flt.bind(list(group_for(class_id)))
        having_parts.append(
            f"""(
              COALESCE(
                CASE MAX(l.kind)
                      WHEN 'person'  THEN 0
                      WHEN 'vehicle' THEN 2
                      WHEN 'object'  THEN 2
                      WHEN 'pet'     THEN 15
                      WHEN 'bird'    THEN 14
                    END,
                (array_agg(t.class_id ORDER BY t.ended_at DESC)
                    FILTER (WHERE t.class_id IS NOT NULL))[1]
              ) = ANY({gids_ph})
            )"""
        )
    if labeled is True:
        having_parts.append("MAX(l.name) IS NOT NULL")
    elif labeled is False:
        having_parts.append("MAX(l.name) IS NULL")
    if q:
        having_parts.append(f"MAX(l.name) ILIKE {flt.bind(f'%{q}%')}")
    if has_image is True:
        # Hide "bez thumbnaila" identities by default — short tracks
        # where both thumbnail capture paths failed clutter the gallery
        # with empty black tiles and have no visual identity to manage.
        # A label with operator-uploaded reference photos counts as
        # "has image" too — those photos are exactly the picture of
        # the identity the user is managing.
        having_parts.append(
            "(COUNT(t.crop_path) > 0 OR COUNT(t.thumbnail_path) > 0 "
            "OR EXISTS (SELECT 1 FROM identity_reference_photos rp "
            "WHERE rp.global_id = g.global_id AND rp.photo_path <> ''))"
        )
    elif has_image is False:
        having_parts.append(
            "(COUNT(t.crop_path) = 0 AND COUNT(t.thumbnail_path) = 0 "
            "AND NOT EXISTS (SELECT 1 FROM identity_reference_photos rp "
            "WHERE rp.global_id = g.global_id AND rp.photo_path <> ''))"
        )

    near_distance_select = "NULL::real AS nearest_dist"
    # Default order: labeled identities first (user-named come up top —
    # "Marko" before all the anonymous person sightings under him), then
    # most-recent within each bucket. `IS NULL ASC` puts FALSE (= named)
    # before TRUE (= anonymous). The `near` query overrides this to
    # cosine-sort for the merge picker.
    near_order = "(MAX(l.name) IS NULL) ASC, MAX(t.ended_at) DESC"
    if near is not None:
        # Reference embedding = the near identity's most recent track that
        # has one. Most-recent (vs averaged) keeps the picker reactive to
        # the latest pose/lighting we've seen for that identity, which
        # tends to be what the user is actually trying to match against.
        # Fall back to its newest reference-photo body embedding so a
        # photo-only identity (enrolled, 0 sightings) can still seed the
        # picker when it is the one doing the searching.
        ref_embedding = await pool.fetchval(
            """
            SELECT embedding FROM tracks
            WHERE global_id = $1 AND embedding IS NOT NULL
            ORDER BY ended_at DESC LIMIT 1
            """,
            near,
        )
        if ref_embedding is None:
            ref_embedding = await pool.fetchval(
                """
                SELECT body_embedding FROM identity_reference_photos
                WHERE global_id = $1 AND body_embedding IS NOT NULL
                ORDER BY uploaded_at DESC LIMIT 1
                """,
                near,
            )
        if ref_embedding is not None:
            ref_ph = flt.bind(ref_embedding)
            near_ph = flt.bind(near)
            # Exclude self at the GROUP level, NOT via a per-row track
            # filter: a per-row `t.embedding IS NOT NULL` turns the LEFT
            # JOIN into an inner join and silently drops label-only
            # identities (enrolled by reference photo, 0 sightings — e.g.
            # "Marko") from the merge picker, so they can never be a merge
            # target. Distance is the MIN cosine over BOTH the candidate's
            # tracks AND its reference photos, so a 0-track enrolled
            # identity still gets a score and appears in the list.
            having_parts.append(f"g.global_id <> {near_ph}")
            dist_expr = (
                f"LEAST("  # noqa: S608
                f"MIN(t.embedding <=> {ref_ph}::vector) "
                f"FILTER (WHERE t.embedding IS NOT NULL), "
                f"(SELECT MIN(rp.body_embedding <=> {ref_ph}::vector) "
                f"FROM identity_reference_photos rp "
                f"WHERE rp.global_id = g.global_id "
                f"AND rp.body_embedding IS NOT NULL))"
            )
            near_distance_select = f"{dist_expr} AS nearest_dist"
            near_order = f"{dist_expr} ASC NULLS LAST"
        # If the near identity has no embedding we silently fall back to
        # the default chronological order — better than a 4xx because the
        # caller (UI merge picker) just wants *some* list to show.

    limit_ph = flt.bind(limit)
    where_clause = flt.where()
    sql = f"""
        WITH gids AS (
            SELECT global_id FROM tracks WHERE global_id IS NOT NULL
            UNION
            SELECT global_id FROM identity_labels
        )
        SELECT
            g.global_id,
            -- Most-recent track class wins; fall back to label.kind
            -- (mapped to canonical class_id) for label-only identities
            -- that haven't been sighted yet.
            COALESCE(
                (array_agg(t.class_id ORDER BY t.ended_at DESC)
                    FILTER (WHERE t.class_id IS NOT NULL))[1],
                CASE MAX(l.kind)
                    WHEN 'person'  THEN 0
                    WHEN 'vehicle' THEN 2
                    WHEN 'pet'     THEN 15
                    WHEN 'bird'    THEN 14
                END
            ) AS class_id,
            COALESCE(
                (array_agg(t.class_name ORDER BY t.ended_at DESC)
                    FILTER (WHERE t.class_name IS NOT NULL))[1],
                MAX(l.kind)
            ) AS class_name,
            COUNT(t.id)                    AS n_tracks,
            MIN(t.started_at)              AS first_seen,
            MAX(t.ended_at)                AS last_seen,
            -- Distinct camera slugs this identity has been observed on.
            -- NULL-filtered so label-only identities don't carry a phantom
            -- {{NULL}} element from the LEFT JOIN.
            COALESCE(
                array_agg(DISTINCT c.slug) FILTER (WHERE c.slug IS NOT NULL),
                ARRAY[]::text[]
            ) AS cameras,
            -- Most recent non-null thumbnail across the identity's tracks.
            (array_agg(t.thumbnail_path ORDER BY t.ended_at DESC)
                FILTER (WHERE t.thumbnail_path IS NOT NULL))[1] AS thumbnail_path,
            -- Focused subject image. An operator-pinned cover wins so the
            -- gallery card stops flipping to whatever the latest detection
            -- happened to be; otherwise prefer the most recent track crop,
            -- then fall back to the most recent operator-uploaded reference
            -- photo so labelled identities with no sightings yet still render
            -- a face/object card instead of an empty black tile.
            COALESCE(
                MAX(l.cover_photo_path),
                (array_agg(t.crop_path ORDER BY t.ended_at DESC)
                    FILTER (WHERE t.crop_path IS NOT NULL))[1],
                (SELECT photo_path FROM identity_reference_photos rp
                  WHERE rp.global_id = g.global_id AND rp.photo_path <> ''
                  ORDER BY uploaded_at DESC LIMIT 1)
            ) AS crop_path,
            -- Operator-set label, if any. NULL when the identity hasn't
            -- been named yet. Aggregated via MAX because the LEFT JOIN
            -- duplicates the same single label row across every track.
            MAX(l.name)                    AS label_name,
            MAX(l.kind)                    AS label_kind,
            MAX(l.tags)                    AS label_tags,
            MAX(l.affiliation)             AS label_affiliation,
            BOOL_OR(l.resident)            AS label_resident,
            MAX(l.species)                 AS label_species,
            MAX(l.plate)                   AS label_plate,
            MAX(l.source)                  AS label_source,
            BOOL_OR(l.reference_embedding IS NOT NULL) AS has_reference_embedding,
            -- Provenance: does this identity have ANY face evidence -- a
            -- captured face on one of its tracks, or an enrolled face
            -- reference? Drives the "face-confirmed vs appearance-only" badge.
            (BOOL_OR(t.face_embedding IS NOT NULL)
                OR BOOL_OR(l.face_embedding IS NOT NULL)) AS face_confirmed,
            {near_distance_select}
        FROM gids g
        LEFT JOIN tracks t ON t.global_id = g.global_id
        LEFT JOIN cameras c ON c.id = t.camera_id
        LEFT JOIN identity_labels l ON l.global_id = g.global_id
        {where_clause}
        GROUP BY g.global_id
        {("HAVING " + " AND ".join(having_parts)) if having_parts else ""}
        ORDER BY {near_order}
        LIMIT {limit_ph}
    """  # noqa: S608
    rows = await pool.fetch(sql, *flt.args)
    return [
        {
            "global_id": str(r["global_id"]),
            "class_id": r["class_id"],
            "class_name": r["class_name"],
            "n_tracks": r["n_tracks"],
            "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "cameras": list(r["cameras"] or []),
            "thumbnail_path": r["thumbnail_path"],
            "crop_path": r["crop_path"],
            "nearest_dist": (float(r["nearest_dist"]) if r["nearest_dist"] is not None else None),
            "face_confirmed": bool(r["face_confirmed"]),
            "label": (
                {
                    "name": r["label_name"],
                    "kind": r["label_kind"],
                    "tags": list(r["label_tags"] or []),
                    "affiliation": r["label_affiliation"] or "unknown",
                    "resident": bool(r["label_resident"]),
                    "species": r["label_species"],
                    "plate": r["label_plate"],
                    "source": r["label_source"] or "manual",
                    "has_reference_embedding": bool(r["has_reference_embedding"]),
                }
                if r["label_name"] is not None
                else None
            ),
        }
        for r in rows
    ]


# NOTE: registered BEFORE /identities/{global_id} — "anon-groups" would
# otherwise be captured by the UUID path param and 422.
@identities_router.get("/identities/anon-groups")
async def anon_groups(
    request: Request,
    threshold: float = Query(default=0.35, ge=0.05, le=1.0),
    limit: int = Query(default=400, ge=10, le=1000),
) -> dict[str, Any]:
    """Similarity grouping of UNNAMED identities, for gallery triage.

    The event-manager's anonymous clustering is deliberately narrow (same
    camera + same day + tight distance) so it can run unattended without
    over-merging — the price is a gallery of hundreds of single-sighting
    cards where the same person appears dozens of times. This endpoint does
    the WIDE grouping that is safe precisely because a human confirms it:
    one representative embedding per anon identity (its most recent track's —
    the same expression the `near` kNN uses), pairwise pgvector distance
    under `threshold` (default 0.35 = the UI's likely-match line), connected
    components. The UI renders each group as one stack with one "Ovo je…"
    action, so assigning a person's 30 scattered cards is one click, not 30.

    Returns only multi-member groups (`{"groups": [[gid, …], …]}`, largest
    first) — everything absent is a single and renders as before. Class
    groups are respected (person never groups with car; dog/cat pool
    together, same as matching)."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT gid, class_id, n_tracks, emb FROM (
            SELECT DISTINCT ON (t.global_id)
                   t.global_id AS gid, t.class_id,
                   count(*) OVER (PARTITION BY t.global_id) AS n_tracks,
                   t.embedding::text AS emb
            FROM tracks t
            LEFT JOIN identity_labels l ON l.global_id = t.global_id
            WHERE l.global_id IS NULL AND t.embedding IS NOT NULL
            ORDER BY t.global_id, t.ended_at DESC
        ) r
        ORDER BY n_tracks DESC
        LIMIT $1
        """,
        limit,
    )
    # LEADER clustering, not connected components: components chain (A~B~C…
    # collapsed the whole gallery into one 299-member blob — measured), while a
    # leader group admits a member only when it is within `threshold` of the
    # LEADER, bounding the group's diameter. Leaders are seeded most-seen
    # first, so the best-established view of a subject anchors its group.
    import numpy as np

    def _vec(raw: str) -> np.ndarray | None:
        try:
            v = np.array([float(x) for x in raw.strip("[]").split(",")], dtype=np.float32)
        except ValueError:
            return None
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None

    leaders: list[tuple[np.ndarray, frozenset[int], list[str]]] = []
    for r in rows:
        v = _vec(r["emb"])
        if v is None:
            continue
        cls = group_for(r["class_id"])
        gid = str(r["gid"])
        placed = False
        for lv, lcls, members in leaders:
            if lcls == cls and float(1.0 - np.dot(lv, v)) < threshold:
                members.append(gid)
                placed = True
                break
        if not placed:
            leaders.append((v, cls, [gid]))
    groups = sorted(
        (m for _v, _c, m in leaders if len(m) >= 2),
        key=len,
        reverse=True,
    )
    return {"groups": groups}


# NOTE: like anon-groups, registered BEFORE the /identities/{global_id}
# routes so the literal "anon" segment isn't captured by the UUID param.
@identities_router.delete("/identities/anon", status_code=202)
async def delete_all_anon(
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, int]:
    """Purge EVERY unnamed identity — tracks, events, embedding samples and
    all their JPEGs. The operator's "wipe the backlog" button: junk clusters
    accumulate faster than one-by-one deletion is worth anyone's time, and
    retention would reap them anyway — this just does it now.

    Runs in the BACKGROUND and returns 202 immediately: the file unlinks for
    hundreds of clusters can exceed Cloudflare's ~100 s proxy timeout, and the
    established pattern for bulk ops is accept-then-work (see the async bulk-op
    precedent). The gallery's SSE refresh shows the shrinking list live.
    Labeled identities and their sightings are untouched."""
    pool = request.app.state.pool
    media_root = request.app.state.config.media_path
    track_rows = await pool.fetch(
        """
        SELECT t.id, t.thumbnail_path, t.crop_path, t.face_crop_path
        FROM tracks t
        LEFT JOIN identity_labels l ON l.global_id = t.global_id
        WHERE l.global_id IS NULL
        """
    )
    n = len(track_rows)
    log.info("anon purge scheduled: %d tracks (by %s)", n, user.username)
    if n == 0:
        return {"scheduled": 0}

    async def _purge() -> None:
        try:
            ids = [r["id"] for r in track_rows]
            async with pool.acquire() as conn:
                sample_rows = await conn.fetch(
                    "SELECT crop_path, face_crop_path FROM track_embedding_samples "
                    "WHERE track_id = ANY($1)",
                    ids,
                )
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM track_embedding_samples WHERE track_id = ANY($1)", ids
                    )
                    await conn.execute("DELETE FROM events WHERE track_id = ANY($1)", ids)
                    await conn.execute("DELETE FROM tracks WHERE id = ANY($1)", ids)

            def _unlink(rel: str | None) -> None:
                if not rel:
                    return
                try:
                    (media_root / rel).unlink(missing_ok=True)
                except Exception:
                    log.exception("anon purge: failed to unlink %s", rel)

            def _unlink_all() -> None:
                for r in track_rows:
                    _unlink(r["thumbnail_path"])
                    _unlink(r["crop_path"])
                    _unlink(r["face_crop_path"])
                for r in sample_rows:
                    _unlink(r["crop_path"])
                    _unlink(r["face_crop_path"])

            # Off the loop. The 202 above exists because these unlinks can run
            # for minutes, and running them here with no await between the
            # first and the last made the whole api unresponsive for exactly
            # that long — the early return bought nothing but a status code.
            await asyncio.to_thread(_unlink_all)
            log.info("anon purge done: %d tracks removed", n)
        except Exception:
            log.exception("anon purge failed")

    # spawn keeps a strong reference so the task can't be GC'd mid-purge (a
    # bare create_task is only weakly held) and its exceptions surface.
    spawn(_purge(), name="anon-purge")
    return {"scheduled": n}


@identities_router.get("/identities/{global_id}")
async def get_identity(global_id: UUID, request: Request) -> dict[str, Any]:
    """Detail view for one identity: aggregate metadata plus every
    constituent track in chronological order. Used by the UI to render
    the identity's timeline (which cameras saw it when, with thumbnails
    and links into the recordings/events views)."""
    pool = request.app.state.pool

    rows = await pool.fetch(
        """
        SELECT t.id, t.camera_id, c.slug AS camera_slug, c.name AS camera_name,
               t.class_id, t.class_name, t.started_at, t.ended_at,
               t.n_observations, t.thumbnail_path, t.crop_path,
               t.face_embedding IS NOT NULL AS has_face,
               EXTRACT(EPOCH FROM (t.ended_at - t.started_at)) AS duration_s
        FROM tracks t
        JOIN cameras c ON c.id = t.camera_id
        WHERE t.global_id = $1
          -- Only show reviewable sightings: a track with neither a crop nor
          -- a thumbnail is un-verifiable noise on the timeline.
          AND (t.crop_path IS NOT NULL OR t.thumbnail_path IS NOT NULL)
        ORDER BY t.started_at ASC
        """,
        global_id,
    )

    # Fetch label first — used as fallback metadata when there are no
    # tracks yet (labelled but never sighted) and as the source of
    # truth for the operator-curated parts of the identity.
    label_row = await pool.fetchrow(
        """
        SELECT name, kind, tags, notes, plate, source, ai_described_at,
               reference_count, cover_photo_path, opus_person_id,
               reference_embedding IS NOT NULL AS has_reference_embedding,
               face_embedding IS NOT NULL AS has_face_embedding
        FROM identity_labels WHERE global_id = $1
        """,
        global_id,
    )

    if not rows and label_row is None:
        # Truly unknown gid — neither a sighting nor a curated label.
        raise HTTPException(404, f"identity {global_id} not found")

    tracks = [
        {
            "id": str(r["id"]),
            "camera": {
                "id": str(r["camera_id"]),
                "slug": r["camera_slug"],
                "name": r["camera_name"],
            },
            "class_id": r["class_id"],
            "class_name": r["class_name"],
            "started_at": r["started_at"].isoformat(),
            "ended_at": r["ended_at"].isoformat(),
            "duration_s": float(r["duration_s"]) if r["duration_s"] is not None else None,
            "n_observations": r["n_observations"],
            "thumbnail_path": r["thumbnail_path"],
            "crop_path": r["crop_path"],
            "has_face": bool(r["has_face"]),
        }
        for r in rows
    ]

    # Same aggregate fields as the list endpoint, derived from the per-track
    # rows we already fetched — saves a second query.
    cameras = sorted({t["camera"]["slug"] for t in tracks})
    latest_thumb = next(
        (t["thumbnail_path"] for t in reversed(tracks) if t["thumbnail_path"]),
        None,
    )
    # Operator-curated cover override wins outright when set — this is
    # the whole point of the cover_photo_path column (let the operator
    # pin a specific frame as the identity's "face card" instead of
    # whatever the last sighting happened to look like).
    cover_override = (
        label_row["cover_photo_path"]
        if label_row is not None and label_row["cover_photo_path"]
        else None
    )
    if cover_override:
        latest_crop = cover_override
    else:
        latest_crop = next(
            (t["crop_path"] for t in reversed(tracks) if t["crop_path"]),
            None,
        )
        # When there are no tracks, fall back to a reference photo as the
        # subject image — same approach the list endpoint takes.
        if latest_crop is None:
            latest_crop = await pool.fetchval(
                """
                SELECT photo_path FROM identity_reference_photos
                 WHERE global_id = $1 AND photo_path <> ''
                 ORDER BY uploaded_at DESC LIMIT 1
                """,
                global_id,
            )

    # Most-recent class wins for display. For label-only identities
    # we map label.kind to its canonical class_id so the gallery
    # filters and badges keep working.
    _kind_to_class: dict[str, tuple[int, str]] = {
        "person": (0, "person"),
        "vehicle": (2, "car"),
        "pet": (15, "cat"),
        "bird": (14, "bird"),
    }
    if tracks:
        latest_class_id = tracks[-1]["class_id"]
        latest_class_name = tracks[-1]["class_name"]
    elif label_row is not None and label_row["kind"] in _kind_to_class:
        latest_class_id, latest_class_name = _kind_to_class[label_row["kind"]]
    else:
        latest_class_id, latest_class_name = None, None

    # Face evidence on any sighting, or an enrolled face reference.
    face_confirmed = any(t["has_face"] for t in tracks) or bool(
        label_row is not None and label_row["has_face_embedding"]
    )

    return {
        "global_id": str(global_id),
        "class_id": latest_class_id,
        "class_name": latest_class_name,
        "n_tracks": len(tracks),
        "first_seen": tracks[0]["started_at"] if tracks else None,
        "last_seen": tracks[-1]["ended_at"] if tracks else None,
        "cameras": cameras,
        "thumbnail_path": latest_thumb,
        "crop_path": latest_crop,
        "face_confirmed": face_confirmed,
        "tracks": tracks,
        "label": (
            {
                "name": label_row["name"],
                "kind": label_row["kind"],
                "tags": list(label_row["tags"] or []),
                "notes": label_row["notes"],
                "plate": label_row["plate"],
                "source": label_row["source"],
                "ai_described_at": (
                    label_row["ai_described_at"].isoformat()
                    if label_row["ai_described_at"] is not None
                    else None
                ),
                "reference_count": label_row["reference_count"],
                "has_reference_embedding": label_row["has_reference_embedding"],
                "cover_photo_path": label_row["cover_photo_path"],
                "opus_person_id": label_row["opus_person_id"],
            }
            if label_row is not None
            else None
        ),
    }


class _LabelIn(BaseModel):
    """Upsert payload. All fields optional individually but at least `name`
    is required for a fresh row — the SQL handles partial updates via
    COALESCE so omitting a field keeps its prior value."""

    name: str | None = Field(None, min_length=1, max_length=128)
    kind: str | None = Field(None, max_length=32)
    tags: list[str] | None = None
    notes: str | None = Field(None, max_length=2000)
    plate: str | None = Field(None, max_length=32)
    # Structured, vocabulary-bound — deliberately NOT tags. These are what
    # automations key on, and free-text tags had already drifted (one identity
    # carried both `obitelj` and `family`, another only `obitelj`, so a filter
    # on "family" silently missed it). Tags stay for free description.
    affiliation: (
        Literal[
            "family", "friend", "neighbour", "guest", "delivery", "service", "official", "unknown"
        ]
        | None
    ) = None
    # Orthogonal to affiliation, not a value of it: Nika is family but no
    # longer lives here, a lodger is resident without being family. One field
    # could not hold both without losing someone.
    resident: bool | None = None
    # For pets: the identity is the source of truth for what an animal IS —
    # the nano detector flips cat<->dog on the same animal (the very reason
    # PET_GROUP pools them for re-ID), so its per-frame label can't be.
    species: str | None = Field(None, max_length=32)
    # For vehicles: the PERSON identity this vehicle belongs to. Lets a
    # departure close the owner's presence episodes (left_with_vehicle) —
    # never used to name anyone; anyone can drive a car.
    linked_person: UUID | None = None
    # The library person this identity stands for. Null unlinks.
    opus_person_id: int | None = None


_COVER_PHOTO_ALLOWED_PREFIXES = (
    f"{CROPS}/",
    f"{THUMBNAILS}/",
    f"{FACE_CROPS}/",
    f"{REFERENCE_PHOTOS}/",
)


def _validate_cover_photo_path(path: str) -> str:
    """Reject path-traversal and out-of-media inputs. Returns the
    normalised path on success; raises HTTPException otherwise."""
    p = path.strip()
    if not p:
        raise HTTPException(400, "cover photo path is empty")
    if ".." in p.split("/") or p.startswith("/") or p.startswith("\\"):
        raise HTTPException(400, "cover photo path must be media-relative")
    if not any(p.startswith(prefix) for prefix in _COVER_PHOTO_ALLOWED_PREFIXES):
        raise HTTPException(
            400,
            f"cover photo path must start with one of: {', '.join(_COVER_PHOTO_ALLOWED_PREFIXES)}",
        )
    return p


@identities_router.get("/identities/{global_id}/presence")
async def get_identity_presence(
    global_id: UUID, request: Request, days: int = 7
) -> list[dict[str, Any]]:
    """The identity's presence episodes, newest first: which camera held them,
    since when, until when, on what evidence, and why the stay ended.

    Episodes, not tracks: a track is a span of the tracker keeping lock (a
    seated person fragments into dozens), an episode is the durable stay the
    presence registry maintains — opened by a sustained live verdict, closed
    only on a measured departure signal. This is the record that answers "how
    long was Marko on the patio", so it is what the timeline renders."""
    days = max(1, min(days, 90))
    rows = await request.app.state.pool.fetch(
        """
        SELECT e.camera_id, c.name AS camera_name, e.evidence,
               e.present_since, e.last_confirmed_at, e.departed_at, e.closed_by
        FROM presence_episodes e
        JOIN cameras c ON c.id = e.camera_id
        WHERE e.global_id = $1
          AND e.present_since > now() - make_interval(days => $2)
        ORDER BY e.present_since DESC
        """,
        global_id,
        days,
    )
    return [
        {
            "camera_id": str(r["camera_id"]),
            "camera_name": r["camera_name"],
            "evidence": r["evidence"],
            "present_since": r["present_since"].isoformat(),
            "departed_at": r["departed_at"].isoformat() if r["departed_at"] else None,
            "closed_by": r["closed_by"],
            "duration_s": (
                (r["departed_at"] - r["present_since"]).total_seconds()
                if r["departed_at"]
                else (r["last_confirmed_at"] - r["present_since"]).total_seconds()
            ),
        }
        for r in rows
    ]


@identities_router.get("/identities/{global_id}/label")
async def get_identity_label(global_id: UUID, request: Request) -> dict[str, Any] | None:
    """Return the operator-set label for this identity, or null if none."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        """
        SELECT global_id, name, kind, tags, notes, plate, linked_person, opus_person_id, source,
               ai_described_at, reference_count, reference_embedding,
               cover_photo_path,
               created_by, created_at, updated_at
        FROM identity_labels WHERE global_id = $1
        """,
        global_id,
    )
    if row is None:
        return None
    return _label_row_to_dict(row)


@identities_router.put("/identities/{global_id}/label")
async def upsert_identity_label(
    global_id: UUID,
    payload: _LabelIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Create or update the label. Insert path requires `name`; update path
    keeps existing values for any omitted field. Tags being passed as an
    empty list explicitly *clears* tags — distinguishable from "not sent"
    because pydantic's model_dump(exclude_unset=True) drops absent keys."""
    fields = payload.model_dump(exclude_unset=True)
    pool = request.app.state.pool
    async with pool.acquire() as conn:  # noqa: SIM117 (kept nested on purpose)
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT plate FROM identity_labels WHERE global_id = $1",
                global_id,
            )
            if existing is None:
                if "name" not in fields or not fields["name"]:
                    raise HTTPException(400, "`name` required when creating a new label")
                row = await conn.fetchrow(
                    """
                    INSERT INTO identity_labels (global_id, name, kind, tags, notes, plate,
                                                 affiliation, resident, species, linked_person,
                                                 opus_person_id, created_by, source)
                    VALUES ($1, $2, $3, COALESCE($4, '{}'::text[]), $5, $6,
                            COALESCE($7, 'unknown'), COALESCE($8, false), $9, $10, $11, $12, 'manual')
                    RETURNING global_id, name, kind, tags, notes, plate, linked_person, affiliation, resident,
                              species, source, ai_described_at, reference_count,
                              reference_embedding, cover_photo_path, opus_person_id,
                              created_by, created_at, updated_at
                    """,
                    global_id,
                    fields["name"],
                    fields.get("kind"),
                    fields.get("tags"),
                    fields.get("notes"),
                    fields.get("plate"),
                    fields.get("affiliation"),
                    fields.get("resident"),
                    fields.get("species"),
                    fields.get("linked_person"),
                    fields.get("opus_person_id"),
                    user.id,
                )
            else:
                # Build a sparse UPDATE. asyncpg has no clean "update only
                # these columns" helper, so we do it by hand. Every field
                # the caller omitted stays at its prior value. We also
                # promote source 'ai' → 'ai_reviewed' on the first manual
                # edit so the UI can stop showing the "draft" affordance.
                set_parts: list[str] = []
                args: list[Any] = []
                for k in (
                    "name",
                    "kind",
                    "tags",
                    "notes",
                    "plate",
                    "affiliation",
                    "resident",
                    "species",
                    "linked_person",
                    "opus_person_id",
                ):
                    if k in fields:
                        args.append(fields[k])
                        set_parts.append(f"{k} = ${len(args)}")
                # Auto-promote source on any real edit so the gallery
                # stops showing the "draft AI" affordance once the
                # operator has reviewed.
                if set_parts:
                    set_parts.append(
                        "source = CASE WHEN source = 'ai' THEN 'ai_reviewed' ELSE source END"
                    )
                if not set_parts:
                    # Nothing to update; return the existing row.
                    row = await conn.fetchrow(
                        """
                        SELECT global_id, name, kind, tags, notes, plate, linked_person,
                               affiliation, resident, species, opus_person_id,
                               reference_count, reference_embedding, created_by,
                               created_at, updated_at
                        FROM identity_labels WHERE global_id = $1
                        """,
                        global_id,
                    )
                else:
                    args.append(global_id)
                    row = await conn.fetchrow(
                        f"""
                        UPDATE identity_labels SET {", ".join(set_parts)}
                        WHERE global_id = ${len(args)}
                        RETURNING global_id, name, kind, tags, notes, plate, linked_person, affiliation, resident,
                                  species, opus_person_id, reference_count, reference_embedding,
                                  created_by, created_at, updated_at
                        """,  # noqa: S608
                        *args,
                    )
            # Labeling promotes this identity's tracks into the 60-day
            # retention tier (vs the default 30 for unlabeled). Use
            # GREATEST so we never shorten an existing deadline — a
            # track that already qualifies for the 90-day reference
            # tier stays there.
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
                LABELLED,
            )
            # An enrolled plate reaches the reads that predate it. The normal
            # flow is enrol-from-yesterday's-visit: the read already exists,
            # unmatched, and the episode it named shows a raw registration.
            # A CORRECTED (or cleared) plate reaches them too: everything the
            # old value matched is un-matched first, or the fix would change
            # the gallery while every past conclusion kept standing.
            if "plate" in fields:
                old_plate = existing["plate"] if existing is not None else None
                if fields.get("plate") != old_plate:
                    await conn.execute(
                        "UPDATE plate_reads SET global_id = NULL "
                        "WHERE global_id = $1",
                        global_id,
                    )
                    # The read stays on the row, so the raw registration
                    # renders until the re-match below decides otherwise.
                    await conn.execute(
                        "UPDATE place_occupancy SET global_id = NULL "
                        "WHERE released_at IS NULL AND global_id = $1 "
                        "AND evidence = 'plate'",
                        global_id,
                    )
                await _rematch_plate_reads(conn)
    log.info("identity label upsert: %s by %s (%s)", global_id, user.username, list(fields.keys()))
    return _label_row_to_dict(row)


async def _rematch_plate_reads(conn: Any) -> None:
    """Re-run the gallery rule over recent unmatched reads and let every row
    that hangs off a read follow: the read itself, the track it was read on,
    and the episodes it named — closed history freely, the open one only when
    the identity is not already standing somewhere else."""
    gallery_rows = await conn.fetch(
        "SELECT global_id, plate FROM identity_labels "
        "WHERE plate IS NOT NULL AND plate <> ''"
    )
    gallery = {str(r["global_id"]): r["plate"] for r in gallery_rows}
    if not gallery:
        return
    reads = await conn.fetch(
        "SELECT id, plate_text, track_id FROM plate_reads "
        "WHERE global_id IS NULL AND read_at > now() - interval '30 days'"
    )
    for r in reads:
        hit = match_gallery(r["plate_text"], gallery)
        if hit is None:
            continue
        gid = UUID(hit.key)
        await conn.execute(
            "UPDATE plate_reads SET global_id = $2 WHERE id = $1", r["id"], gid
        )
        if r["track_id"] is not None:
            await conn.execute(
                "UPDATE tracks SET global_id = $2, identity_source = 'plate' "
                "WHERE id = $1",
                r["track_id"], gid,
            )
        # evidence='plate' keeps a DEMOTED episode out: it holds its read as
        # history, but the join already proved that car stands elsewhere.
        await conn.execute(
            "UPDATE place_occupancy SET global_id = $2 "
            "WHERE plate_read_id = $1 AND released_at IS NOT NULL "
            "AND global_id IS NULL AND evidence = 'plate'",
            r["id"], gid,
        )
        await conn.execute(
            """
            UPDATE place_occupancy SET global_id = $2
             WHERE plate_read_id = $1 AND released_at IS NULL
               AND global_id IS NULL AND evidence = 'plate'
               AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                                WHERE o2.released_at IS NULL
                                  AND o2.global_id = $2)
            """,
            r["id"], gid,
        )
        log.info("plate read %s retro-matched to %s (%r)",
                 r["id"], gid, r["plate_text"])


class _CoverPhotoIn(BaseModel):
    """Operator's chosen cover photo. `photo_path` is a media-relative
    string like "crops/<uuid>.jpg" or "reference_photos/<uuid>.jpg",
    or null to clear the override and fall back to the default
    most-recent-crop heuristic."""

    photo_path: str | None = None


@identities_router.put("/identities/{global_id}/cover-photo")
async def set_identity_cover_photo(
    global_id: UUID,
    payload: _CoverPhotoIn,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Pin or clear the identity's cover image.

    Pinning: the chosen path is stored on identity_labels and overrides
    the auto-picked latest_crop in both the detail and list endpoints.
    No FK / file-exists check — a stale path gracefully degrades to the
    auto-pick when the underlying file is gone, which is preferable to
    cascading the delete across two unrelated tables.

    Clearing (photo_path = null): drops the override so the detail
    endpoint goes back to "most recent track's crop_path, then most
    recent reference photo" priority.

    Auto-creates a stub label row if none exists yet — same pattern as
    the reference-photo upload endpoint, so the operator can pick a
    cover before naming the identity."""
    normalised: str | None = None
    if payload.photo_path is not None:
        normalised = _validate_cover_photo_path(payload.photo_path)

    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchrow(
            "SELECT 1 FROM identity_labels WHERE global_id = $1",
            global_id,
        )
        if existing is None:
            await conn.execute(
                """
                    INSERT INTO identity_labels
                        (global_id, name, created_by, cover_photo_path)
                    VALUES ($1, $2, $3, $4)
                    """,
                global_id,
                f"Identitet {str(global_id)[:8]}",
                user.id,
                normalised,
            )
        else:
            await conn.execute(
                """
                    UPDATE identity_labels
                    SET cover_photo_path = $1,
                        updated_at = now()
                    WHERE global_id = $2
                    """,
                normalised,
                global_id,
            )
        row = await conn.fetchrow(
            """
                SELECT global_id, name, kind, tags, notes, plate, linked_person, opus_person_id, source,
                       ai_described_at, reference_count, reference_embedding,
                       cover_photo_path,
                       created_by, created_at, updated_at
                FROM identity_labels WHERE global_id = $1
                """,
            global_id,
        )
    log.info(
        "identity cover photo %s: gid=%s by %s",
        "cleared" if normalised is None else "set",
        global_id,
        user.username,
    )
    return _label_row_to_dict(row)


@identities_router.delete("/identities/{global_id}/label", status_code=204)
async def delete_identity_label(
    global_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    """Remove the label entirely. Photos / reference embedding go with it.
    The underlying tracks + global_id are untouched."""
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        result = await conn.execute(
            "DELETE FROM identity_labels WHERE global_id = $1",
            global_id,
        )
        if result.endswith(" 0"):
            raise HTTPException(404, "label not found")
        # An open place holding this identity keeps its read (the plate is
        # still the plate) but loses the gid — the badge falls back to the
        # raw registration instead of rendering a label that no longer
        # exists.
        await conn.execute(
            "UPDATE place_occupancy SET global_id = NULL "
            "WHERE released_at IS NULL AND global_id = $1",
            global_id,
        )
    log.info("identity label deleted: %s by %s", global_id, user.username)


@identities_router.delete("/identities/{global_id}", status_code=204)
async def delete_identity(
    global_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> None:
    """Full purge of an identity: label, reference photos (DB + JPEGs),
    every track that ever clustered under this gid (cascading samples
    and events via ON DELETE SET NULL — those rows then dangle without
    a gid but keep their other context), and the audit history bound
    to this gid.

    Used by the operator-facing "Izbriši identitet" affordance to wipe
    misclustered identities outright rather than relabelling them. For
    label-only entries (no tracks yet) this is the only sensible
    delete — the gid exists nowhere except `identity_labels`.
    """
    pool = request.app.state.pool
    media_root = request.app.state.config.media_path

    async with pool.acquire() as conn, conn.transaction():
        # Collect reference photo paths before deleting rows so
        # the post-commit unlink loop has something to work with.
        photo_rows = await conn.fetch(
            "SELECT photo_path FROM identity_reference_photos WHERE global_id = $1",
            global_id,
        )
        # Collect track thumb / crop paths as well — every JPEG
        # this identity contributed to disk should go.
        track_rows = await conn.fetch(
            "SELECT thumbnail_path, crop_path FROM tracks WHERE global_id = $1",
            global_id,
        )
        sample_rows = await conn.fetch(
            """SELECT crop_path, face_crop_path
                FROM track_embedding_samples
                WHERE track_id IN (SELECT id FROM tracks WHERE global_id = $1)""",
            global_id,
        )

        # Order: children first (samples / events), then tracks,
        # then label + photos. samples + events have ON DELETE
        # SET NULL on track_id so we wipe them explicitly first.
        await conn.execute(
            """DELETE FROM track_embedding_samples
                WHERE track_id IN (SELECT id FROM tracks WHERE global_id = $1)""",
            global_id,
        )
        await conn.execute(
            """DELETE FROM events
                WHERE track_id IN (SELECT id FROM tracks WHERE global_id = $1)""",
            global_id,
        )
        await conn.execute(
            "DELETE FROM tracks WHERE global_id = $1",
            global_id,
        )
        # The registry keeps its episodes — occupancy happened — but the
        # occupant no longer exists anywhere, so the rows drop the gid. An
        # episode named by a read keeps it (and shows the raw plate); reads
        # keep their text and drop only the match.
        await conn.execute(
            "UPDATE place_occupancy SET global_id = NULL, "
            "evidence = CASE WHEN plate_read_id IS NULL THEN 'unknown' "
            "                ELSE evidence END "
            "WHERE global_id = $1",
            global_id,
        )
        await conn.execute(
            "UPDATE plate_reads SET global_id = NULL WHERE global_id = $1",
            global_id,
        )
        # Presence cannot drop its gid the way occupancy does — an episode IS
        # a person being somewhere, and there is no such thing as one belonging
        # to nobody. So it goes with the person.
        await conn.execute(
            "DELETE FROM presence_episodes WHERE global_id = $1",
            global_id,
        )
        await conn.execute(
            "DELETE FROM identity_reference_photos WHERE global_id = $1",
            global_id,
        )
        label_result = await conn.execute(
            "DELETE FROM identity_labels WHERE global_id = $1",
            global_id,
        )
        # Audit rows whose payload references this gid. Stored as
        # jsonb so we filter by `payload->>'gid'`. Cleaning these
        # keeps the per-identity history page from showing rows
        # that point at a no-longer-existing record.
        await conn.execute(
            """DELETE FROM identity_audit
                WHERE payload->>'gid' = $1
                   OR payload->>'from' = $1
                   OR payload->>'into' = $1""",
            str(global_id),
        )

    # If absolutely nothing was deleted, the gid doesn't exist anywhere.
    if not photo_rows and not track_rows and label_result.endswith(" 0"):
        raise HTTPException(404, f"identity {global_id} not found")

    # Unlink JPEGs after the transaction commits. Crash here leaves
    # orphan files at worst, never half-deleted DB state.
    def _unlink(rel: str | None) -> None:
        if not rel:
            return
        try:
            (media_root / rel).unlink(missing_ok=True)
        except Exception:
            log.exception("failed to unlink %s", rel)

    for r in photo_rows:
        _unlink(r["photo_path"])
    for r in track_rows:
        _unlink(r["thumbnail_path"])
        _unlink(r["crop_path"])
    for r in sample_rows:
        _unlink(r["crop_path"])
        _unlink(r["face_crop_path"])

    log.info(
        "identity deleted: gid=%s tracks=%d photos=%d samples=%d by %s",
        global_id,
        len(track_rows),
        len(photo_rows),
        len(sample_rows),
        user.username,
    )
