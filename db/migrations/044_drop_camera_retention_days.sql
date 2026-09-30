-- Retention is global (recording_settings.retention_days), not per-camera.
-- The per-camera cameras.retention_days column was a live UI control that the
-- recorder silently ignored — every camera was pruned at the global cap — so
-- setting it created a false expectation (data kept/deleted differently than
-- the UI promised). The Storage settings page already states retention is
-- global; this drops the vestigial column so there is a single source of truth.
ALTER TABLE cameras DROP COLUMN IF EXISTS retention_days;
