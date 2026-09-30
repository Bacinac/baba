-- Tracks which migration files have been applied. The api service consults
-- this on startup and applies any new files in lexicographic order.
CREATE TABLE IF NOT EXISTS schema_versions (
    version     text         PRIMARY KEY,
    applied_at  timestamptz  NOT NULL DEFAULT now()
);
