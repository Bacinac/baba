"""Live identity for tracks that are still on screen.

Why this exists
---------------
Identity used to be assigned only at track finalize. Measured on the patio
camera 2026-07-15:

    17:22:21  track starts
    17:22:24  best face lands, distance 0.354 to Marko's references  <- we KNOW
    17:22:44  track ends
    17:22:50  finalize assigns the identity                        <- we SAY

The face gives the person away in ~3 seconds; we sat on it for 27 and only
published "1 person" in the meantime. For a consumer driving automation off
`baba.state.<uuid>` that is the difference between "turn the light on for Marko"
and a note that Marko was here, delivered after he left.

Two signals, and their precedence
----------------------------------
* FACE (authoritative). Resolves "which of the roster is this" against curated
  face evidence (enrolled reference photos + each named identity's canonical
  face vector). Face is outfit- and day-invariant, so it is the only signal
  trusted to name across sessions — and here it is trusted to OVERRIDE a body
  guess. It never links to anonymous clusters.
* BODY (provisional). While a track is on screen with no confident face yet,
  its body may match a recently face-verified track of a named identity — the
  live twin of the face-anchored body chain finalize runs, rule shared with it
  in `face_anchor` (patio 2026-07-16: a shirtless session sat 0.185 from three
  face-confirmed Marko tracks). This names presence in real time so DIDA doesn't
  wait for a clear frontal face that a sitting person may never turn.

Precedence (Ivo's rule, 2026-07-16): a CONFIDENT face always wins on
disagreement. "Confident" = the best face crop this track produced cleared the
distance gate AND scored at least `confident_face_score` (SCRFD face_score,
migration 060). So:
  - a confident face on a blank track names it (face, sticky);
  - a confident face on a body-guessed track overrides it — whether it agrees
    (upgrade) or disagrees (the face is right, the body was a lookalike);
  - a body match names a track only while no confident face has spoken, and
    stays provisional: every tick still asks the face.
A body-named track is therefore never final until a face confirms it or the
track leaves; the fence against a lookalike hijacking a named identity is the
distance, window and runner-up margin `face_anchor` defines, plus the face's
standing override.

Stickiness
----------
A face verdict is kept for the track's lifetime — a later, worse-angled face
must not un-name someone mid-visit. A body verdict is kept too, but remains
open to a confident face on every subsequent tick.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from baba_core import PERSON_CLASS_ID

from baba_event_manager.face_anchor import (
    FACE_ID_MIN_PX,
    anchor_sql,
    chain_enabled,
    chain_params,
)
from baba_event_manager.identity_rules import survives_rivals

log = logging.getLogger(__name__)

# Don't re-query a still-open track more often than this. A person walking into
# frame produces a face within a few seconds; polling faster just adds DB
# round-trips per active person per camera for an answer that rarely changes —
# the embedder is debounced at 500 ms and only writes a new face when it finds
# one. Applies to BOTH the face retry on an unresolved/body track and the body
# probe on an unresolved one.
_RETRY_INTERVAL_S = 2.0

# Drop cache entries this long after a track was last seen. Local track ids are
# per-camera and monotonic, so an entry that stops being asked about belongs to
# a track that ended; without a sweep the map grows for the process lifetime.
_GC_AFTER_S = 300.0

# How far back a live track's own samples can be. Without a bound the query
# matched every unclaimed sample ever left under this camera and local id —
# and samples are left: finalize claims only those inside the track's finished
# window, so anything outside it keeps `track_id IS NULL` forever. Measured
# 05.09: 252,260 unclaimed samples going back to 06.08, 1,994 of them carrying
# a face. They named new tracks by "face" that then finalized owning no face at
# all — a body-named row wearing a face badge, and eligible to anchor a body
# chain as a "real face confirmation". A live track's own face is seconds old.
_LIVE_SAMPLE_HORIZON_S = 900

# Match provenance. (Pets/vehicles are no longer resolved here — the tracker
# stamps their identity at the source; see baba_tracker.identity_stamp.)
_SRC_FACE = "face"
_SRC_BODY = "body"


@dataclass(slots=True, frozen=True)
class LiveMatch:
    global_id: UUID
    name: str
    dist: float


@dataclass(slots=True)
class _Entry:
    match: LiveMatch | None = None
    source: str = ""  # _SRC_FACE (final) | _SRC_BODY (provisional) | "" (none)
    last_try: float = 0.0
    last_seen: float = 0.0


class LiveIdentityMatcher:
    """Resolves on-screen person tracks to named identities: face wins, body
    fills the gap until it does."""

    def __init__(self, get_config: Callable[[], Any]) -> None:
        # A PROVIDER, not a snapshot. Every threshold below is read through it
        # at use time, so when the operator moves one in the UI the matcher
        # sees it on the next track — no setter, and none to forget when the
        # next threshold is added.
        self._cfg = get_config
        # Per-track state, and the GC clock for it. These were lost when this
        # constructor was rewritten to take a config provider: the thresholds
        # moved to properties and the rest of the body went with them, so every
        # resolve() raised AttributeError on _last_gc and live identity
        # matching was dead for the whole afternoon. Nothing caught it because
        # the handler logs the traceback and carries on.
        self._by_track: dict[tuple[str, int], _Entry] = {}
        self._last_gc = 0.0

    @property
    def _threshold(self) -> float:
        # Same face gate finalize uses; resolved from face_recognition_settings.
        return self._cfg().reid_face_cosine_threshold

    @property
    def _confident_face_score(self) -> float:
        # A face must score at least this to OVERRIDE a body guess.
        return self._cfg().reid_confident_face_score

    @property
    def _sample_min_score(self) -> float:
        # Match over the track's face samples scoring at least this, not just
        # the single best-score one — see config.reid_face_sample_min_score.
        return self._cfg().reid_face_sample_min_score

    async def resolve(
        self,
        conn,
        camera_id: str,
        camera_uuid: UUID,
        local_track_ids: list[int],
    ) -> dict[UUID, tuple[str, str]]:
        """Return `{global_id: (name, source)}` for the named people currently
        on screen, where source is "face" or "body".

        `local_track_ids` are the camera's live PERSON tracks that passed the
        ever_active gate — callers must not hand us static clutter, for the same
        reason the snapshot's other vision fields are gated on it.
        """
        now = time.monotonic()
        self._gc(now)
        out: dict[UUID, tuple[str, str]] = {}

        def emit(m: LiveMatch, source: str) -> None:
            # Face beats body when the same person is carried on two live
            # tracks (one face-named, one body-guessed): the badge/DIDA source
            # must reflect the strongest evidence for that identity, not the
            # last track iterated.
            prev = out.get(m.global_id)
            if prev is None or (prev[1] == _SRC_BODY and source == _SRC_FACE):
                out[m.global_id] = (m.name, source)

        for tid in local_track_ids:
            key = (camera_id, tid)
            entry = self._by_track.get(key)
            if entry is None:
                entry = _Entry()
                self._by_track[key] = entry
            entry.last_seen = now

            # A face verdict is final — never re-litigated.
            if entry.source == _SRC_FACE:
                assert entry.match is not None
                emit(entry.match, _SRC_FACE)
                continue

            # Throttle DB work; keep publishing any provisional body verdict.
            if now - entry.last_try < _RETRY_INTERVAL_S:
                if entry.match is not None:
                    emit(entry.match, entry.source)
                continue
            entry.last_try = now

            resolved = await self._resolve_one(conn, camera_id, camera_uuid, tid, entry)
            if resolved is not None:
                emit(resolved, entry.source)

        return out

    async def _resolve_one(
        self,
        conn,
        camera_id: str,
        camera_uuid: UUID,
        tid: int,
        entry: _Entry,
    ) -> LiveMatch | None:
        # 1) FACE — always tried, always able to win.
        try:
            face = await self._match_face(conn, camera_uuid, tid)
        except Exception:
            log.exception("live face match failed cam=%s track=%s", camera_id, tid)
            face = None

        if face is not None:
            match, score = face
            if entry.match is None:
                # Blank track — a face naming it is the established behaviour.
                entry.match, entry.source = match, _SRC_FACE
                log.info(
                    "live identity: cam=%s track=%s -> %r by face dist=%.3f",
                    camera_id, tid, match.name, match.dist,
                )
                return match
            if entry.source == _SRC_BODY and score >= self._confident_face_score:
                # A confident face overrides the provisional body guess —
                # agreeing (upgrade to final) or disagreeing (face is right).
                if match.global_id != entry.match.global_id:
                    log.info(
                        "live identity: cam=%s track=%s body-guess %r OVERRIDDEN "
                        "by confident face -> %r (face dist=%.3f score>=%.2f)",
                        camera_id, tid, entry.match.name, match.name,
                        match.dist, self._confident_face_score,
                    )
                entry.match, entry.source = match, _SRC_FACE
                return match
            # A weak face (below the confidence floor) does not unseat a body
            # guess — keep the body verdict and keep asking.
            if entry.match is not None:
                return entry.match

        # 2) No usable face. Keep any provisional body verdict...
        if entry.match is not None:
            return entry.match

        # 3) ...otherwise try to name the track by BODY now.
        if not chain_enabled(self._cfg()):
            return None
        try:
            body = await self._match_body_anchor(conn, camera_uuid, tid)
        except Exception:
            log.exception("live body anchor failed cam=%s track=%s", camera_id, tid)
            return None
        if body is not None:
            entry.match, entry.source = body, _SRC_BODY
            log.info(
                "live identity: cam=%s track=%s -> %r by body anchor dist=%.3f "
                "(provisional; a confident face can still override)",
                camera_id, tid, body.name, body.dist,
            )
            return body
        return None

    async def _match_face(self, conn, camera_uuid: UUID, local_track_id: int):
        """This live track's face(s) vs curated named face evidence, MIN over
        the track's decent-score face samples (not just the single best-score
        one — the top-score crop is often not the best identity match). Returns
        (LiveMatch, best_face_score) or None; the score is the best crop the
        track produced (what the confident-override gate reads)."""
        rows = await conn.fetch(
            """
            WITH faces AS (
                -- Every decent face THIS live track has produced. A live track's
                -- samples are unclaimed (track_id IS NULL) until finalize; the
                -- track_id filter is load-bearing, not cosmetic: local_track_id
                -- is a per-camera counter that WRAPS and gets reused, so without
                -- it this picks up a stale reused-id track's samples (a face
                -- from hours ago) and matches — or fails to match — against the
                -- wrong body entirely. Scope to the current, unfinalized track.
                SELECT face_embedding, face_score
                FROM track_embedding_samples
                WHERE camera_id = $1
                  AND local_track_id = $2
                  AND track_id IS NULL
                  -- This track's own, not everything ever orphaned under its
                  -- number. See _LIVE_SAMPLE_HORIZON_S.
                  AND captured_at > now() - make_interval(secs => $5)
                  AND face_embedding IS NOT NULL
                  AND (face_score IS NULL OR face_score >= $3)
                  -- Confidence says this is a face; only its size says there is
                  -- enough of one to tell people apart. Below the floor the
                  -- verdict disagrees with the enrolled photographs one time in
                  -- five, and it is the live path that puts a name on screen.
                  AND face_px >= $4
                  -- Only vectors from the active embedder: a face embedding is
                  -- comparable ONLY within the model that produced it (migration
                  -- 032). NULL is legacy/unknown space, not the active one —
                  -- cosine across spaces is noise and would put a wrong name on
                  -- a face, which is worse than no name. Such rows are excluded
                  -- until recomputed, never matched on.
                  AND face_embedding_model = $6
            ),
            named_face AS (
                SELECT global_id, face_embedding
                FROM identity_reference_photos
                WHERE face_embedding IS NOT NULL
                  AND face_embedding_model = $6
                UNION ALL
                SELECT global_id, face_embedding
                FROM identity_labels
                WHERE face_embedding IS NOT NULL
                  AND face_embedding_model = $6
            )
            SELECT nf.global_id,
                   il.name,
                   MIN(nf.face_embedding <=> f.face_embedding) AS dist,
                   (SELECT max(face_score) FROM faces) AS face_score
            FROM named_face nf
            JOIN identity_labels il ON il.global_id = nf.global_id
            CROSS JOIN faces f
            WHERE il.name IS NOT NULL
            GROUP BY nf.global_id, il.name
            ORDER BY dist ASC
            -- Two, because the question is not who is nearest but whether
            -- anybody else is just as near. Asking for one is how this path
            -- came to answer a different question than finalize did.
            LIMIT 2
            """,
            camera_uuid,
            local_track_id,
            self._sample_min_score,
            FACE_ID_MIN_PX,
            _LIVE_SAMPLE_HORIZON_S,
            self._cfg().face_embedding_model,
        )
        row = rows[0] if rows else None
        if row is None or row["dist"] is None or row["dist"] >= self._threshold:
            return None
        if not survives_rivals(
            row["global_id"],
            float(row["dist"]),
            float(rows[1]["dist"]) if len(rows) > 1 and rows[1]["dist"] is not None
            else None,
        ):
            log.info(
                "live face naming declined on cam=%s track=%s: nearest %.3f and "
                "another identity at %.3f — the same neighbourhood finalize "
                "refuses to decide",
                camera_uuid, local_track_id,
                float(row["dist"]), float(rows[1]["dist"]),
            )
            return None
        match = LiveMatch(global_id=row["global_id"], name=row["name"], dist=float(row["dist"]))
        # face_score can be NULL on an older sample; treat as 0 so it can name a
        # blank track but never clear the override floor.
        score = float(row["face_score"]) if row["face_score"] is not None else 0.0
        return match, score

    async def _match_body_anchor(self, conn, camera_uuid: UUID, local_track_id: int):
        """This live track's best-framed body vs recent face-verified named
        tracks. Returns a LiveMatch or None.

        The rule is `face_anchor`'s, shared with finalize and the resweep — the
        only thing local to being live is what counts as "me": the samples of a
        track that has not finalized yet."""
        row = await conn.fetchrow(
            anchor_sql(
                # ALL body samples of THIS live track (track_id IS NULL until
                # finalize), one row each — the chain takes the nearest, which
                # is not always the best-framed crop. The track_id filter is
                # load-bearing: local_track_id wraps and is reused, so without
                # it a stale reused-id track's sample from hours ago competes
                # with the current one — the live miss that left a present
                # person unnamed.
                """
                SELECT $2::bigint AS key, $1::uuid AS camera_id, now() AS at,
                       embedding, $3::int[] AS class_ids
                FROM track_embedding_samples
                WHERE camera_id = $1
                  AND local_track_id = $2
                  AND track_id IS NULL
                  -- Same bound as the face side, for the same reason: an
                  -- orphaned body sample under this number is not this track.
                  AND captured_at > now() - make_interval(secs => $4)
                  AND embedding IS NOT NULL
                """,
                first_param=5,
            ),
            camera_uuid,
            local_track_id,
            [PERSON_CLASS_ID],
            _LIVE_SAMPLE_HORIZON_S,
            *chain_params(self._cfg()),
        )
        if row is None or row["dist"] is None:
            return None
        return LiveMatch(global_id=row["global_id"], name=row["name"], dist=float(row["dist"]))

    def transfer(self, camera_id: str, old_tid: int, new_tid: int) -> None:
        """Carry a verdict across an anchor handover: the tracker just proved
        the two ids are one subject, so the new id starts with the old one's
        identity instead of a blank entry. Without this the badge (and DIDA)
        blinked anonymous for a re-probe round on every seam — for a fact the
        system had already established. A face verdict stays face (final); a
        body verdict stays provisional and re-litigable, exactly as it was."""
        entry = self._by_track.pop((camera_id, old_tid), None)
        if entry is not None and entry.match is not None:
            self._by_track[(camera_id, new_tid)] = entry
            log.info(
                "live identity: cam=%s verdict %r carried %s → %s (anchor handover)",
                camera_id, entry.match.name, old_tid, new_tid,
            )

    def verdict(self, camera_id: str, local_track_id: int) -> tuple[UUID, str, str] | None:
        """The sticky live verdict for a track, for FINALIZE to adopt.

        Live naming resolves with MIN over ALL of the track's samples — a
        strictly better comparison than finalize's single canonical embedding
        (live body-anchor hit 0.212 on a track whose canonical embedding sat
        past the 0.25 gate → the badge said Marko, the Activity entry said
        anonymous). The verdict was already earned under the full fences
        (face gate, or body anchored to a face-verified track), so finalize
        re-deriving it with a weaker metric only ever LOSES information.
        Returns (global_id, name, source) or None. Entries outlive the track
        by the GC grace (~5 min), which covers finalize's ~5 s timeout lag."""
        entry = self._by_track.get((camera_id, local_track_id))
        if entry is None or entry.match is None or not entry.source:
            return None
        return entry.match.global_id, entry.match.name, entry.source

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 60.0:
            return
        self._last_gc = now
        stale = [k for k, e in self._by_track.items() if now - e.last_seen > _GC_AFTER_S]
        for k in stale:
            self._by_track.pop(k, None)
