-- How big the face behind a track's face embedding actually was.
--
-- The identity decision has been made on the detector's CONFIDENCE, which says
-- how sure it is that this is a face — never how many pixels of one it had. A
-- confident detection of a 35-pixel face is still a 35-pixel face, and TopoFR
-- was being asked to name a person from it.
--
-- Measured over 853 face samples on tracks identified by face, 14 days to
-- 31.08, against the identity whose ENROLLED PHOTOGRAPH is nearest:
--
--     under 40 px   363 samples   79% agree
--     40-50         152           85%
--     50-60          77           88%
--     60-80         115           96%
--     80+           146           97%
--
-- The knee is at 60, where the error rate drops four-fold, and 60 is already
-- what the reference side demands of a portrait. So the same number now gates
-- the query side, and `tracks.face_px` is what makes it enforceable: without it
-- a candidate's face size was only knowable by joining every one of its samples.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS face_px real;

COMMENT ON COLUMN tracks.face_px IS
    'Smaller side, in pixels, of the face box behind this track''s '
    'face_embedding. A face under the identification floor may support a '
    'within-visit body anchor but never names anybody.';

-- Backfill from the samples, so the floor applies to the history that is
-- already the candidate pool rather than only to what arrives next.
UPDATE tracks t
   SET face_px = s.px
  FROM (SELECT track_id, max(face_px) AS px
          FROM track_embedding_samples
         WHERE face_embedding IS NOT NULL AND face_px IS NOT NULL
         GROUP BY track_id) s
 WHERE s.track_id = t.id AND t.face_px IS NULL;

CREATE INDEX IF NOT EXISTS tracks_face_px_idx ON tracks (face_px)
    WHERE face_embedding IS NOT NULL;
