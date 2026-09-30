-- A track's canonical face carries the model that produced it, like every
-- other stored face vector since 032. Finalize compared tracks.face_embedding
-- against the active model's samples with no way to know which space it was
-- in; after a model switch that is cosine across spaces — noise that can name.
--
-- Backfilled from the sample the vector was copied from. A track whose sample
-- is gone (retention) keeps NULL and is never matched on.
ALTER TABLE tracks_all ADD COLUMN IF NOT EXISTS face_embedding_model text;

CREATE OR REPLACE VIEW tracks AS SELECT * FROM tracks_all WHERE suppressed_reason IS NULL;

UPDATE tracks_all t
   SET face_embedding_model = s.face_embedding_model
  FROM track_embedding_samples s
 WHERE s.track_id = t.id
   AND t.face_embedding IS NOT NULL
   AND t.face_embedding_model IS NULL
   AND s.face_embedding = t.face_embedding;
