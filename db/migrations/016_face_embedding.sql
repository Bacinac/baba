-- Face recognition stack columns. AuraFace v1 outputs 512-d vectors;
-- pgvector column is sized to match and lives alongside the existing
-- 384-d DINOv2 body embedding. Both run in parallel at re-ID time —
-- whichever pool produces the closer match for a given new track
-- wins (with a tie-breaker preferring reference enrollments).
--
-- face_crop_path holds the path to the 112x112 aligned face JPEG that
-- the embedder writes for every sample where YuNet found a face. The
-- canonical "best face" gets copied to tracks.face_crop_path at
-- finalize so the UI can show "system recognised this *face*" instead
-- of just the body crop.

ALTER TABLE track_embedding_samples
    ADD COLUMN IF NOT EXISTS face_embedding vector(512),
    ADD COLUMN IF NOT EXISTS face_crop_path text;

ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS face_embedding vector(512),
    ADD COLUMN IF NOT EXISTS face_crop_path text;

ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS face_embedding vector(512);

-- HNSW indexes — same cosine ops as the body embedding side. Partial
-- so face-less rows don't bloat the index (most pet / vehicle samples
-- won't have a face).
CREATE INDEX IF NOT EXISTS track_embedding_samples_face_idx
    ON track_embedding_samples USING hnsw (face_embedding vector_cosine_ops)
    WHERE face_embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS tracks_face_embedding_idx
    ON tracks USING hnsw (face_embedding vector_cosine_ops)
    WHERE face_embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS identity_labels_face_embedding_idx
    ON identity_labels USING hnsw (face_embedding vector_cosine_ops)
    WHERE face_embedding IS NOT NULL;

-- Widen the audit op constraint to include 'face_match' so the per-
-- identity history can distinguish "matched by face" from the existing
-- body-based 'auto_match'.
ALTER TABLE identity_audit
    DROP CONSTRAINT IF EXISTS identity_audit_op_check;
ALTER TABLE identity_audit
    ADD CONSTRAINT identity_audit_op_check
    CHECK (op IN ('merge', 'split', 'auto_match', 'ai_describe', 'face_match'));
