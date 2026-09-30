-- When a track's face was last re-read from the recording, at full size.
--
-- Faces are embedded off the SHM ring, which is downscaled: patio records
-- 3040x1368 and the ring carries 1280, so 2.38x of the picture is thrown away
-- before anything looks at it. Measured on 31.08, the operator standing on the
-- patio looking straight into the camera for fifteen seconds: the best face the
-- ring could offer was 57.9 px and the system stayed silent, while the SAME
-- moment cut from the segment is 124 x 146 px and matches his enrolled
-- photographs at 0.242 — with the next person 0.614 behind.
--
-- The same lesson as plate reading, which stopped depending on the ring for
-- exactly this reason. So a person track whose ring face was too small to
-- identify anybody gets its face read again from the recording, minutes later,
-- with nothing waiting on it — and this column is what keeps that from being
-- done twice.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS face_native_at timestamptz;

COMMENT ON COLUMN tracks.face_native_at IS
    'When this track''s face was re-read from the recording at native '
    'resolution. NULL means it has not been, not that there was nothing to '
    'find.';

CREATE INDEX IF NOT EXISTS tracks_face_native_pending_idx
    ON tracks (ended_at DESC)
    WHERE class_id = 0 AND face_native_at IS NULL;
