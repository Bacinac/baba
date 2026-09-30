-- Switch the body re-ID embedding from DINOv2 (384-d) to OSNet (512-d).
--
-- DINOv2 is a general self-supervised feature extractor — it scores ~0.3-4.7%
-- mAP on person re-ID (it clusters by colour/shape, not identity), which is
-- why a long-haired woman matched a bald man at distance 0.32 ("everyone
-- resembles Marko"). OSNet (osnet_x0_25_msmt17) is trained with identity metric
-- learning and outputs 512-d, so every body-embedding column changes width.
--
-- We DROP + re-add (not ALTER TYPE): the existing DINOv2 vectors are both the
-- wrong width AND semantically incompatible, and the source crops were already
-- purged, so there's nothing to re-encode in place. Reference-photo body
-- embeddings get recomputed from the surviving JPEGs by a post-deploy step;
-- track embeddings repopulate as new tracks finalize under OSNet.

-- tracks.embedding (+ its two HNSW indexes)
ALTER TABLE tracks DROP COLUMN embedding;
ALTER TABLE tracks ADD COLUMN embedding vector(512);
CREATE INDEX tracks_embedding_idx ON tracks
    USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;
CREATE INDEX tracks_face_verified_embedding_idx ON tracks
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL AND face_verified = true;

-- per-observation samples
ALTER TABLE track_embedding_samples DROP COLUMN embedding;
ALTER TABLE track_embedding_samples ADD COLUMN embedding vector(512);
CREATE INDEX track_embedding_samples_embedding_idx ON track_embedding_samples
    USING hnsw (embedding vector_cosine_ops);

-- enrolled reference photos (body) — no index in the original schema
ALTER TABLE identity_reference_photos DROP COLUMN body_embedding;
ALTER TABLE identity_reference_photos ADD COLUMN body_embedding vector(512);

-- identity body centroid (+ its HNSW index)
ALTER TABLE identity_labels DROP COLUMN reference_embedding;
ALTER TABLE identity_labels ADD COLUMN reference_embedding vector(512);
CREATE INDEX identity_labels_reference_embedding_idx ON identity_labels
    USING hnsw (reference_embedding vector_cosine_ops) WHERE reference_embedding IS NOT NULL;

-- Pending DINOv2-based suggestions are now meaningless (the model changed).
UPDATE identity_suggestions SET status = 'rejected', resolved_at = now()
    WHERE status = 'pending';
