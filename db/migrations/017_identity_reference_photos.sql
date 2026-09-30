-- One row per uploaded reference photo so the operator can view and
-- delete individual photos from the UI. Previously `identity_labels`
-- stored only an averaged `reference_embedding` + a `reference_count`
-- integer — the originals were discarded after the upload computed
-- the mean. That left no way to fix a bad upload short of "delete
-- the whole label and start over".
--
-- This table sits next to identity_labels.reference_embedding. The
-- averaged embedding stays where it is for fast kNN lookup at re-ID
-- time — every upload/delete recomputes it from the surviving rows.
-- That keeps the hot query path one indexed read (label row, hits
-- HNSW) rather than an aggregation across multiple photo rows.

CREATE TABLE IF NOT EXISTS identity_reference_photos (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The identity this photo enrolls. Not a hard FK to identity_labels
    -- because labels can be created/deleted independently; we manage
    -- referential integrity at the app level.
    global_id     uuid         NOT NULL,
    -- Original upload, served via /api/reference-photos/<id>.jpg.
    -- Path stored relative to the media root.
    photo_path    text         NOT NULL,
    -- Per-photo embeddings. Body always populated (DINOv2 runs on every
    -- crop unconditionally); face populated only when YuNet detected a
    -- face in this specific photo. When the label's aggregates are
    -- recomputed, photos with NULL face_embedding still contribute to
    -- the body average but not to the face average.
    body_embedding vector(384) NOT NULL,
    face_embedding vector(512),
    -- Bookkeeping.
    uploaded_by    uuid                  REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS identity_reference_photos_gid_idx
    ON identity_reference_photos (global_id, uploaded_at DESC);

-- One-shot backfill: turn the old `reference_count` integer (which we
-- don't have originals for) into a single synthetic row per labelled
-- identity, *only* when there's a `reference_embedding` to preserve.
-- Photo path is empty (UI will render a "(original lost)" placeholder);
-- the embedding itself is what matters for matching, and the operator
-- can re-upload originals to replace this row.
INSERT INTO identity_reference_photos (global_id, photo_path, body_embedding, face_embedding, uploaded_at)
SELECT global_id, '', reference_embedding, face_embedding, COALESCE(updated_at, now())
FROM identity_labels
WHERE reference_embedding IS NOT NULL
ON CONFLICT DO NOTHING;
