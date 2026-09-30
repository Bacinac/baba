-- AI assistant + zones.
--
-- ai_settings:
--   One row per provider ("anthropic", "openai", …). v1 ships with the
--   Anthropic row only — the UI asks the user to paste an API key and pick a
--   model. The key is encrypted at rest with Fernet using a derivation of
--   BABA_SECRET_KEY (see baba_api.crypto). The encrypted value is bytea so
--   we never accidentally log it as text.
--
-- zones:
--   Per-camera polygons in *normalized* image coordinates (0..1 on each axis),
--   so the same zone survives camera resolution changes. Polygon is a JSONB
--   array of [x, y] pairs; we don't use PostGIS yet because rules will run
--   in-process in the event-manager (shapely on detection centroids), not in
--   SQL. `kind` is a free-form tag the rules engine will read later
--   ("entry", "restricted", "parking", …) — kept as text so we can add new
--   kinds without a migration.
--
--   We do NOT cascade-delete from cameras yet: zones are user-authored and
--   accidentally re-creating a camera (same slug, different id) shouldn't wipe
--   them. Operator can delete zones explicitly. On a real camera deletion the
--   FK with ON DELETE CASCADE handles cleanup.

CREATE TABLE IF NOT EXISTS ai_settings (
    provider          text         PRIMARY KEY,
    model             text         NOT NULL,
    api_key_encrypted bytea        NOT NULL,
    enabled           boolean      NOT NULL DEFAULT true,
    last_used_at      timestamptz,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS ai_settings_touch ON ai_settings;
CREATE TRIGGER ai_settings_touch
    BEFORE UPDATE ON ai_settings
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


CREATE TABLE IF NOT EXISTS zones (
    id          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id   uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name        text         NOT NULL,
    kind        text         NOT NULL DEFAULT 'generic',
    -- [[x,y], …] with x,y in [0,1]. Validated in the API layer; the DB
    -- only stores the array.
    polygon     jsonb        NOT NULL,
    color       text         NOT NULL DEFAULT '#f59e0b',
    enabled     boolean      NOT NULL DEFAULT true,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS zones_camera_idx ON zones(camera_id);

DROP TRIGGER IF EXISTS zones_touch ON zones;
CREATE TRIGGER zones_touch
    BEFORE UPDATE ON zones
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
