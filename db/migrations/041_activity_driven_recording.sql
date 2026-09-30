-- Activity-driven recording retention.
--
-- The 'activity' mode previously kept the last `activity_buffer_minutes`
-- of footage continuously (a recency buffer) and only pruned older quiet
-- segments. That left up to an hour of recording with no activity. Repurpose
-- the column as a pre/post-ROLL: how many minutes of context to keep around
-- each activity. The recorder now prunes any ended segment that isn't within
-- this roll of a finalized track (and whose camera isn't currently active),
-- so retained footage tracks activity instead of wall-clock recency. Allow
-- small values (down to 0 = tightest) and reset the singleton to a tight
-- default since the semantics changed.

ALTER TABLE recording_settings
  DROP CONSTRAINT IF EXISTS recording_settings_activity_buffer_minutes_check;

ALTER TABLE recording_settings
  ADD CONSTRAINT recording_settings_activity_buffer_minutes_check
  CHECK (activity_buffer_minutes >= 0 AND activity_buffer_minutes <= 60);

ALTER TABLE recording_settings
  ALTER COLUMN activity_buffer_minutes SET DEFAULT 1;

UPDATE recording_settings SET activity_buffer_minutes = 1 WHERE id = 1;

COMMENT ON COLUMN recording_settings.activity_buffer_minutes IS
  'Activity mode: minutes of context (pre/post-roll) kept around each '
  'activity. Ended segments not within this roll of a finalized track '
  '(and whose camera is not currently active) are pruned. 0 = tightest.';
