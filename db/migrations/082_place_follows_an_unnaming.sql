-- A place follows its track's identity in BOTH directions.
--
-- The projection only touched an episode when the incoming global_id already
-- carried a label, so merging a named identity into an unlabelled one left the
-- episode holding the name that had just moved away — and the name then stood
-- on two open places at once, which is the thing the unique index exists to
-- make impossible and the trigger quietly walked around.

CREATE OR REPLACE FUNCTION place_occupant_project() RETURNS trigger AS $$
DECLARE
    taken    boolean;
    labelled boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM place_occupancy o2
         WHERE o2.released_at IS NULL
           AND o2.global_id = NEW.global_id
           AND o2.track_id IS DISTINCT FROM NEW.id
    ) INTO taken;

    SELECT EXISTS (
        SELECT 1 FROM identity_labels il WHERE il.global_id = NEW.global_id
    ) INTO labelled;

    IF taken THEN
        RAISE WARNING 'place: % already stands elsewhere — episodes on track % stay unnamed',
            NEW.global_id, NEW.id;
    END IF;

    UPDATE place_occupancy o
       SET global_id = CASE WHEN taken OR NOT labelled THEN NULL ELSE NEW.global_id END,
           evidence  = CASE
               WHEN taken OR NOT labelled OR NEW.global_id IS NULL THEN 'unknown'
               ELSE COALESCE(NEW.identity_source, 'body')
           END
     WHERE o.track_id = NEW.id
       AND o.released_at IS NULL;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Self-heal what the old projection already left behind: an open episode whose
-- bound track no longer carries a labelled identity is not that identity's.
UPDATE place_occupancy o
   SET global_id = NULL, evidence = 'unknown'
 WHERE o.released_at IS NULL
   AND o.global_id IS NOT NULL
   AND o.track_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM tracks t
        WHERE t.id = o.track_id
          AND t.global_id = o.global_id
          AND EXISTS (SELECT 1 FROM identity_labels il WHERE il.global_id = t.global_id));
