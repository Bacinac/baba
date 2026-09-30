"""Who a finalized track is — decided in one order, written by nobody here.

Face first, and only off a face big enough to tell people apart. Then the
verdict live naming already earned for this very track. Then appearance: an
enrolled pet by its body references, otherwise the same-day anonymous body
cluster, otherwise a fresh identity seeded from the track itself. Last, a
result that is still anonymous may inherit a name through the face-anchored
body chain.

`Naming` carries the decision and the evidence behind it; finalize files it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from baba_core import PET_GROUP, VEHICLE_GROUP, group_for

from baba_event_manager.config import EventManagerConfig
from baba_event_manager.face_anchor import (
    FACE_ID_MIN_PX,
    anchor_sql,
    chain_enabled,
    chain_params,
)
from baba_event_manager.identity_rules import face_match_survives_rivals

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Naming:
    global_id: UUID
    # 'match' | 'new' | 'face_anchor_body'
    decision: str
    # The face match that named the track: a curated reference or an anonymous
    # past track's cluster.
    face: dict | None = None
    # The same-day anonymous cluster the track joined by body.
    body_cluster: dict | None = None
    # The live verdict adopted was a FACE verdict.
    live_face: bool = False
    anchor_dist: float | None = None
    anchor_xcam: bool = False

    @property
    def source(self) -> str:
        return "face" if self.decision == "match" and self.face is not None else "body"

    @property
    def dist(self) -> float | None:
        if self.face is not None:
            return float(self.face["dist"]) if self.face["dist"] is not None else None
        if self.body_cluster is not None:
            return float(self.body_cluster["dist"])
        return None


def reference_kind_for_class(
    class_id: int, config: EventManagerConfig
) -> tuple[str | None, float]:
    """Map a track's majority class to its non-person identity `kind` and the
    body-reference distance threshold. Persons return (None, 0.0) — they never
    body-reference-match; identity is face-only. So do VEHICLES: measured on
    this property, a stranger's dark car sits closer to the enrolled reference
    than the owner's own car (0.128 against 0.170), so appearance naming a
    vehicle is a machine for stamping the household name on visitors — the
    plate names a vehicle, or nothing does. dog/cat both map to 'pet' (the
    detector flips between them, so the pool is intentional — nearest
    reference decides which pet)."""
    if class_id in PET_GROUP:
        return "pet", config.reid_body_reference_pet_threshold
    return None, 0.0


async def identity_fits_class(conn, global_id, class_id: int) -> bool:
    """Whether an identity may name a track of this class.

    An anonymous identity fits anything — it is only a cluster key. A named one
    has to agree: `match_kind` is the pool an identity matches in, and a
    vehicle class may only take a vehicle's, a pet class a pet's, everything
    else a person's.
    """
    kind = await conn.fetchval(
        "SELECT COALESCE(match_kind, kind) FROM identity_labels WHERE global_id = $1",
        global_id,
    )
    if kind is None:
        return True
    if class_id in VEHICLE_GROUP:
        return kind == "vehicle"
    if class_id in PET_GROUP:
        return kind in ("pet", "object")
    return kind == "person"


async def name_track(
    conn,
    config: EventManagerConfig,
    live: Any,
    cam_slug: str,
    cam_id: UUID,
    local_tid: int,
    class_id: int,
    track_id: UUID,
    has_samples: bool,
) -> Naming:
    """Identity policy: FACE (or the operator) is the ONLY cross-session
    identity signal. BODY appearance (DINOv2/OSNet) is used only for
    within-visit tracking, never to say WHO across visits — outfits change and
    different people look alike, so body identity is unreliable."""
    face = await _face_match(conn, config, cam_slug, local_tid, track_id) if has_samples else None
    if face is not None:
        # Reference rows carry id=NULL, so the identity is their global_id; a
        # pre-re-ID neighbour has no global_id yet and lends its own id.
        naming = Naming(face["global_id"] or face["id"], "match", face=face)
    elif (
        live is not None
        and (verdict := live.verdict(cam_slug, local_tid)) is not None
        # A local track id wraps and is reused, so a stale verdict can be
        # waiting under a number a different subject now carries. A car must
        # not finalize as a person because the number it was given used to
        # belong to one.
        and await identity_fits_class(conn, verdict[0], class_id)
    ):
        # No finalize-time FACE match, but LIVE naming already earned a verdict
        # for this very track (face, or body anchored to a face-verified track)
        # using MIN over ALL its samples — a strictly better comparison than
        # the single canonical embedding the finalize chain would re-derive
        # with. Re-deriving only ever LOSES information (observed: live said
        # Marko at 0.212, finalize's canonical distance sat past the 0.25 gate →
        # badge and Activity disagreed). Adopt the live verdict — and its
        # PROVENANCE with it: a live FACE verdict passed the same face gate over
        # the same samples this chain would use, so the track is face-verified
        # by it. A live BODY verdict stays body and must never anchor a further
        # chain.
        gid, name, source = verdict
        naming = Naming(gid, "match", live_face=source == "face")
        log.info("finalize adopted live verdict: track %s -> %r (%s)", track_id, name, source)
    else:
        naming = await _by_appearance(conn, config, cam_id, class_id, track_id, has_samples)
    if has_samples and chain_enabled(config):
        await _face_anchor(conn, config, class_id, track_id, naming)
    return naming


async def _face_match(
    conn, config: EventManagerConfig, cam_slug: str, local_tid: int, track_id: UUID
) -> dict | None:
    new_face = await conn.fetchrow(
        "SELECT face_embedding IS NOT NULL AS has_face, face_px FROM tracks WHERE id = $1",
        track_id,
    )
    if not (new_face and new_face["has_face"]):
        return None
    # A face too small to identify anybody does not get to try. Confidence
    # answers "is this a face"; only its size answers "is there enough of one
    # here to tell people apart", and below the floor the answer disagrees with
    # the enrolled photographs one time in five.
    if (new_face["face_px"] or 0.0) < FACE_ID_MIN_PX:
        log.info(
            "face too small to identify on cam=%s track=%s: %.0f px, floor %.0f — "
            "falling through to the body anchor",
            cam_slug, local_tid, new_face["face_px"] or 0.0, FACE_ID_MIN_PX,
        )
        return None
    # ORDER BY: a labeled candidate within threshold wins over a closer
    # unlabeled match. Without this, fragmented visits of the same person
    # create a chain of "phantom" gids whenever the absolute-closest neighbour
    # happens to be an earlier unlabeled track rather than a labeled Marko track —
    # face_d on the same camera between consecutive splits is noisy enough
    # (0.40-0.60) to outrank labeled candidates at higher but still
    # under-threshold distances. The labeled preference re-anchors continuity
    # to the named identity.
    face_rows = await conn.fetch(
        """
        WITH new_emb AS (
            -- All of this track's DECENT-score face crops, not the single
            -- best-score one: identity is decided by the best-MATCHING face,
            -- and the top-score crop often isn't it (patio 07-16: scores
            -- 0.666/0.683/0.695 → dists 0.891/0.428/0.804). MIN over these vs
            -- each candidate.
            SELECT face_embedding FROM track_embedding_samples
            WHERE track_id = $1
              AND face_embedding IS NOT NULL
              AND face_embedding_model = $6
              AND (face_score IS NULL OR face_score >= $4)
        ),
        face_tracks AS (
            SELECT t.id, t.global_id,
                   (SELECT min(t.face_embedding <=> ne.face_embedding)
                    FROM new_emb ne) AS dist,
                   'track'::text AS source
            FROM tracks t
            WHERE t.id <> $1
              AND t.face_embedding IS NOT NULL
              AND t.face_embedding_model = $6
              -- And the same floor on the OTHER side. A track named off a
              -- 35-pixel face is not evidence of anything, and leaving it in
              -- the pool is how one bad call becomes tomorrow's proof: the
              -- four nearest neighbours that gave a patio face Ana's name on
              -- 31.08 were themselves labelled exactly this way.
              AND t.face_px >= $5
              AND t.ended_at > now() - make_interval(days => $2)
              -- A NAME never comes from here. Asking which past face is
              -- nearest is not the same question as which enrolled person
              -- this is, and there are thousands of past faces against a
              -- handful of portraits, so the portrait loses: Vesna walked
              -- onto the patio on 31.08 and her own portrait, at 0.756, lost
              -- to an old track of Ana's at 0.663. Past tracks stay in the
              -- pool for the one thing they are evidence of — that two
              -- ANONYMOUS sightings are the same person — and curated
              -- evidence does the naming.
              AND NOT EXISTS (
                  SELECT 1 FROM identity_labels il
                   WHERE il.global_id = t.global_id
              )
        ),
        face_refs AS (
            -- Curated evidence, and all of it: the enrolled photographs plus
            -- the vector an operator attached to the identity itself. The live
            -- path has always asked exactly this pool; it is the same
            -- question, so it is the same answer.
            SELECT NULL::uuid AS id, r.global_id,
                   MIN(r.face_embedding <=> ne.face_embedding) AS dist,
                   'reference'::text AS source
            FROM (
                SELECT global_id, face_embedding
                  FROM identity_reference_photos
                 WHERE face_embedding IS NOT NULL
                   AND face_embedding_model = $6
                UNION ALL
                SELECT global_id, face_embedding
                  FROM identity_labels
                 WHERE face_embedding IS NOT NULL
                   AND face_embedding_model = $6
            ) r
            CROSS JOIN new_emb ne
            GROUP BY r.global_id
        ),
        candidates AS (
            -- Curated evidence is the only labelled kind now: a named past
            -- track cannot reach this union at all.
            SELECT c.*, (c.source = 'reference') AS labeled
            FROM (SELECT * FROM face_tracks UNION ALL SELECT * FROM face_refs) c
            WHERE c.dist IS NOT NULL
        )
        SELECT id, global_id, dist, source FROM candidates
        ORDER BY
            (labeled AND dist < $3) DESC,
            dist ASC,
            (source = 'reference') DESC
        LIMIT 16
        """,
        track_id,
        config.reid_window_days,
        config.reid_face_cosine_threshold,
        config.reid_face_sample_min_score,
        FACE_ID_MIN_PX,
        config.face_embedding_model,
    )
    hit, rival_gap = face_match_survives_rivals(list(face_rows or []))
    if hit is None:
        if face_rows:
            log.info(
                "face re-ID declined on cam=%s track=%s: nearest %.3f and the next "
                "identity only %.3f further — the neighbourhood cannot tell them apart",
                cam_slug, local_tid, float(face_rows[0]["dist"]),
                rival_gap if rival_gap is not None else -1.0,
            )
        return None
    if hit["dist"] is None or hit["dist"] >= config.reid_face_cosine_threshold:
        return None
    # Confidence floor on TRACK-TO-TRACK matches: a neighbour-face match on a
    # below-floor crop is not a real face confirmation — it's two poor crops of
    # (usually) the same person that happen to embed close. Reject it so the
    # track falls through to the body anchor, which names the SAME identity
    # with honest "body" provenance instead of a green-badge face claim.
    # Reference matches are exempt: a crop that lands under threshold against a
    # clean enrolled portrait is a good crop by construction.
    if hit["source"] == "track" and config.reid_confident_face_score > 0:
        best_face_score = await conn.fetchval(
            "SELECT max(face_score) FROM track_embedding_samples "
            "WHERE track_id = $1 AND face_embedding IS NOT NULL "
            "AND face_embedding_model = $2",
            track_id, config.face_embedding_model,
        )
        if best_face_score is None or best_face_score < config.reid_confident_face_score:
            return None
    return dict(hit, modality="face")


async def _by_appearance(
    conn,
    config: EventManagerConfig,
    cam_id: UUID,
    class_id: int,
    track_id: UUID,
    has_samples: bool,
) -> Naming:
    # An operator-enrolled PET reference is the only appearance path that names
    # a non-person. Match MIN over that identity's reference photos and adopt
    # the nearest only when it is under the threshold AND clearly closer than
    # the runner-up (the margin keeps a cat off a dog's card). Runs before
    # anonymous clustering so a known pet becomes ITSELF, not a fresh card.
    ref_kind, ref_thresh = reference_kind_for_class(class_id, config)
    if has_samples and ref_kind is not None and ref_thresh > 0:
        ref_rows = await conn.fetch(
            """
            WITH me AS (SELECT embedding FROM tracks WHERE id = $1)
            SELECT il.global_id,
                   MIN(rp.body_embedding <=> (SELECT embedding FROM me)) AS dist
            FROM identity_reference_photos rp
            JOIN identity_labels il
              ON il.global_id = rp.global_id
             AND COALESCE(il.match_kind, il.kind) = $2
            WHERE rp.body_embedding IS NOT NULL
            GROUP BY il.global_id
            ORDER BY dist ASC
            LIMIT 2
            """,
            track_id,
            ref_kind,
        )
        if ref_rows and ref_rows[0]["dist"] is not None and ref_rows[0]["dist"] < ref_thresh:
            best_d = ref_rows[0]["dist"]
            runner_d = ref_rows[1]["dist"] if len(ref_rows) > 1 else None
            if runner_d is None or (runner_d - best_d) >= config.reid_body_reference_margin:
                return Naming(ref_rows[0]["global_id"], "match")

    # Fold into an existing ANONYMOUS cluster by BODY appearance — but only
    # inside the envelope where appearance == identity: same camera, same day
    # (reid_body_cluster_window_hours), tight OSNet distance, and the candidate
    # must NOT be named/enrolled (those stay face-only). This consolidates
    # repeat visits of one unnamed subject into a single card without ever
    # risking the body→named over-merge vortex.
    if (
        has_samples
        and config.reid_body_cluster_window_hours > 0
        and config.reid_body_cluster_threshold > 0
    ):
        cluster = await conn.fetchrow(
            """
            WITH me AS (SELECT embedding FROM tracks WHERE id = $1)
            SELECT t.global_id,
                   (t.embedding <=> (SELECT embedding FROM me)) AS dist
            FROM tracks t
            WHERE t.id <> $1
              AND t.embedding IS NOT NULL
              AND t.camera_id = $2
              AND t.class_id = ANY($3::int[])
              AND t.ended_at > now() - make_interval(hours => $4)
              AND NOT EXISTS (
                  SELECT 1 FROM identity_labels il
                  WHERE il.global_id = t.global_id)
              AND NOT EXISTS (
                  SELECT 1 FROM identity_reference_photos rp
                  WHERE rp.global_id = t.global_id)
            ORDER BY dist ASC
            LIMIT 1
            """,
            track_id,
            cam_id,
            [int(c) for c in group_for(class_id)],
            config.reid_body_cluster_window_hours,
        )
        if (
            cluster is not None
            and cluster["dist"] is not None
            and cluster["dist"] < config.reid_body_cluster_threshold
        ):
            return Naming(cluster["global_id"], "match", body_cluster=cluster)

    # A fresh anonymous identity seeded from the track's own id, so global_id
    # is always set. The operator names anonymous clusters in the UI.
    return Naming(track_id, "new")


async def _face_anchor(
    conn, config: EventManagerConfig, class_id: int, track_id: UUID, naming: Naming
) -> None:
    """If the identity chosen so far is still ANONYMOUS but this track's body
    strongly matches a recent FACE-VERIFIED track of a NAMED identity, adopt
    that identity instead. Whichever path chose it — a face match to an
    anonymous neighbour (the patio 07:18 case), a body-cluster join or a fresh
    seed — all three leave it anonymous. The anchor being face_verified is the
    anti-drift guard: body chains anchor only to real face confirmations,
    never to each other."""
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM identity_labels WHERE global_id = $1)",
        naming.global_id,
    ):
        return
    anchor = await conn.fetchrow(
        anchor_sql(
            "SELECT id AS key, camera_id, "
            "COALESCE(ended_at, now()) AS at, embedding, "
            "$2::int[] AS class_ids FROM tracks WHERE id = $1",
            first_param=3,
            extra="t.id <> $1",
        ),
        track_id,
        [int(c) for c in group_for(class_id)],
        *chain_params(config),
    )
    if anchor is not None and anchor["dist"] is not None:
        naming.global_id = anchor["global_id"]
        naming.decision = "face_anchor_body"
        naming.anchor_dist = float(anchor["dist"])
        naming.anchor_xcam = not anchor["same_cam"]
