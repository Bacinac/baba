-- A place that filled can read its own arrival, and a place that is occupied
-- always has an episode.
--
-- Two holes closed, both found on the same incident: P1 stood occupied from
-- 26.08 16:13 for nineteen hours with no episode, no name, and no record that
-- anything had arrived — over footage in which the registration reads
-- perfectly at 75-124 px, six frames running, matching the enrolled plate
-- exactly.
--
-- The first hole is that reading a plate needed a TRACK. That arrival
-- produced none (the dwell gate ate it), so nothing ever pointed the reader
-- at the segment. The reader can now be triggered by the fill itself, and
-- this column is how it remembers it tried: one attempt per episode, because
-- the footage does not improve on the second look and a nightly re-scan of
-- the same minute is pure cost.
ALTER TABLE place_occupancy
    ADD COLUMN IF NOT EXISTS plate_search_at timestamptz;

COMMENT ON COLUMN place_occupancy.plate_search_at IS
    'When the recording of this arrival was searched for a plate. Set before '
    'the attempt, so a failed or crashed search is not retried forever.';

-- The second hole is that an episode is opened ONLY on the empty->present
-- transition. If the arrival gate refuses at that instant — or the service is
-- down, or mid-deploy — the place stays `present` with no episode and nothing
-- ever looks again. The registry is supposed to describe the places, so a
-- place reporting `present` with no open episode is a contradiction, and
-- reconcile_places now repairs it on its own interval.
--
-- Repair what is standing right now, so the running instance starts consistent
-- rather than waiting for the next arrival. `occupied_since` is the earliest
-- moment any view of the place committed to `present` — the fill happened
-- then, whatever the registry failed to write at the time.
INSERT INTO place_occupancy (place, evidence, occupied_since)
SELECT sr.place, 'unknown', min(s.current_state_since)
FROM scene_regions sr
JOIN scene_region_status s ON s.region_id = sr.id
WHERE sr.place IS NOT NULL AND sr.enabled AND s.current_state = 'present'
  AND NOT EXISTS (SELECT 1 FROM place_occupancy o
                   WHERE o.place = sr.place AND o.released_at IS NULL)
GROUP BY sr.place
ON CONFLICT (place) WHERE released_at IS NULL DO NOTHING;
