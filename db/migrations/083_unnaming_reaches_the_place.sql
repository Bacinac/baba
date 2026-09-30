-- Two ways an identity leaves that never reached the place it was standing in.
--
-- The projection fires on `tracks`. Deleting a LABEL touches no track, so the
-- open episode kept the name the operator had just removed — and because
-- `place_occupant_project` refuses to project an identity that already stands
-- on an open episode, that stale row then blocked the same identity from ever
-- being projected onto a place again.
--
-- Deleting an identity deletes its tracks, and place_occupancy had no foreign
-- key, so the episode was left pointing at a row that no longer exists. The
-- retention sweeper prunes tracks the same way. Nothing rebound it.

UPDATE place_occupancy o
   SET global_id = NULL, evidence = 'unknown'
 WHERE o.released_at IS NULL
   AND o.global_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM identity_labels il WHERE il.global_id = o.global_id);

CREATE OR REPLACE FUNCTION place_forget_unlabelled() RETURNS trigger AS $$
BEGIN
    UPDATE place_occupancy o
       SET global_id = NULL, evidence = 'unknown'
     WHERE o.released_at IS NULL AND o.global_id = OLD.global_id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS identity_label_forgotten ON identity_labels;
CREATE TRIGGER identity_label_forgotten
    AFTER DELETE ON identity_labels
    FOR EACH ROW EXECUTE FUNCTION place_forget_unlabelled();

UPDATE place_occupancy o
   SET track_id = NULL
 WHERE o.track_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.id = o.track_id);

ALTER TABLE place_occupancy
    DROP CONSTRAINT IF EXISTS place_occupancy_track_fk;
ALTER TABLE place_occupancy
    ADD CONSTRAINT place_occupancy_track_fk
    FOREIGN KEY (track_id) REFERENCES tracks (id) ON DELETE SET NULL;
