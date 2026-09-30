-- A place that filled unread is asked again while it stays unnamed. The
-- approach search runs once because that footage never improves; the car
-- standing there is different footage every minute, and at night a plate
-- unreadable on arrival becomes legible the moment the lights go off.
ALTER TABLE place_occupancy ADD COLUMN resident_search_at timestamptz;
