-- An open place carries its occupant again, and the database keeps it single.
--
-- 078 made the identity a join through `track_id` so a plate read minutes after
-- the car parked would reach the place for free. It did — and it also made the
-- one-car-one-place rule unenforceable, because two tracks resolving to one
-- identity is invisible to a constraint on either. 076's unique index was
-- dropped in the same migration. On 22.08 a shed track read the plate of the
-- car standing at P2 and P1 bound to it: one identity, two places, fifteen
-- hours, and every reader rendered it without complaint.
--
-- The occupant is back on the row, but nothing in Python puts it there. A
-- trigger projects it from `tracks` inside the naming transaction, so the eight
-- writers of tracks.global_id — including an operator merging two identities in
-- the UI — are covered without any of them knowing this table exists. The
-- index is the backstop for any path that ever bypasses the trigger.

-- What decided a name, so `evidence` stops being inferred from the mere
-- presence of a plate string on the track.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS identity_source text
    CHECK (identity_source IN ('plate', 'body', 'face'));

-- Backfill the occupant of every open episode from its track.
UPDATE place_occupancy o
   SET global_id = t.global_id,
       evidence  = CASE
           WHEN t.global_id IS NULL THEN 'unknown'
           WHEN COALESCE(t.plate_text, '') <> '' THEN 'plate'
           ELSE 'body'
       END
  FROM tracks t
 WHERE o.track_id = t.id
   AND o.released_at IS NULL
   AND EXISTS (SELECT 1 FROM identity_labels il WHERE il.global_id = t.global_id);

-- Self-heal before the index exists: where one identity now holds several open
-- places, the OLDEST episode keeps the name. A car cannot have arrived twice,
-- and the later claim is the one built on the weaker evidence.
UPDATE place_occupancy o
   SET global_id = NULL, evidence = 'unknown'
 WHERE o.released_at IS NULL
   AND o.global_id IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM place_occupancy o2
        WHERE o2.released_at IS NULL
          AND o2.global_id = o.global_id
          AND o2.id <> o.id
          AND o2.occupied_since < o.occupied_since);

CREATE UNIQUE INDEX IF NOT EXISTS place_occupancy_open_vehicle_idx
    ON place_occupancy (global_id)
 WHERE released_at IS NULL AND global_id IS NOT NULL;

-- The projection. No policy lives here: it copies the identity onto every open
-- episode bound to the track, and writes NULL instead when that identity
-- already stands somewhere else. There is nothing to grade and nothing to
-- order, so there is nothing here a unit test could pin.
CREATE OR REPLACE FUNCTION place_occupant_project() RETURNS trigger AS $$
DECLARE
    taken boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM place_occupancy o2
         WHERE o2.released_at IS NULL
           AND o2.global_id = NEW.global_id
           AND o2.track_id IS DISTINCT FROM NEW.id
    ) INTO taken;

    IF taken THEN
        RAISE WARNING 'place: % already stands elsewhere — episodes on track % stay unnamed',
            NEW.global_id, NEW.id;
    END IF;

    UPDATE place_occupancy o
       SET global_id = CASE WHEN taken THEN NULL ELSE NEW.global_id END,
           evidence  = CASE
               WHEN taken OR NEW.global_id IS NULL THEN 'unknown'
               ELSE COALESCE(NEW.identity_source, 'body')
           END
     WHERE o.track_id = NEW.id
       AND o.released_at IS NULL
       AND EXISTS (SELECT 1 FROM identity_labels il WHERE il.global_id = NEW.global_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tracks_project_occupant
    AFTER UPDATE OF global_id, identity_source ON tracks
    FOR EACH ROW EXECUTE FUNCTION place_occupant_project();

-- The same projection from the other side: an episode that binds (or rebinds)
-- to a track takes that track's identity in the same statement.
CREATE OR REPLACE FUNCTION place_occupancy_adopt() RETURNS trigger AS $$
DECLARE
    src   tracks%ROWTYPE;
    taken boolean;
BEGIN
    IF NEW.released_at IS NOT NULL OR NEW.track_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO src FROM tracks WHERE id = NEW.track_id;
    IF NOT FOUND OR src.global_id IS NULL
       OR NOT EXISTS (SELECT 1 FROM identity_labels il WHERE il.global_id = src.global_id) THEN
        NEW.global_id := NULL;
        NEW.evidence  := 'unknown';
        RETURN NEW;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM place_occupancy o2
         WHERE o2.released_at IS NULL
           AND o2.global_id = src.global_id
           AND o2.id IS DISTINCT FROM NEW.id
    ) INTO taken;
    NEW.global_id := CASE WHEN taken THEN NULL ELSE src.global_id END;
    NEW.evidence  := CASE WHEN taken THEN 'unknown'
                          ELSE COALESCE(src.identity_source, 'body') END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER place_occupancy_adopt
    BEFORE INSERT OR UPDATE OF track_id ON place_occupancy
    FOR EACH ROW EXECUTE FUNCTION place_occupancy_adopt();
