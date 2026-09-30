-- Body-based re-ID is no longer allowed to silently assign a cross-session
-- identity (that produced the "everyone collapses into the one person with
-- live face captures" vortex: a few face-verified tracks seeded a verified
-- body pool, then the loose 0.50 DINOv2 threshold absorbed hundreds of
-- strangers). From now on ONLY face (live or enrolled reference) or the
-- operator may assign a named identity across sessions. A body match instead
-- becomes a *suggestion* the operator confirms or rejects here.
--
-- This table is the review queue. The hot identity path never reads it; it is
-- only written at finalize (one row per body candidate) and read by the
-- Suggestions UI.

CREATE TABLE IF NOT EXISTS identity_suggestions (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The track proposed for linking. Dropped if the track is retained out.
    track_id            uuid        NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    -- Candidate identity (a global_id). Not a hard FK — same as
    -- identity_reference_photos, referential integrity is app-managed.
    suggested_global_id uuid        NOT NULL,
    -- Cosine distance at the time the suggestion was raised (lower = closer).
    dist                real        NOT NULL,
    -- Which signal raised it. Today always 'body'; column kept for future
    -- low-confidence face suggestions.
    modality            text        NOT NULL DEFAULT 'body',
    -- Whether the candidate came from a past track or an enrolled reference.
    source              text        NOT NULL DEFAULT 'track',
    status              text        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    resolved_by         uuid                 REFERENCES users(id) ON DELETE SET NULL
);

-- One decision per (track, candidate): once rejected it stays rejected, so a
-- re-finalize / re-eval never re-proposes the same pair (ON CONFLICT DO NOTHING).
CREATE UNIQUE INDEX IF NOT EXISTS identity_suggestions_track_cand_idx
    ON identity_suggestions (track_id, suggested_global_id);

-- Pending queue, ordered by confidence (closest first) for bulk-accept-by-threshold.
CREATE INDEX IF NOT EXISTS identity_suggestions_pending_idx
    ON identity_suggestions (status, dist)
    WHERE status = 'pending';

-- "Suggestions pointing at this identity".
CREATE INDEX IF NOT EXISTS identity_suggestions_candidate_idx
    ON identity_suggestions (suggested_global_id)
    WHERE status = 'pending';
