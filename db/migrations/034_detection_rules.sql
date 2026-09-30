-- Three-layer detection sensitivity / class-filtering.
--
-- Layer 1 (detector_global_rules):  GLOBAL floor.  Per-class min_confidence
--    and on/off flag.  Applied in the detector service AFTER postprocessing,
--    BEFORE publishing — saves GPU/bandwidth for downstream consumers
--    (tracker, embedder).  A class without a row is treated as DISABLED,
--    which is intentional — the seeded set below is the "out of the box"
--    classes BABA cares about; the operator can enable more in the UI.
--
-- Layer 2 (camera_detection_rules): PER-CAMERA override on top of the
--    global floor.  `min_confidence` and `enabled` can each be NULL,
--    meaning "inherit the global value for this class".  Indoor cameras
--    typically disable vehicles, outdoor cameras tighten the person
--    confidence to avoid mannequin/poster false positives, etc.
--
-- Layer 3 (zones.rules JSONB):      PER-ZONE refinement evaluated by the
--    event-manager when a track enters / dwells / exits.  Shape:
--      { "enabled_classes": {
--           "person": { "min_confidence": 0.4,
--                       "min_area_pct": 0.01,
--                       "min_dwell_ms": 5000,
--                       "cooldown_s": 30 }, ... } }
--    All four fields are optional per class.  When `rules` is `{}` the
--    zone falls back to the camera/global decision (current behaviour).
--    When `enabled_classes` is set, ONLY listed classes can fire events
--    in this zone — useful for "parking" (vehicles only) or "doorway"
--    (people only) without needing to disable the class globally.
--
-- All three layers hot-reload via NOTIFY so the operator can tune
-- thresholds from the web UI without restarting services.

-- ---------------------------------------------------------------- Layer 1

CREATE TABLE IF NOT EXISTS detector_global_rules (
    class_name      text         PRIMARY KEY,
    min_confidence  real         NOT NULL DEFAULT 0.25
                                 CHECK (min_confidence >= 0 AND min_confidence <= 1),
    enabled         boolean      NOT NULL DEFAULT true,
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS detector_global_rules_touch ON detector_global_rules;
CREATE TRIGGER detector_global_rules_touch
    BEFORE UPDATE ON detector_global_rules
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Seed the eight classes that are immediately useful for residential /
-- light-commercial CCTV.  Any class NOT in this table is treated as
-- disabled by the detector — the operator must explicitly add it via
-- the Settings → AI page if they want bird/tv/handbag/etc.  We use
-- ON CONFLICT DO NOTHING so re-running the migration is safe and so
-- operator-tuned thresholds aren't reset on schema upgrades.
INSERT INTO detector_global_rules (class_name, min_confidence, enabled) VALUES
    ('person',     0.25, true),
    ('bicycle',    0.30, true),
    ('car',        0.30, true),
    ('motorcycle', 0.30, true),
    ('bus',        0.30, true),
    ('truck',      0.30, true),
    ('dog',        0.40, true),
    ('cat',        0.40, true)
ON CONFLICT (class_name) DO NOTHING;

-- ---------------------------------------------------------------- Layer 2

CREATE TABLE IF NOT EXISTS camera_detection_rules (
    camera_id       uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    class_name      text         NOT NULL,
    -- Both NULL = inherit global wholesale.  Either set to a concrete
    -- value overrides the corresponding global field for this camera.
    min_confidence  real         CHECK (min_confidence IS NULL
                                        OR (min_confidence >= 0 AND min_confidence <= 1)),
    enabled         boolean,
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, class_name)
);

CREATE INDEX IF NOT EXISTS camera_detection_rules_camera_idx
    ON camera_detection_rules(camera_id);

DROP TRIGGER IF EXISTS camera_detection_rules_touch ON camera_detection_rules;
CREATE TRIGGER camera_detection_rules_touch
    BEFORE UPDATE ON camera_detection_rules
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------- NOTIFY

-- Both global + per-camera rules feed the detector service, so one
-- channel covers both.  Payload is minimal — the loader does a full
-- refresh, same as it does for cameras/zones.  Keep payload tiny to
-- stay well under Postgres's 8 KB notify limit.
CREATE OR REPLACE FUNCTION detection_rules_notify() RETURNS trigger AS $$
DECLARE
    payload jsonb;
    cam_id  uuid;
BEGIN
    -- camera_id only exists on the per-camera table; on the global
    -- table we publish NULL so the listener can tell the layers apart
    -- if it ever wants to.
    IF TG_TABLE_NAME = 'camera_detection_rules' THEN
        cam_id := COALESCE(NEW.camera_id, OLD.camera_id);
    ELSE
        cam_id := NULL;
    END IF;
    payload := jsonb_build_object(
        'op',        TG_OP,
        'scope',     TG_TABLE_NAME,
        'camera_id', cam_id
    );
    PERFORM pg_notify('detection_rules_changed', payload::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS detector_global_rules_notify ON detector_global_rules;
CREATE TRIGGER detector_global_rules_notify
    AFTER INSERT OR UPDATE OR DELETE ON detector_global_rules
    FOR EACH ROW EXECUTE FUNCTION detection_rules_notify();

DROP TRIGGER IF EXISTS camera_detection_rules_notify ON camera_detection_rules;
CREATE TRIGGER camera_detection_rules_notify
    AFTER INSERT OR UPDATE OR DELETE ON camera_detection_rules
    FOR EACH ROW EXECUTE FUNCTION detection_rules_notify();

-- ---------------------------------------------------------------- Layer 3

-- Per-zone rules live on the zones row itself.  JSONB rather than a
-- separate child table because the rule blob is small (≤ ~80 classes
-- × few KB) and there's exactly one rule set per zone — no point in
-- a join.  The existing zones_notify trigger already fires on UPDATE
-- so an edit to `rules` propagates to event-manager for free.
ALTER TABLE zones
    ADD COLUMN IF NOT EXISTS rules jsonb NOT NULL DEFAULT '{}'::jsonb;
