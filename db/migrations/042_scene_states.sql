-- Scene-state evaluation: persistent state of a fixed region in the frame
-- (e.g. "gate open / closed", "garage door up / down", "light on / off").
--
-- This is a NEW pipeline concept, orthogonal to tracks. Tracks are about
-- *transient objects* moving through the scene; a scene region is about the
-- *persistent state of scene structure* in a fixed polygon. There is no
-- detector class for "open gate" — instead we few-shot it: the operator draws
-- a region, captures a handful of reference frames per state, and the
-- state-evaluator service embeds the region crop with DINOv2 (general-purpose
-- visual features — NOT OSNet, which is tuned for person identity and is the
-- wrong tool for scene structure) and nearest-neighbours it against the
-- prototypes on a slow cadence (every few seconds, not per-frame).
--
-- Three tables, deliberately separated by *ownership* to keep a single source
-- of truth for each kind of data and to avoid a config<->runtime feedback loop:
--
--   scene_regions          — CONFIG. Operator-authored via the API: geometry,
--                            state labels, sampling + hysteresis knobs. A
--                            `regions_changed` NOTIFY fires on every change so
--                            the evaluator hot-reloads (mirrors zones_changed).
--
--   scene_region_prototypes — REFERENCE EVIDENCE. Few-shot examples captured by
--                            the operator. The crop image is stored immediately;
--                            the embedding is filled in asynchronously by the
--                            evaluator (which owns the inference backend), the
--                            same "store now, embed over LISTEN" pattern used by
--                            face_recompute_jobs. embedding_model tags the
--                            backbone so a future backbone swap can refuse to
--                            match across embedding spaces.
--
--   scene_region_status    — RUNTIME. Written ONLY by the evaluator: the
--                            currently-committed state + when it last changed.
--                            Kept in its own table precisely so the evaluator's
--                            frequent status writes do NOT trip the
--                            regions_changed config NOTIFY and reload itself.
--                            Persists across restart so we don't re-emit a
--                            transition for a state that was already current.
--
-- Transitions become ordinary rows in `events` (kind='scene_state_change'),
-- which already fires NOTIFY 'events_new' on insert — so scene-state changes
-- flow through the exact same path as zone/track events to the API WebSocket,
-- the Activity feed, and (via the events bridge) the DIDA automation bus. No
-- new transport.


-- ── CONFIG ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scene_regions (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id           uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name                text         NOT NULL,
    -- [[x,y], …] with x,y in [0,1], same normalized convention as zones so the
    -- region survives camera resolution changes. Validated in the API layer.
    polygon             jsonb        NOT NULL,
    -- Ordered list of state labels, e.g. {'open','closed'}. The UI orders by
    -- this; evaluation needs >= 2 labelled states (enforced in the API before
    -- the region is allowed to enable).
    states              text[]       NOT NULL DEFAULT '{}',
    -- Seconds between evaluations. Scene state changes slowly; default 8s keeps
    -- GPU load trivial across all cameras.
    sample_interval_s   integer      NOT NULL DEFAULT 8   CHECK (sample_interval_s >= 1),
    -- Require this many consecutive identical classifications before committing
    -- a transition. Debounces flicker from a momentary occlusion / lighting blip.
    hysteresis_n        integer      NOT NULL DEFAULT 3   CHECK (hysteresis_n >= 1),
    -- Cosine *distance* above which the nearest prototype is too far to trust →
    -- classify as 'unknown' and emit nothing (occluded gate, car parked in
    -- front, never-seen lighting). [0,2] like the rest of the pgvector code.
    unknown_margin      real         NOT NULL DEFAULT 0.40
        CHECK (unknown_margin >= 0.0 AND unknown_margin <= 2.0),
    -- UI overlay colour. Cyan by default to read as distinct from zones (amber).
    color               text         NOT NULL DEFAULT '#22d3ee',
    enabled             boolean      NOT NULL DEFAULT true,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scene_regions_camera_idx ON scene_regions(camera_id);

DROP TRIGGER IF EXISTS scene_regions_touch ON scene_regions;
CREATE TRIGGER scene_regions_touch
    BEFORE UPDATE ON scene_regions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Hot-reload signal for the evaluator. Mirrors zones_notify exactly: minimal
-- payload (op + camera_id + region_id) so a per-camera refresh is enough and we
-- never approach the 8 KB notify limit.
CREATE OR REPLACE FUNCTION scene_regions_notify() RETURNS trigger AS $$
DECLARE
    payload jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        payload := jsonb_build_object('op', 'delete', 'camera_id', OLD.camera_id, 'region_id', OLD.id);
    ELSIF TG_OP = 'UPDATE' THEN
        payload := jsonb_build_object('op', 'update', 'camera_id', NEW.camera_id, 'region_id', NEW.id);
    ELSE
        payload := jsonb_build_object('op', 'insert', 'camera_id', NEW.camera_id, 'region_id', NEW.id);
    END IF;
    PERFORM pg_notify('scene_regions_changed', payload::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS scene_regions_notify ON scene_regions;
CREATE TRIGGER scene_regions_notify
    AFTER INSERT OR UPDATE OR DELETE ON scene_regions
    FOR EACH ROW EXECUTE FUNCTION scene_regions_notify();


-- ── REFERENCE EVIDENCE ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scene_region_prototypes (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    region_id       uuid         NOT NULL REFERENCES scene_regions(id) ON DELETE CASCADE,
    state_label     text         NOT NULL,
    -- DINOv2 ViT-S/14 → 384 dims. NULL until the evaluator fills it in over the
    -- scene_regions_changed / dedicated enroll LISTEN (inference lives there,
    -- not in the API). Several prototypes per state are expected (day / night /
    -- rain) — kNN over them naturally handles the multi-modal distribution.
    embedding       vector(384),
    -- Backbone provenance key. Refuse cross-space matching if the backbone is
    -- ever swapped (same rationale as track face_embedding_model).
    embedding_model text,
    -- Reference crop saved under the media root, for re-embedding + UI display.
    crop_path       text,
    captured_at     timestamptz  NOT NULL DEFAULT now(),
    created_by      uuid         REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS scene_region_prototypes_region_idx
    ON scene_region_prototypes(region_id);
-- Consistency with the rest of the embedding tables; the row count per region
-- is tiny so this is for uniformity, not a real ANN need.
CREATE INDEX IF NOT EXISTS scene_region_prototypes_embedding_idx
    ON scene_region_prototypes USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;


-- ── RUNTIME ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scene_region_status (
    region_id           uuid         PRIMARY KEY REFERENCES scene_regions(id) ON DELETE CASCADE,
    -- Last *committed* state (after hysteresis). NULL = not yet evaluated.
    -- 'unknown' is a real committed value (nearest prototype beyond margin).
    current_state       text,
    current_state_since timestamptz,
    -- Diagnostics for the live "Test" panel: last raw (pre-hysteresis) label and
    -- nearest-prototype distance from the most recent evaluation tick.
    last_eval_at        timestamptz,
    last_label_raw      text,
    last_distance       real
);

COMMENT ON TABLE scene_regions IS
    'CONFIG for scene-state evaluation: per-camera region geometry + state labels + sampling/hysteresis knobs. Operator-authored; scene_regions_changed NOTIFY drives evaluator hot-reload.';
COMMENT ON TABLE scene_region_prototypes IS
    'Few-shot reference crops per state. embedding (DINOv2 384-d) filled asynchronously by the evaluator; embedding_model tags the backbone to prevent cross-space matching.';
COMMENT ON TABLE scene_region_status IS
    'RUNTIME state written only by the evaluator. Separate table so frequent status writes do not trip the scene_regions_changed config NOTIFY. Persists current state across restarts to avoid duplicate transition events.';
