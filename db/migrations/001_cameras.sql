-- Cameras are the primary domain entity. Everything (detections, events,
-- recordings) is scoped to a camera. The ingestor supervisor reads this table
-- to know what RTSP sources to subscribe to; CRUD on this table is the
-- public API for "add/remove a camera."

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS cameras (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Human-facing
    name                 text        NOT NULL,
    slug                 text        NOT NULL UNIQUE,

    -- Source. rtsp_url is stored as plain text; treat as secret.
    -- go2rtc handles the mux/restream, so this is the upstream camera URL.
    rtsp_url             text        NOT NULL,

    -- Pipeline knobs
    enabled              boolean     NOT NULL DEFAULT true,
    target_fps           integer     NOT NULL DEFAULT 5  CHECK (target_fps BETWEEN 1 AND 30),
    downscale_max_edge   integer     NOT NULL DEFAULT 1280 CHECK (downscale_max_edge >= 320),
    hw_decode            text,                 -- e.g. 'h264_cuvid', 'h264_qsv'; null = software

    -- Bookkeeping
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cameras_enabled_idx ON cameras (enabled) WHERE enabled;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS cameras_touch ON cameras;
CREATE TRIGGER cameras_touch
    BEFORE UPDATE ON cameras
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- NOTIFY channel used by the ingestor supervisor to react to changes without
-- polling. Inserts/updates/deletes emit a small JSON payload.
CREATE OR REPLACE FUNCTION cameras_notify() RETURNS trigger AS $$
DECLARE
    payload jsonb;
BEGIN
    payload := jsonb_build_object(
        'op', TG_OP,
        'id', COALESCE(NEW.id, OLD.id),
        'slug', COALESCE(NEW.slug, OLD.slug)
    );
    PERFORM pg_notify('cameras_changed', payload::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS cameras_notify ON cameras;
CREATE TRIGGER cameras_notify
    AFTER INSERT OR UPDATE OR DELETE ON cameras
    FOR EACH ROW EXECUTE FUNCTION cameras_notify();
