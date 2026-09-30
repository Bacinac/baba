-- DB-owned retention for `tracks` and their associated media files.
--
-- Without this, there is no authoritative deadline for when an old
-- track + its `crop_path`/`thumbnail_path` jpegs may be deleted, and
-- the only thing pruning media files is external/manual cleanup that
-- leaves the DB rows dangling — every "broken thumb in the UI" bug
-- traces back to that.
--
-- `retain_until` is a *deadline*, not a delete timestamp: a background
-- task scans rows whose deadline has passed and unlinks the media
-- files in lock-step with the DB row delete, so the two tiers can't
-- diverge again. The event-manager sets the column on track finalize
-- using a per-identity tier (unnamed → 30d, named no refs → 60d,
-- named with refs → 90d); reference-photo enrollment bumps existing
-- rows of the same identity into the longer tier so a just-enrolled
-- identity isn't pruned at the next sweep.
--
-- Existing rows get a conservative 30-day deadline counted from their
-- last sighting. After the upcoming factory_reset.sh wipes them
-- anyway, this initial backfill is mostly defensive — but it means
-- you can apply the migration without the reset and still get
-- well-defined retention semantics for everything already in the DB.

ALTER TABLE tracks
  ADD COLUMN IF NOT EXISTS retain_until TIMESTAMPTZ;

-- Conservative backfill: 30 days from each track's last sighting.
-- Event-manager / reference-photo upserts will overwrite this with
-- the proper tiered deadline on new writes; backfilled rows just get
-- a sane default so the pruner has something to act on.
UPDATE tracks
   SET retain_until = ended_at + interval '30 days'
 WHERE retain_until IS NULL;

-- Pruner does `SELECT ... WHERE retain_until < now() LIMIT N` and
-- needs the index to avoid scanning the whole table every 6 hours.
-- Partial index keeps it small (skips NULLs — they shouldn't exist
-- after this migration but the partial predicate is free insurance).
CREATE INDEX IF NOT EXISTS tracks_retain_until_idx
    ON tracks(retain_until)
    WHERE retain_until IS NOT NULL;
