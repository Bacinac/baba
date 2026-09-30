-- Add a per-camera low-resolution substream URL.
--
-- Dual-stream rationale: detector + tracker run cheaply on the substream
-- (typical 640×360, 5-15 fps), while recognition stages (DINOv2 re-ID, plate
-- OCR, face) crop from the mainstream so they have enough pixels per object.
-- Same sensor → both streams share monotonic source time, so a bbox detected
-- on the sub can be scaled and matched to a near-PTS frame from the main.
--
-- Nullable: cameras without a configured substream fall back to single-stream
-- behaviour (mainstream used for everything). go2rtc, ingestor and embedder
-- pick up the second URL only when it's present.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS substream_url text NULL;
