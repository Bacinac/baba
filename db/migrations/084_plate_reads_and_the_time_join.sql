-- A plate read becomes an event of its own, and a place is named by time.
--
-- West reads the plate of every car that drives the approach, and the place
-- registry records every fill within the following minute — measured over 21
-- days: zero cases of two places filling within three minutes of each other,
-- and a ±3 minute window catches every read a ±10 minute window does. Yet the
-- two facts were joined through four serial mechanisms: the read was stamped
-- onto a TRACK, a geometric binder matched the track's resting box against
-- hand-drawn polygons, a trigger projected the track's identity onto the
-- episode, and three more triggers kept the projection honest under merges,
-- unnamings and deletes. Each mechanism had its own month of fixes; the chain
-- failed whenever any link did.
--
-- This migration replaces the chain with the join the data supports: a read is
-- a row carrying the moment the car showed its plate, and an unnamed episode
-- takes the read that immediately precedes its fill. Nothing else relates them.

CREATE TABLE IF NOT EXISTS plate_reads (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id  uuid        NOT NULL REFERENCES cameras (id) ON DELETE CASCADE,
    -- The track whose footage was read, for audit. Soft: retention prunes
    -- tracks long before anyone stops caring which car drove in that day.
    track_id   uuid        REFERENCES tracks (id) ON DELETE SET NULL,
    -- When the car showed the plate — the median frame time of the readings
    -- that carried the vote, NOT when the sweep got around to deciding. The
    -- decision lands minutes later (the covering segment must close first),
    -- and joining on the decision time would misplace every arrival.
    read_at    timestamptz NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    -- The consensus string. Never empty: "decided, nothing readable" stays
    -- where it always lived, as tracks.plate_text = ''.
    plate_text text        NOT NULL CHECK (plate_text <> ''),
    ocr_score  real,
    readings   int         NOT NULL,
    -- The enrolled identity the reading matched, or NULL for a plate nobody
    -- has enrolled — which is a first-class answer: the badge shows the raw
    -- plate of an unknown visitor instead of nothing. Soft reference; enrolling
    -- the plate later back-fills it.
    global_id  uuid,
    crop_path  text
);

CREATE INDEX IF NOT EXISTS plate_reads_read_at_idx ON plate_reads (read_at DESC);
CREATE INDEX IF NOT EXISTS plate_reads_gid_idx
    ON plate_reads (global_id) WHERE global_id IS NOT NULL;
-- One read per track, so the reader's retry after a mid-write failure is
-- idempotent (its terminal sentinel is written after the read, not before).
CREATE UNIQUE INDEX IF NOT EXISTS plate_reads_track_once_idx
    ON plate_reads (track_id) WHERE track_id IS NOT NULL;

-- An episode records WHICH READ named it. One read names one episode, ever —
-- the index makes consume-once an invariant rather than a loop's intention.
ALTER TABLE place_occupancy
    ADD COLUMN IF NOT EXISTS plate_read_id uuid REFERENCES plate_reads (id)
        ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS place_occupancy_read_once_idx
    ON place_occupancy (plate_read_id) WHERE plate_read_id IS NOT NULL;

-- The projection machinery goes. All of it: the identity of a place no longer
-- has anything to do with what any track thinks it is called, so the eight
-- writers of tracks.global_id stop reaching this table entirely, and the two
-- operator paths that must reach it (merge, label delete) now do so in their
-- own route code, where the action is visible.
DROP TRIGGER IF EXISTS tracks_project_occupant ON tracks;
DROP TRIGGER IF EXISTS place_occupancy_adopt ON place_occupancy;
DROP TRIGGER IF EXISTS identity_label_forgotten ON identity_labels;
DROP FUNCTION IF EXISTS place_occupant_project();
DROP FUNCTION IF EXISTS place_occupancy_adopt();
DROP FUNCTION IF EXISTS place_forget_unlabelled();

-- The geometric binding goes with it. `came_to_rest` keeps the one bit the
-- resting box was also answering — "did this car park, or drive through" —
-- which the presence sweep asks to tell a departure from an arrival.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS came_to_rest boolean;
ALTER TABLE tracks ALTER COLUMN came_to_rest SET DEFAULT false;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tracks' AND column_name = 'rest_bbox') THEN
        UPDATE tracks SET came_to_rest = true WHERE rest_bbox IS NOT NULL;
    END IF;
END $$;
-- Never NULL: the presence sweep asks `t.came_to_rest AND ...`, and a NULL
-- there turns the whole guard NULL and silently swallows the departure.
UPDATE tracks SET came_to_rest = false WHERE came_to_rest IS NULL;
DROP INDEX IF EXISTS tracks_rest_idx;
ALTER TABLE tracks DROP COLUMN IF EXISTS rest_bbox;
CREATE INDEX IF NOT EXISTS tracks_came_to_rest_idx
    ON tracks (ended_at DESC) WHERE came_to_rest;

ALTER TABLE place_occupancy DROP CONSTRAINT IF EXISTS place_occupancy_track_fk;
ALTER TABLE place_occupancy DROP COLUMN IF EXISTS track_id;

-- Clean what the trigger era left behind: episodes "named" by an anonymous
-- self-seeded gid that never had a label (four of the six body-evidence
-- episodes in the measured window). An identity nobody can render is not a
-- name.
UPDATE place_occupancy o
   SET global_id = NULL, evidence = 'unknown'
 WHERE o.global_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM identity_labels il
                    WHERE il.global_id = o.global_id);

-- 'inherited' was admitted by the CHECK and written by nothing, ever — but
-- this migration runs on every instance, so any row that somehow carries it
-- is converted rather than left to abort the constraint swap. 'body' stays
-- admissible for the historical rows that carry it, but nothing writes it
-- any more: appearance cannot tell this property's dark cars apart
-- (measured: a stranger's car at 0.128 against the owner's own 0.170), so a
-- vehicle is named by its plate or not at all.
UPDATE place_occupancy SET evidence = 'unknown' WHERE evidence = 'inherited';
ALTER TABLE place_occupancy DROP CONSTRAINT IF EXISTS place_occupancy_evidence_check;
ALTER TABLE place_occupancy
    ADD CONSTRAINT place_occupancy_evidence_check
    CHECK (evidence IN ('plate', 'body', 'unknown'));

-- The 'transition_only' motion gate is gone from the code (its arrived /
-- departed semantics live in the place registry now). A stored rule naming
-- it would no longer resolve to any gate at all — rewrite it to the gate it
-- always aliased.
UPDATE zones
   SET rules = replace(rules::text, '"transition_only"', '"moving_only"')::jsonb
 WHERE rules::text LIKE '%transition_only%';

-- Reads decided by pre-084 code exist only as columns on tracks. Carry the
-- recent ones over so an episode open across the deploy can still be named;
-- read_at falls back to the track's end, which for a parked arrival is the
-- moment the car stopped. Identities ride along only when the PLATE decided
-- them — a body-guessed gid must not become plate evidence.
INSERT INTO plate_reads (camera_id, track_id, read_at, plate_text, ocr_score,
                         readings, global_id, crop_path)
SELECT t.camera_id, t.id, t.ended_at, t.plate_text, t.plate_score, 1,
       CASE WHEN t.identity_source = 'plate' THEN t.global_id END,
       t.plate_crop_path
FROM tracks t
WHERE COALESCE(t.plate_text, '') <> ''
  AND t.ended_at > now() - interval '7 days'
ON CONFLICT (track_id) WHERE track_id IS NOT NULL DO NOTHING;

COMMENT ON TABLE plate_reads IS
    'One row per successful plate consensus on one camera''s view of a pass. '
    'read_at is the moment the car showed the plate; the place registry joins '
    'on it.';
COMMENT ON COLUMN place_occupancy.plate_read_id IS
    'The read that named this episode. Unique among episodes: a read is '
    'consumed at most once.';
