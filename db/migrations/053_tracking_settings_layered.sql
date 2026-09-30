-- Layered tracking settings: global defaults + per-camera NULL-inherit
-- overrides — the same three-layer model the detection rules use.
--
-- Motivation (2026-07-13, patio presence work): stillness_ratio and
-- park_seconds were NOT NULL per-camera columns, so "tune the fleet" meant
-- editing every camera; and the Norfair track-lifetime windows
-- (lost_seconds / reid_lost_seconds) existed only as env vars, invisible
-- to the UI even though they are scene-dependent (long coast+reid windows
-- keep a seated person's identity on a quiet patio, but raise the
-- wrong-reattach risk on a busy street camera).
--
-- Resolution order everywhere: illumination profile (writes explicit
-- per-camera values, stays the top layer) > per-camera override (non-NULL
-- column) > app_settings 'tracking_defaults' > env bootstrap.

ALTER TABLE cameras ALTER COLUMN stillness_ratio DROP NOT NULL;
ALTER TABLE cameras ALTER COLUMN stillness_ratio DROP DEFAULT;
ALTER TABLE cameras ALTER COLUMN park_seconds DROP NOT NULL;
ALTER TABLE cameras ALTER COLUMN park_seconds DROP DEFAULT;

ALTER TABLE cameras ADD COLUMN IF NOT EXISTS lost_seconds integer;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS reid_lost_seconds integer;
ALTER TABLE cameras ADD CONSTRAINT cameras_lost_seconds_range
    CHECK (lost_seconds IS NULL OR (lost_seconds >= 1 AND lost_seconds <= 300));
ALTER TABLE cameras ADD CONSTRAINT cameras_reid_lost_seconds_range
    CHECK (reid_lost_seconds IS NULL OR (reid_lost_seconds >= 0 AND reid_lost_seconds <= 900));

-- Rows still carrying the historical schema defaults were never deliberate
-- per-camera choices — fold them into the global layer. Values an operator
-- actually tuned (anything else) survive as explicit overrides.
-- NOTE: stillness_ratio is `real` — an exact `= 0.08` comparison silently
-- misses (float4 0.08 is 0.0799999982…), so compare with a tolerance.
UPDATE cameras SET stillness_ratio = NULL WHERE abs(stillness_ratio - 0.08) < 1e-6;
UPDATE cameras SET park_seconds = NULL WHERE park_seconds = 60;

INSERT INTO app_settings (key, value)
VALUES (
    'tracking_defaults',
    '{"stillness_ratio": 0.08, "park_seconds": 60, "lost_seconds": 30, "reid_lost_seconds": 120}'::jsonb
)
ON CONFLICT (key) DO NOTHING;
