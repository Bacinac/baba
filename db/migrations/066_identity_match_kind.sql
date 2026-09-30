-- Decouple the operator's taxonomy from the matching pool. `kind` was doing
-- two jobs: what the identity IS to the user ("Kosilica" is a device, not a
-- pet) and which body-reference matching pool its references live in (the
-- detector calls the mower a DOG, so it must be matched among dog/cat-class
-- tracks or it silently reverts to being named "Franka"). `match_kind`
-- carries the pool when it must differ from the display kind; NULL means
-- "same as kind" (the overwhelmingly common case). Matching reads
-- COALESCE(match_kind, kind) everywhere.
ALTER TABLE identity_labels ADD COLUMN IF NOT EXISTS match_kind text;
