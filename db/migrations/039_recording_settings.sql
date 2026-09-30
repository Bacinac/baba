-- Global recording policy. Until now retention was age-only and per-camera
-- (cameras.retention_days), with NO disk-aware cap — so a busy fleet filled
-- the media volume to 100% (295G of segments on a 300G NFS export) and the
-- recorder thrashed on ENOSPC. This singleton holds one global policy the
-- operator configures in Settings → Recording.
--
--   mode = 'continuous' : record 24/7, bounded only by retention + disk cap.
--   mode = 'activity'   : keep the last `activity_buffer_minutes` of every
--                         camera continuously (for pre-roll / live scrub), but
--                         beyond that keep ONLY segments that overlap a detected
--                         track. Most of the day is empty → huge disk saving.
--
-- The disk water-marks are the safety net that GUARANTEES the volume never
-- fills again: when usage crosses `disk_high_water_pct`, the recorder deletes
-- the oldest segments (any camera) down to `disk_low_water_pct`.

CREATE TABLE IF NOT EXISTS recording_settings (
    id                      smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode                    text        NOT NULL DEFAULT 'continuous'
                                        CHECK (mode IN ('continuous', 'activity')),
    -- Hard age cap (days) for ALL cameras. Supersedes cameras.retention_days.
    retention_days          int         NOT NULL DEFAULT 7   CHECK (retention_days BETWEEN 1 AND 365),
    -- Disk safety net (% of the media volume).
    disk_high_water_pct     int         NOT NULL DEFAULT 85  CHECK (disk_high_water_pct BETWEEN 50 AND 99),
    disk_low_water_pct      int         NOT NULL DEFAULT 75  CHECK (disk_low_water_pct BETWEEN 40 AND 98),
    -- Activity mode: minutes of always-kept continuous buffer before the
    -- "keep only segments with activity" rule kicks in.
    activity_buffer_minutes int         NOT NULL DEFAULT 60  CHECK (activity_buffer_minutes BETWEEN 5 AND 1440),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (disk_low_water_pct < disk_high_water_pct)
);

INSERT INTO recording_settings (id) VALUES (1) ON CONFLICT DO NOTHING;
