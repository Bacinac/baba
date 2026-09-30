-- An open place stops carrying a copy of who is parked in it.
--
-- `global_id` was written when the spot filled, one to two minutes before the
-- plate can be read — the sweep needs the segment closed first — so the copy
-- was taken while the answer was still unknown, and nothing ever revisited it.
-- P2 stood as `unknown` from 31.07 to 16.08 while west had read ZG9648GZ seven
-- seconds before it filled.
--
-- The occupant of an open episode is now read through `track_id`. The column
-- survives as the FROZEN answer, written when the episode closes, because
-- tracks are pruned by retention and a finished visit must keep its name after
-- its evidence expires.

UPDATE place_occupancy
   SET global_id = NULL, evidence = 'unknown'
 WHERE released_at IS NULL;

-- One car in one place was a uniqueness rule over those copies. Open episodes
-- no longer hold one, and the invariant now comes from the binding itself: a
-- track takes at most one place, checked where the claim is made.
DROP INDEX IF EXISTS place_occupancy_open_vehicle_idx;
