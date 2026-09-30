-- Persisted bbox crops so the UI shows what the identity *is* (not
-- the wider scene) and the AI describe call sees the same focused
-- pixels the embedder used to compute the canonical embedding.
--
-- Two parallel columns:
--   track_embedding_samples.crop_path — written by the embedder, one
--     JPEG per sample, paths like 'crops/<sample_uuid>.jpg' relative
--     to BABA_MEDIA_HOST.
--   tracks.crop_path — copied at finalize from the highest-confidence
--     sample. Becomes the canonical "this is what the identity looks
--     like" image used by the gallery, detail page, AI describe and
--     reference-photo enrollment defaults.
--
-- thumbnail_path stays as the wider-context image (full frame from the
-- recording at track midpoint); UI shows both — crop as the primary
-- subject, thumbnail as a smaller "the scene where it was captured"
-- panel.

ALTER TABLE track_embedding_samples
    ADD COLUMN IF NOT EXISTS crop_path text;

ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS crop_path text;
