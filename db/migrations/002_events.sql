-- Tracks and events are the durable output of the pipeline. Each tracked
-- object (across one continuous trajectory on a camera) gets one tracks row;
-- significant moments along that trajectory (entered zone, crossed line,
-- match against a known person) become event rows.

-- Tracks: one row per finalized track. Created when the tracker emits a
-- "track ended" signal (track lost for > lost_buffer frames).
CREATE TABLE IF NOT EXISTS tracks (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id       uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    local_track_id  integer      NOT NULL,    -- the ByteTrack id, scoped per camera; rolls over
    class_id        integer      NOT NULL,
    class_name      text         NOT NULL,
    started_at      timestamptz  NOT NULL,
    ended_at        timestamptz  NOT NULL,
    n_observations  integer      NOT NULL,
    -- Best-quality embedding for re-ID. NULL until embedder finalizes track.
    -- DINOv2 ViT-S/14 is 384 dims; ViT-B/14 is 768. Default to 384 here; if we
    -- swap models, alter or add a vector_b column.
    embedding       vector(384),
    -- Best thumbnail path relative to media root.
    thumbnail_path  text,
    -- For cross-camera re-ID. Filled by event-manager after embedding match.
    global_id       uuid
);

CREATE INDEX IF NOT EXISTS tracks_camera_started_idx ON tracks (camera_id, started_at DESC);
CREATE INDEX IF NOT EXISTS tracks_global_id_idx ON tracks (global_id) WHERE global_id IS NOT NULL;
-- HNSW for fast similarity search across all embeddings.
CREATE INDEX IF NOT EXISTS tracks_embedding_idx
    ON tracks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- Events: discrete things that happened. Polymorphic via `kind` + jsonb
-- payload; we don't pretend each event needs its own table at this stage.
-- Examples: kind='zone_enter', payload={zone_id, dwell_ms_so_far},
--           kind='reid_match',  payload={matched_global_id, score}.
CREATE TABLE IF NOT EXISTS events (
    id          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id   uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    track_id    uuid                  REFERENCES tracks(id) ON DELETE SET NULL,
    kind        text         NOT NULL,
    at          timestamptz  NOT NULL DEFAULT now(),
    payload     jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS events_camera_at_idx ON events (camera_id, at DESC);
CREATE INDEX IF NOT EXISTS events_kind_at_idx ON events (kind, at DESC);
CREATE INDEX IF NOT EXISTS events_track_idx ON events (track_id) WHERE track_id IS NOT NULL;

-- Live event notification, same pattern as cameras.
CREATE OR REPLACE FUNCTION events_notify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('events_new', jsonb_build_object(
        'id', NEW.id,
        'camera_id', NEW.camera_id,
        'kind', NEW.kind,
        'at', NEW.at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_notify ON events;
CREATE TRIGGER events_notify
    AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION events_notify();
