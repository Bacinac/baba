-- Per-camera detection class remap.
--
-- The nano detector systematically mislabels certain scene objects on
-- specific cameras. Live incident (west, 4096×1152 ultrawide): the large
-- dark glossy car parked under the carport reads as `suitcase` on BOTH the
-- full-frame AND the magnified tile pass — an uniform low-texture mass the
-- nano DETR has no vehicle prior for at that scale. `suitcase` is a
-- disabled class, so the WHOLE vehicle is dropped and only a jittery
-- high-contrast corner survives as `car` (≈21% box, ~0.5 conf) — too weak
-- for the tracker to hold, killing the parked car's track + re-ID crop.
--
-- A per-camera remap rewrites the offending class to the intended one
-- INSIDE the detector, right after decode and BEFORE dedup + the rules
-- filter, so the corrected detection dedups, tracks, embeds and re-IDs
-- like any other. Scoped per camera because these misfires are
-- scene-specific — a `suitcase` on an outdoor driveway is never real, but
-- an indoor camera might legitimately see one. Hot-reloads on the
-- detector's existing `detection_rules_changed` channel.

CREATE TABLE IF NOT EXISTS camera_class_remap (
    camera_id   uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    from_class  text         NOT NULL,
    to_class    text         NOT NULL,
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, from_class)
);

CREATE INDEX IF NOT EXISTS camera_class_remap_camera_idx
    ON camera_class_remap(camera_id);

DROP TRIGGER IF EXISTS camera_class_remap_touch ON camera_class_remap;
CREATE TRIGGER camera_class_remap_touch
    BEFORE UPDATE ON camera_class_remap
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Reuse the detector rules channel — the loader does a full refresh on any
-- notify, so remap edits propagate alongside global + per-camera rules
-- without a detector restart. detection_rules_notify() publishes
-- camera_id=NULL for tables other than camera_detection_rules, which is
-- fine (the loader refreshes everything regardless).
DROP TRIGGER IF EXISTS camera_class_remap_notify ON camera_class_remap;
CREATE TRIGGER camera_class_remap_notify
    AFTER INSERT OR UPDATE OR DELETE ON camera_class_remap
    FOR EACH ROW EXECUTE FUNCTION detection_rules_notify();

-- Seed the known west incident by SLUG (UUIDs differ per deployment; slug
-- is stable). No-op on installs where 'west' doesn't exist yet.
INSERT INTO camera_class_remap (camera_id, from_class, to_class)
SELECT id, 'suitcase', 'car' FROM cameras WHERE slug = 'west'
ON CONFLICT (camera_id, from_class) DO NOTHING;
