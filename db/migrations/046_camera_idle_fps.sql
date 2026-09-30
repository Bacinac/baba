-- Adaptive detection rate: when idle_fps is set, the ingestor drops the
-- camera's frame feed to idle_fps whenever the tracker reports no activity
-- (no initializing tracks, everything parked) and snaps back to target_fps
-- the moment a fresh detection appears. Full-frame detection NEVER stops —
-- this is a rate change, not motion gating — so stationary subjects stay
-- detected at the idle cadence. NULL = adaptive off (constant target_fps).
ALTER TABLE cameras ADD COLUMN idle_fps INTEGER;
ALTER TABLE cameras ADD CONSTRAINT cameras_idle_fps_range
    CHECK (idle_fps IS NULL OR (idle_fps >= 1 AND idle_fps <= target_fps));
