-- The activity-thumbnail box is now BAKED into the thumbnail JPEG at capture
-- time (event-manager SnapshotBaker draws it on the detection's own SHM
-- frame), so the browser no longer overlays a separate normalised box. The
-- column added in 064 is dead — drop it.
ALTER TABLE tracks DROP COLUMN IF EXISTS thumbnail_bbox;
