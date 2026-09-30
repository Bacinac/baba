-- The pipeline ranked a track's faces by the detector's confidence that a face
-- was there, which says nothing about whether that face is turned toward the
-- camera. On a camera mounted high the crown of a head is bigger and more
-- confidently detected than the one-second glance upward, so the glance lost.
-- Worse, what won was sometimes not a head at all: patio carried a 117 px
-- "face" that is a beam of the awning, and because face_px is the best the
-- track ever had, that beam also lifted the track over the 60 px identity
-- floor and kept it out of the native re-read.
--
-- The residual of the landmarks against the ArcFace template separates those
-- cleanly. Measured from both sides on 2026-09-10: of 162 curated reference
-- portraits NONE exceeds 0.29 (median 0.085, max 0.269), and every accepted
-- detection above it was paving, a beam or a wall.
ALTER TABLE track_embedding_samples ADD COLUMN face_frontality real;

COMMENT ON COLUMN track_embedding_samples.face_frontality IS
    'Landmark residual against the ArcFace template, in inter-ocular units. '
    'Lower is more face-on; above baba_core.face.FRONTALITY_MAX the crop is '
    'almost certainly not a face. Ranks candidates, never rejects them.';
