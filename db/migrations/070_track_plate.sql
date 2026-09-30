-- What a vehicle's plate read as, and how sure the reading was.
--
-- A plate is to a vehicle what a face is to a person: the one identifier that
-- is not appearance. Body re-ID has never been allowed to name a person for
-- that reason, and it fails on cars for the same one — measured 2026-07-24, a
-- car sat 0.27 from its own enrolled reference against a 0.22 gate, because
-- "dark estate car seen from above" describes half the street.
--
-- `plate_text` is the CONSENSUS over the readings of one track, not any single
-- frame's answer. Measured on west, 25.07: while a car is parked and oblique
-- the OCR returns two dozen different strings for one plate, and one character
-- position agreed across all thirty readings while being wrong. During the
-- approach, the same plate at 96-183 px read ZG9420GZ at confidence 1.0 on
-- seven consecutive frames. The column stores the former only when the latter
-- kind of evidence justifies it.
--
-- `plate_score` is the mean per-character OCR confidence of the winning
-- consensus, recorded for audit rather than as a gate — agreement measures how
-- consistent the viewpoint was, not how right the reading is.
ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS plate_text  text,
    ADD COLUMN IF NOT EXISTS plate_score real,
    ADD COLUMN IF NOT EXISTS plate_crop_path text;

COMMENT ON COLUMN tracks.plate_text IS
    'Consensus plate reading over this track''s frames, normalised to A-Z0-9. '
    'NULL = no plate was read.';
COMMENT ON COLUMN tracks.plate_score IS
    'Mean per-character OCR confidence of the winning consensus. Audit signal, '
    'not a gate.';
COMMENT ON COLUMN tracks.plate_crop_path IS
    'Media-relative path to the plate crop the consensus came from, so an '
    'operator can see what was actually read.';

-- The gallery lookup goes the other way: given a reading, which enrolled plate
-- is it. That is a scan over a handful of labelled vehicles, so it needs no
-- index — but finding whether a plate is already enrolled, on every enrolment
-- and every rename, does.
CREATE INDEX IF NOT EXISTS identity_labels_plate_idx
    ON identity_labels (plate) WHERE plate IS NOT NULL;
