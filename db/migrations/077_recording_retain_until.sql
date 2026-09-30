-- Person footage outlives the rotation: class-aware retention for segments.
--
-- Retention treated all footage as equal — AGE cap, activity prune and the
-- disk high-water sweep each deleted flatly by time, so the segment behind a
-- person event died as fast as an hour of empty driveway. The event row kept
-- its tiered deadline (037: unnamed 30 d, named 60 d, enrolled 90 d) while
-- the video it points at was already gone; the archive then answered with an
-- empty-but-valid MP4. Found the honest way, on cabin: a person event from
-- yesterday whose clip came back 262 bytes.
--
-- `retain_until` on a segment is a PIN, not a schedule: NULL means "rotate
-- normally" (the overwhelmingly common case — parking, motion, empty frames),
-- a timestamp means "a person track overlaps this footage; keep it at least
-- as long as that track row lives". The recorder's sweep keeps it aligned
-- with the overlapping tracks' own `retain_until`, so enrolling an identity
-- (which bumps its tracks' tier) automatically extends the footage too, and
-- every prune (age / activity / disk) refuses rows whose pin is in the
-- future. The footage and the event that references it now share one
-- lifetime, from one source.

ALTER TABLE recordings
  ADD COLUMN IF NOT EXISTS retain_until TIMESTAMPTZ;

-- Prunes filter on `retain_until < now()`; pinned rows are a small minority
-- so the partial index stays tiny.
CREATE INDEX IF NOT EXISTS recordings_retain_until_idx
    ON recordings(retain_until)
    WHERE retain_until IS NOT NULL;

-- The pin pass needs "segments of this camera overlapping this time range"
-- to be an index range-scan, not a table scan, once per person track.
CREATE INDEX IF NOT EXISTS recordings_camera_started_idx
    ON recordings(camera_id, started_at);

-- Backfill: pin what can still be saved. Same rule the recorder sweep will
-- keep applying (person class, really-moved tracks, live deadline); the
-- 1-minute pad matches the activity_buffer_minutes default — the sweep
-- re-pins with the configured value within one cycle.
WITH keep AS (
    SELECT r.id, max(t.retain_until) AS keep_until
      FROM tracks t
      JOIN recordings r
        ON r.camera_id = t.camera_id
       AND r.started_at < t.ended_at + interval '1 minute'
       AND COALESCE(r.ended_at, 'infinity'::timestamptz)
             > t.started_at - interval '1 minute'
     WHERE t.class_id = 0
       AND t.retain_until > now()
     GROUP BY r.id
)
UPDATE recordings r
   SET retain_until = k.keep_until
  FROM keep k
 WHERE r.id = k.id
   AND (r.retain_until IS NULL OR r.retain_until < k.keep_until);
