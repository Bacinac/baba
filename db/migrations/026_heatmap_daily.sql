-- Per-camera, per-day movement heatmap.
--
-- Event-manager accumulates a small in-memory grid (32×18 cells over
-- normalized [0,1] coords) keyed by camera. Every flush_interval_s it
-- UPSERTs the day's row, summing into the existing cells. Grid size is
-- fixed at 32×18 (16:9 cell aspect to mirror typical camera framing)
-- so the int[] length is predictable and the renderer can assume it.
--
-- Row volume: 1 row per camera per day. 8 cams × 365 days = 2920 rows
-- per year — negligible. Cells are stored as int[] (576 ints per row,
-- ~2-3 KB uncompressed); reading a week back is a single index scan.
--
-- The accumulator only counts bottom-centre points (foot position),
-- same anchor zone events use, so the heatmap matches what an operator
-- sees on a top-down camera view.

CREATE TABLE IF NOT EXISTS heatmap_daily (
    camera_id   uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    day         date         NOT NULL,
    grid_w      smallint     NOT NULL,
    grid_h      smallint     NOT NULL,
    -- Length = grid_w * grid_h. Indexed row-major (y * grid_w + x).
    -- int[] not bytea so SUM/aggregations over a date range are
    -- idiomatic SQL; the few extra bytes per cell over a packed int16
    -- representation are immaterial at v1 scale.
    cells       int[]        NOT NULL,
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, day)
);

-- The dashboard query is "heatmap for camera X over last N days":
-- (camera_id, day desc) is exactly that index. Postgres uses the PK
-- already, but spelling it out as DESC saves a backward scan.
CREATE INDEX IF NOT EXISTS heatmap_daily_camera_day_idx
    ON heatmap_daily (camera_id, day DESC);
