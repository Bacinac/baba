-- Per-class heatmap breakdown.
--
-- Until now `heatmap_daily` rolled every observation into one cell
-- grid per camera/day. That makes "show me where people walked" vs
-- "show me where vehicles parked" impossible to separate after the
-- fact. We widen the PK with a coarse class_group:
--
--   'person'  → COCO 0
--   'vehicle' → COCO 2, 3, 5, 7 (car, motorcycle, bus, truck)
--   'animal'  → COCO 14..23 (bird, cat, dog, horse, sheep, cow, ...)
--   'other'   → everything else
--   'all'     → reserved for backwards-compatible queries (sum view);
--              the accumulator never writes 'all' rows directly,
--              the api endpoint sums on demand instead.
--
-- 4 group rows per camera per day = 4x more rows but absolute numbers
-- stay tiny (8 cams × 4 groups × 365 days ≈ 12k rows/year). Existing
-- rows get backfilled into 'all' so the migration doesn't lose data.

ALTER TABLE heatmap_daily
    ADD COLUMN IF NOT EXISTS class_group text NOT NULL DEFAULT 'all'
        CHECK (class_group IN ('all', 'person', 'vehicle', 'animal', 'other'));

-- Replace the PK with the wider tuple. Drop + add via the constraint
-- name Postgres assigned (`heatmap_daily_pkey`); IF EXISTS makes the
-- script re-runnable on a partially-applied install.
ALTER TABLE heatmap_daily DROP CONSTRAINT IF EXISTS heatmap_daily_pkey;
ALTER TABLE heatmap_daily
    ADD CONSTRAINT heatmap_daily_pkey PRIMARY KEY (camera_id, day, class_group);

-- Old (camera, day) index covered the dashboard query. The new
-- prefix (camera, day) is the first two columns of the new PK so
-- range scans still hit it; drop the explicit index to avoid the
-- duplicate cost.
DROP INDEX IF EXISTS heatmap_daily_camera_day_idx;
