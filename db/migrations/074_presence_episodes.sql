-- Where a named person is, recorded when it happens — the person counterpart
-- of place_occupancy (073).
--
-- "Marko is on the patio" has so far existed only as a chain of in-memory state:
-- a tracker anchor holding the seated body, the event-manager's live verdict
-- cache, and a publish-on-change snapshot. Every link is per-track, and the
-- track is the most fragile thing in the pipeline — a restart or an IR flicker
-- blinks the presence, and history is fragmented into whatever tracks
-- happened to survive. Asking "how long was Marko on the patio" meant
-- archaeology over track rows.
--
-- One row per EPISODE of a named identity being present on one camera.
-- `departed_at IS NULL` is the current state; a closed row answers the
-- duration question the way a closed place_occupancy row does. Episodes are
-- opened and confirmed by the LIVE identity verdicts (face wins, body fills
-- the gap) and closed only on a MEASURED departure signal — never on track
-- death, which is the exact mistake the false-"Odlazak" incident documented.
--
-- Named identities only. An anonymous person cannot be told apart from the
-- next anonymous person across track churn, so an "episode" for one would be
-- false precision — they stay what they are today, a person_count.
CREATE TABLE IF NOT EXISTS presence_episodes (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- identity_labels.global_id; soft reference, same policy as everywhere.
    global_id         uuid        NOT NULL,
    camera_id         uuid        NOT NULL,
    -- What backed the identification, strongest seen so far:
    --   'face' — a face cleared the gate (the plate of persons).
    --   'body' — the face-anchored body chain; provisional by nature.
    -- Only ever upgraded body -> face, never back.
    evidence          text        NOT NULL CHECK (evidence IN ('face', 'body')),
    present_since     timestamptz NOT NULL DEFAULT now(),
    -- The last moment a live verdict actually confirmed them here. The close
    -- rules key off this, and a closed episode ends HERE, not at the moment
    -- the sweep got around to noticing.
    last_confirmed_at timestamptz NOT NULL DEFAULT now(),
    departed_at       timestamptz,
    -- Why the episode closed, so a wrong closure is diagnosable:
    --   'elsewhere' — confirmed on another camera while this one went quiet
    --                 (median camera-to-camera transition: 6 s).
    --   'absence'   — the camera saw no person AT ALL for the guard window
    --                 (a present person is never invisible longer than 4 min
    --                 straight — measured over 7 days of churn gaps).
    --   'stale'     — nothing confirmed for a day; a dead camera must not
    --                 hold people present forever, but loudly, not silently.
    closed_by         text        CHECK (closed_by IN ('elsewhere', 'absence', 'stale')),
    CHECK ((departed_at IS NULL) = (closed_by IS NULL))
);

-- One open episode per identity per camera; a person genuinely visible on two
-- overlapping cameras holds one open episode on each, which is the truth.
CREATE UNIQUE INDEX IF NOT EXISTS presence_episodes_open_idx
    ON presence_episodes (global_id, camera_id) WHERE departed_at IS NULL;

-- "Where has this person been" reads by identity and time.
CREATE INDEX IF NOT EXISTS presence_episodes_gid_idx
    ON presence_episodes (global_id, present_since DESC);
