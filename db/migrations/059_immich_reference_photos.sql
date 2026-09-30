-- Provenance for reference photos, so an Immich-sourced enrollment can be
-- re-run, audited and selectively undone.
--
-- Background: `identity_reference_photos` rows were until now always
-- operator-originated (multipart upload or promote-from-tracks), and
-- `_persist_reference_photos(source=...)` used its `source` argument only
-- as a log-line tag — nothing was persisted. Pulling photos from an external
-- library (Immich) changes that calculus in two ways:
--
--   1. Idempotency. An import is a bulk, repeatable operation over a person's
--      whole asset list. Without a per-asset key, re-running it after adding
--      new photos to Immich would re-embed and re-insert everything already
--      enrolled. The near-duplicate filter would catch most of it, but that
--      wastes GPU time on hundreds of crops to reach a no-op, and any photo
--      that drifted past the dedup threshold would silently double up.
--
--   2. Undo. A bad import (wrong Immich person picked, library mislabeled)
--      must be revertible without touching the operator's hand-curated
--      uploads for the same identity. `source` gives that a WHERE clause.
--
-- `source` is deliberately free text rather than an enum: the set of
-- enrollment origins is expected to grow (other libraries, bulk folder
-- import) and each new one would otherwise need a migration to extend the
-- type. The values in use today are 'upload', 'from-tracks', 'auto-select'
-- and 'immich'.

ALTER TABLE identity_reference_photos
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'upload';

-- Immich's asset uuid. NULL for every non-Immich row, which is why the
-- uniqueness guard below is partial.
ALTER TABLE identity_reference_photos
    ADD COLUMN IF NOT EXISTS immich_asset_id text;

-- One row per (identity, Immich asset). Scoped to global_id rather than
-- global — the same family photo legitimately enrolls two different people
-- (two faces, two identities, one asset), so a global unique index would
-- reject the second one. Partial so the many NULLs from upload/from-tracks
-- rows don't collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS identity_reference_photos_immich_asset_idx
    ON identity_reference_photos (global_id, immich_asset_id)
    WHERE immich_asset_id IS NOT NULL;

-- Backfill: everything that exists today predates external import and is
-- operator-originated. The DEFAULT already stamped 'upload' on existing
-- rows; this is the explicit statement of that intent for the reader.
-- (No-op in practice — kept so the migration reads as a complete story.)
COMMENT ON COLUMN identity_reference_photos.source IS
    'Enrollment origin: upload | from-tracks | auto-select | immich';
COMMENT ON COLUMN identity_reference_photos.immich_asset_id IS
    'Immich asset uuid this photo was imported from; NULL for local origins';
