-- Per-user preferences blob. Schemaless-ish jsonb so we can iterate on what
-- ends up here without a migration each time. Known top-level keys today:
--   default_landing   text   default URL to redirect to after login
--   time_format_24h   bool   true → 24h, false → 12h
--   timezone          text   IANA timezone, e.g. "Europe/Zagreb"
-- The frontend whitelists which keys it sets; missing keys fall back to
-- browser defaults at format time.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS preferences jsonb NOT NULL DEFAULT '{}'::jsonb;
