-- Generic key/value application settings (jsonb). First consumer:
-- telemetry_auto_analyze — the UI switch that lets the api's auto-analyzer
-- run AI verdicts on incidents as the watchers open them, instead of
-- waiting for a manual click. Deliberately a kv table: one-off operator
-- toggles don't each deserve a migration.
CREATE TABLE app_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
