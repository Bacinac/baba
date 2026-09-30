-- 043 — cross-camera started_at index for retention sweeps.
--
-- The recorder's retention pass runs two cross-camera queries that only exist
-- an index on the leading (camera_id, ...) composite can't serve:
--   * age prune:  DELETE ... WHERE started_at < now() - interval   (all cameras)
--   * disk prune: SELECT ... ORDER BY started_at ASC LIMIT 100      (all cameras)
-- (services/recorder/src/baba_recorder/supervisor.py). With only the existing
-- recordings_camera_started_idx (camera_id, started_at DESC), both fall back to
-- a sequential scan + sort of the whole table (~80k live rows at 60s segments,
-- 8 cams, 7-day retention). A plain (started_at) index makes both index scans.

CREATE INDEX IF NOT EXISTS recordings_started_at_idx
    ON recordings (started_at);
