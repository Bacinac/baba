-- Minimum detection size, as % of the frame (the LARGER of bbox_w/frame_w
-- and bbox_h/frame_h). Detections smaller than this are dropped by the
-- detector before publishing — kills the tiny speck-phantoms (a 25 px
-- "person" in a hedge) that confidence thresholds can't separate from real
-- distant objects. 0 = no size floor. Per-camera NULL inherits the global.
-- IF NOT EXISTS: the .11 deployment applied this content under the
-- colliding filename 047_detection_rules_min_size (renamed to 048 after a
-- parallel session claimed 047) — re-application must be a no-op.
ALTER TABLE detector_global_rules ADD COLUMN IF NOT EXISTS min_box_pct real NOT NULL DEFAULT 0;
ALTER TABLE camera_detection_rules ADD COLUMN IF NOT EXISTS min_box_pct real;
