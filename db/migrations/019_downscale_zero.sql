-- The decoder treats `downscale_max_edge = 0` as "do not downscale" —
-- keep the mainstream at native resolution all the way into the shm
-- ring. The previous CHECK constraint barred zero, forcing callers to
-- pick a number even when they wanted full-res; relax it so the "off"
-- value is expressible at the data layer too.

ALTER TABLE cameras DROP CONSTRAINT IF EXISTS cameras_downscale_max_edge_check;
ALTER TABLE cameras
    ADD CONSTRAINT cameras_downscale_max_edge_check
    CHECK (downscale_max_edge = 0 OR downscale_max_edge >= 320);
