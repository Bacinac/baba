-- The landmark residual (098) asks whether five points can be made to sit on
-- the ArcFace template. A head of hair satisfies that: the detector puts a
-- plausible arrangement of "eyes" and "mouth" on a crown or an ear, and those
-- points fit the template as well as a face turned sideways does. So the crown
-- and the sideways face land in the same 0.17-0.25 band and geometry cannot
-- part them.
--
-- The image can. Alignment warps the candidate onto the template; hand the
-- result back to the SAME detector and ask whether it still sees a face there.
-- A face presented to the camera survives its own normalisation. An ear warped
-- onto a face template is not a face and the detector says so.
--
-- Measured 2026-09-10 over 242 hand-labelled patio crops plus the enrolled
-- portraits as a control:
--
--     faces turned toward the camera   34/38  = 89.5% survive
--     faces pitched steeply down       36/94  = 38.3%
--     ears, profiles, crowns, backs     0/70  =  0.0%
--     not a head at all                 1/40  =  2.5%   (that one, on review,
--                                                        was a mislabel — a
--                                                        blurred face)
--     162 curated portraits           161/162 = 99.4%
--
-- And it buys identity, not tidiness. Against the enrolled portraits at the
-- live 0.75 threshold, samples that survive sit a median 0.639 away and 76.1%
-- match; samples that do not sit at 0.849 and 17.9%. Ears and paving sit at
-- 0.858 — exactly where noise sits.
--
-- Over the whole archive 54.4% of accepted "faces" survive (patio: 26.0%), and
-- 91 of the 246 tracks currently allowed to name a person hold that right on
-- the strength of a crop that is not a face turned toward the camera.
--
-- NULL means nobody asked — rows written before this migration. It must not be
-- read as failure, or every track in the history loses the face it was named
-- by. Ranking puts a known-good face first, an unmeasured one second and a
-- known-bad one last; `face_px` is measured over everything not known to be
-- bad. Same posture as `COALESCE(face_frontality, 0)` in 098.
ALTER TABLE track_embedding_samples ADD COLUMN face_realigned boolean;

COMMENT ON COLUMN track_embedding_samples.face_realigned IS
    'True when the detector still finds a face in this sample''s aligned '
    '112x112 crop. Separates a head seen from above or behind from a face '
    'turned toward the camera, which the landmark residual cannot. NULL = not '
    'measured (pre-099); never read NULL as false.';

-- The canonical-face pick and the native re-read both rank a track's samples
-- by this first, so it joins the existing partial index on the same rows.
CREATE INDEX IF NOT EXISTS track_embedding_samples_face_realigned_idx
    ON track_embedding_samples (track_id, face_realigned)
    WHERE face_embedding IS NOT NULL;
