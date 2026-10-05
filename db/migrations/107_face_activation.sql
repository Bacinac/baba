ALTER TABLE face_recognition_settings
    ADD COLUMN revision bigint NOT NULL DEFAULT 1,
    ADD COLUMN active_revision bigint NOT NULL DEFAULT 0,
    ADD COLUMN active_model_key text,
    ADD COLUMN active_detector_key text,
    ADD COLUMN active_match_threshold real,
    ADD COLUMN api_revision bigint,
    ADD COLUMN embedder_revision bigint,
    ADD COLUMN api_error text,
    ADD COLUMN embedder_error text;

UPDATE face_recognition_settings
SET active_model_key = model_key,
    active_detector_key = detector_key,
    active_match_threshold = match_threshold;

ALTER TABLE face_recognition_settings
    ALTER COLUMN active_model_key SET NOT NULL,
    ALTER COLUMN active_detector_key SET NOT NULL,
    ALTER COLUMN active_match_threshold SET NOT NULL,
    ADD CHECK (revision > 0 AND active_revision >= 0 AND active_revision <= revision),
    ADD CHECK (active_match_threshold >= 0 AND active_match_threshold <= 2);

DROP TRIGGER face_recognition_settings_notify ON face_recognition_settings;
CREATE TRIGGER face_recognition_settings_notify
    AFTER INSERT OR UPDATE OF model_key, detector_key, match_threshold, active_revision
    ON face_recognition_settings
    FOR EACH ROW EXECUTE FUNCTION notify_face_recognition_changed();
