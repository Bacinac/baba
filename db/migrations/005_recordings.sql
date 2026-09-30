-- Continuous recordings + per-camera retention.
--
-- Recordings are split into fixed-length segments (default 60s) by ffmpeg's
-- segment muxer. Each closed segment gets a row here so the UI can scan a
-- time range fast: "what segments overlap with this event?" is a single
-- indexed lookup. Retention deletes oldest segments first.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS recording_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS retention_days    int     NOT NULL DEFAULT 7
        CHECK (retention_days BETWEEN 1 AND 3650);

CREATE TABLE IF NOT EXISTS recordings (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id    uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    started_at   timestamptz  NOT NULL,
    -- NULL while ffmpeg is still writing this segment. Set when the next
    -- segment appears (or on shutdown). Allows "is this segment open?" queries.
    ended_at     timestamptz,
    duration_s   double precision,
    -- Path relative to BABA_MEDIA_PATH so reorganisations don't break refs.
    path         text         NOT NULL UNIQUE,
    size_bytes   bigint,
    -- Source media metadata captured from the stream when possible.
    codec        text,
    width        int,
    height       int,
    created_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recordings_camera_started_idx
    ON recordings (camera_id, started_at DESC);

-- "Find the recording covering this event's timestamp" — used by the events
-- API to return a play-ready link with seek offset. The time-range condition
-- is `started_at <= at AND (ended_at IS NULL OR at < ended_at)`; index on
-- (camera_id, started_at) plus the dataset shape (sequential segments, all
-- non-overlapping) makes this efficient.
