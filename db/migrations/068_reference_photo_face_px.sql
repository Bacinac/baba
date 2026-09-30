-- Record how big the face actually is on a reference photo.
--
-- Matching is MIN-over-references, so a reference whose face is a handful of
-- pixels can only ever pull a stranger under the threshold — it can never
-- prevent a false accept. Until now nothing recorded face SIZE, only whether a
-- face was found at all, so those references were indistinguishable from good
-- ones after the fact and the "purge faceless" action could not see them.
--
-- Measured on a live 30-reference identity: 25 sit at 47-128 px inter-ocular
-- distance, and 5 (auto-picked body crops — a torso with a head in the corner)
-- sit at 13-31 px against the ~35 px the ArcFace template consumes.
--
-- NULL means "enrolled before this column existed"; it is not the same as 0,
-- which means a face was found but is unusably small.
ALTER TABLE identity_reference_photos
    ADD COLUMN IF NOT EXISTS face_px real;

COMMENT ON COLUMN identity_reference_photos.face_px IS
    'Shorter side of the detected face box, in pixels of the stored photo. '
    'NULL = enrolled before the column existed. Quality signal for enrolment '
    'floors and for purging references that can only cause false accepts.';
