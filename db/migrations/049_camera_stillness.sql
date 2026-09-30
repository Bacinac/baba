-- Per-camera stillness tuning for the tracker's motion-state machine.
--
-- stillness_ratio — the object-relative radius (fraction of bbox diagonal)
-- the centroid may wander while still counting as "not moving". Occlusion-
-- split scenes (a carport pillar cutting a car's bbox into two alternating
-- geometries) need a wider radius than the global default or the object
-- loops active↔stationary forever and never parks.
--
-- park_seconds — continued stillness before the stationary→parked promotion.
--
-- NOT NULL with the historical defaults: every camera row carries its own
-- effective value (single source of truth for the UI slider); the
-- BABA_TRACKER_STATIC_MOVE_RATIO / _PARK_THRESHOLD_MS envs remain only the
-- fallback for a camera the tracker hasn't loaded from the DB yet.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS stillness_ratio real NOT NULL DEFAULT 0.08;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS park_seconds integer NOT NULL DEFAULT 60;
ALTER TABLE cameras ADD CONSTRAINT cameras_stillness_range
    CHECK (stillness_ratio >= 0.01 AND stillness_ratio <= 1.0);
ALTER TABLE cameras ADD CONSTRAINT cameras_park_seconds_range
    CHECK (park_seconds >= 5 AND park_seconds <= 3600);
