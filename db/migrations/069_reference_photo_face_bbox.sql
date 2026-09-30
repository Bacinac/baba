-- Record WHERE the face is on a reference photo, not just how big it was.
--
-- 068 stored face_px, which answers "is this face usable" but not "which part
-- of this picture is the face". The gallery therefore had to show the stored
-- photo whole — a torso crop with a head in the corner looked exactly like a
-- portrait, and the operator could not see that the face the matcher consumed
-- was a thumbnail in the corner of it.
--
-- Normalised to 0..1 of the stored photo so the box survives any later
-- resize of the JPEG and needs no image dimensions to render.
-- NULL = no face, or enrolled before this column existed.
ALTER TABLE identity_reference_photos
    ADD COLUMN IF NOT EXISTS face_bbox real[];

COMMENT ON COLUMN identity_reference_photos.face_bbox IS
    'Detected face box as [x1,y1,x2,y2] normalised to 0..1 of the stored '
    'photo. NULL = no face, or enrolled before the column existed.';
