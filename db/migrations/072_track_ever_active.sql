-- Whether this track ever moved under its own power.
--
-- The tracker has always known it (ever_moved carries through the parked-ghost
-- chain) and the event-manager already gates zones and vision fields on it —
-- but it was never persisted, so the Activity feed could not ask. One night
-- measured what that costs: the tracker lost and re-birthed the parked car
-- twenty times under IR, every rebirth finalized as a fresh track, and the
-- feed showed a stream of visits for a car whose parking spot never
-- transitioned once.
--
-- NULL = finalized before this column existed.
ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS ever_active boolean;

COMMENT ON COLUMN tracks.ever_active IS
    'True when the track moved under its own power at some point; false for '
    'a re-detection of something standing still. NULL = pre-column history.';
