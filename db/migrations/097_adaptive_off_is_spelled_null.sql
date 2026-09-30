-- `idle_fps = target_fps` reads as adaptive-on and behaves as adaptive-off:
-- the ingestor decimates to the rate it was already publishing, while every
-- consumer that tests `idle_fps IS NOT NULL` believes the camera decimates.
-- The watcher is one such consumer, and patio — idle_fps = target_fps = 4 —
-- opened an active_pinned incident on 30 days out of 30, duty 1.0 with zero
-- active tracks, because a duty of 1.0 is the only thing that rate can
-- produce. The camera was never held up by noise; it had nowhere to decay to.
--
-- Adaptive off is spelled NULL. Rewriting these rows changes no frame rate:
-- a camera at idle_fps = target_fps already publishes target_fps.
UPDATE cameras SET idle_fps = NULL WHERE idle_fps IS NOT NULL AND idle_fps >= target_fps;

-- The ingestor's own contract is `1..target_fps-1` (see CameraWorker); the
-- CHECK said `<=` and let the meaningless value through at every layer.
ALTER TABLE cameras DROP CONSTRAINT IF EXISTS cameras_idle_fps_range;
ALTER TABLE cameras ADD CONSTRAINT cameras_idle_fps_range
    CHECK (idle_fps IS NULL OR (idle_fps >= 1 AND idle_fps < target_fps));
