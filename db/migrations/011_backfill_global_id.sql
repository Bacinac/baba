-- Backfill `tracks.global_id` for rows finalized before cross-camera re-ID
-- shipped. The runtime logic (event-manager) sets global_id at finalize
-- time — either to a matched neighbour's gid or to the track's own id —
-- but older rows pre-date that and carry NULL.
--
-- One-time cleanup: every embedded track without a global_id becomes its
-- own seed identity. Newer finalizes can still kNN-match against these
-- because the embedding column was set at the time, just gid was missing.
-- We deliberately don't try to retro-merge here; the Identities UI is the
-- right place for human review of merge candidates.

UPDATE tracks
SET global_id = id
WHERE global_id IS NULL
  AND embedding IS NOT NULL;
