-- Online co-training: face confirmations promote a track's body
-- embedding to "verified". Future tracks can match against verified
-- body samples at a looser threshold than unverified ones — DINOv2
-- recognises the outfit alone once face has vouched for it.
--
-- Demotion goes the other way: if face says "different identity" for
-- a track whose nearest body neighbour was previously verified, the
-- neighbour gets demoted back to unverified (the body model was
-- wrong about that outfit, retract trust).
--
-- A small int counter (face_confirmations) keeps the signal even
-- after demotion — useful for the UI and for deciding whether
-- demotion is one strike or N.

ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS face_verified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS face_confirmations INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS face_negations INTEGER NOT NULL DEFAULT 0;

-- Partial index so the "verified body pool" kNN scan stays cheap.
CREATE INDEX IF NOT EXISTS tracks_face_verified_embedding_idx
    ON tracks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL AND face_verified = true;
