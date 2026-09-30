-- pg_notify trigger on zones so event-manager picks up zone edits
-- (add / move / delete) without a restart. Mirrors the cameras_changed
-- and identities_changed patterns already in the schema.
--
-- Payload: minimal (op + camera_id + zone_id), enough for the resolver
-- in event-manager to know whether to do a per-camera refresh or a
-- full rebuild. Keeping it small avoids hitting the 8 KB Postgres
-- notify payload limit even when many zones are edited at once.

CREATE OR REPLACE FUNCTION zones_notify() RETURNS trigger AS $$
DECLARE
    payload jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        payload := jsonb_build_object(
            'op', 'delete',
            'camera_id', OLD.camera_id,
            'zone_id', OLD.id
        );
    ELSIF TG_OP = 'UPDATE' THEN
        payload := jsonb_build_object(
            'op', 'update',
            'camera_id', NEW.camera_id,
            'zone_id', NEW.id
        );
    ELSE
        payload := jsonb_build_object(
            'op', 'insert',
            'camera_id', NEW.camera_id,
            'zone_id', NEW.id
        );
    END IF;
    PERFORM pg_notify('zones_changed', payload::text);
    -- DELETE returns OLD, others return NEW.
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS zones_notify ON zones;
CREATE TRIGGER zones_notify
    AFTER INSERT OR UPDATE OR DELETE ON zones
    FOR EACH ROW EXECUTE FUNCTION zones_notify();
