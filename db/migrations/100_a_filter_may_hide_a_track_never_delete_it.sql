-- A filter may hide a track. It may not delete one.
--
-- `min_dwell_ms` and the static-object threshold exist to keep Activity
-- readable: a parked car flickering in and out of its own box, a pedestrian
-- clipping the corner of a zone for a second. Both were written as an early
-- `return` in finalize, so neither hid those tracks — they discarded them,
-- before the row was ever written.
--
-- Since 29.07 that row is not a timeline entry but the fact the place
-- registry, the plate sweep and re-ID all read, and a filter that deletes it
-- has to be exempted by hand for every kind of subject that goes missing from
-- the record: a person who stops moving (4b64856), a car that comes to rest
-- (07ecab0), and a man carrying a parcel four seconds through the gate zone —
-- under the five-second dwell, so nothing was written at all and the only
-- surviving evidence was the continuous recording.
--
-- The table now keeps every track the pipeline followed and the verdict is a
-- column on the row. `tracks` becomes the view its readers already assume —
-- unsuppressed only — so every existing query keeps today's meaning, and
-- whoever wants the whole record asks `tracks_all` for it.
--
-- A view expands `SELECT *` once, at creation: a later migration that adds a
-- column to `tracks_all` must CREATE OR REPLACE this view or the column is
-- invisible through it.
ALTER TABLE tracks RENAME TO tracks_all;

ALTER TABLE tracks_all ADD COLUMN suppressed_reason text;
ALTER TABLE tracks_all ADD CONSTRAINT tracks_all_suppressed_reason_check
    CHECK (suppressed_reason IS NULL OR suppressed_reason IN ('off-zone', 'sub-dwell', 'static'));

-- Suppressed rows are read two ways only: swept by retention, and listed when
-- the operator asks what a camera saw and did not show.
CREATE INDEX tracks_all_suppressed_idx ON tracks_all (camera_id, started_at DESC)
    WHERE suppressed_reason IS NOT NULL;

CREATE VIEW tracks AS SELECT * FROM tracks_all WHERE suppressed_reason IS NULL;
