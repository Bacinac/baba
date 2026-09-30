-- The Activity feed draws the detected object's box over the thumbnail so the
-- operator sees WHAT was detected (e.g. that a "dog" is the robot mower on the
-- lawn) without pressing play. Store that box on the track.
--
-- Normalised [x1, y1, x2, y2] in [0,1], taken at the LAST-seen frame — the same
-- frame the thumbnail is now captured from, so the box aligns with the pixels.
-- NULL when the thumbnail is a live-snapshot fallback (the frame no longer shows
-- the object at that position) or a tight embedder crop (the crop IS the object)
-- — the UI then draws no box rather than a misplaced one.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS thumbnail_bbox jsonb;
