-- How far a purge reaches, alongside the flag that one is pending.
--
-- The existing purge erases the video and nothing else: the segment files, the
-- cached clips, and the `recordings` index over them. That is a real thing to
-- want — reclaim the disk, keep the log of what happened — and it stays.
--
-- What it cannot do is start the record over. Everything BABA observed lives in
-- other tables, and after a video-only purge the Activity feed still lists
-- tracks whose footage is gone. Wanting the whole record gone is a different
-- request, not a bigger one, so the operation now carries its reach instead of
-- a second flag meaning almost the same thing.
--
-- NULL means no purge is pending, exactly as `purge_requested_at` NULL does;
-- the two are set and cleared together.
ALTER TABLE recording_settings ADD COLUMN IF NOT EXISTS purge_scope text;

ALTER TABLE recording_settings DROP CONSTRAINT IF EXISTS recording_settings_purge_scope_check;
ALTER TABLE recording_settings ADD CONSTRAINT recording_settings_purge_scope_check
    CHECK (purge_scope IS NULL OR purge_scope IN ('recordings', 'record'));

-- A purge requested before this migration is a video purge, which is all the
-- endpoint could ask for.
UPDATE recording_settings SET purge_scope = 'recordings'
 WHERE purge_requested_at IS NOT NULL AND purge_scope IS NULL;

COMMENT ON COLUMN recording_settings.purge_scope IS
    'How far the pending purge reaches: ''recordings'' for the video and its '
    'index, ''record'' for everything BABA has observed. NULL when none is '
    'pending, set and cleared with purge_requested_at.';
