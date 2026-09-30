-- Face quality per sample, so the canonical face for a track can be chosen by
-- how good the FACE is instead of how good the person box was.
--
-- The bug this fixes: event-manager picks a track's canonical face with
--
--     SELECT face_embedding FROM track_embedding_samples
--     WHERE track_id = $1 AND face_embedding IS NOT NULL
--     ORDER BY confidence DESC LIMIT 1
--
-- and `confidence` is the DETECTOR'S PERSON-BOX confidence. It says nothing
-- about the face. The comment above that query already knew — "a high-
-- confidence sample may not have a face (back of head)" — but there was
-- nothing else to order by, because the embedder threw the face detector's
-- own score and geometry away and stored only the vector.
--
-- The effect, measured on the patio camera 2026-07-15: a track yields ~10 face
-- samples, of which typically one is a real frontal face and the rest are ears,
-- shoulders and backs of heads that cleared YuNet's threshold. Ranking them by
-- person-box confidence picks one at random with respect to face quality, so
-- the good face is usually discarded. Same-person face distances then sat at a
-- median 0.870 against 0.886 for random pairs — no identity signal at all, and
-- face re-ID had never once worked here.
--
-- `face_score` is the detector's own confidence for THAT face (YuNet /
-- SCRFD, 0..1). `face_px` is the short side of the detected face box in the
-- crop it was found in — a proxy for inter-ocular distance, which is what
-- decides whether ArcFace has anything to work with (its canonical template
-- places the eyes 35.24 px apart). Both are NULL for rows written before this
-- migration and for samples with no face; ORDER BY must use NULLS LAST.

ALTER TABLE track_embedding_samples
    ADD COLUMN IF NOT EXISTS face_score real;
ALTER TABLE track_embedding_samples
    ADD COLUMN IF NOT EXISTS face_px real;

COMMENT ON COLUMN track_embedding_samples.face_score IS
    'Face detector confidence for this sample''s face (0..1); NULL = no face / pre-060';
COMMENT ON COLUMN track_embedding_samples.face_px IS
    'Short side of the detected face box in px; proxy for inter-ocular distance';

-- The canonical-face pick is per-track and runs once at finalize, so a plain
-- index on (track_id, face_score) covers it without touching the insert-heavy
-- path more than necessary. Partial: only rows that actually carry a face are
-- ever ranked.
CREATE INDEX IF NOT EXISTS track_embedding_samples_face_rank_idx
    ON track_embedding_samples (track_id, face_score DESC)
    WHERE face_embedding IS NOT NULL;
