-- Face detector: extend face_recognition_settings with detector_key so
-- the UI can swap YuNet for SCRFD (or other future BYOM detectors) the
-- same way it already swaps embedders (AuraFace/TopoFR).
--
-- The detector half of the pipeline produces the bbox + 5 landmarks
-- consumed by the embedder. Both halves can be swapped independently:
-- a stronger detector (SCRFD) finds more faces during head-turned /
-- partially-occluded moments while the embedder choice (AuraFace vs
-- TopoFR vs ...) decides per-face similarity quality.
--
-- detector_key references baba_core.face_detectors.FACE_DETECTORS.
-- API validates before write; SQL leaves it untyped so new detectors
-- can be added to the registry without a migration.

ALTER TABLE face_recognition_settings
    ADD COLUMN IF NOT EXISTS detector_key text NOT NULL DEFAULT 'yunet';

-- Reuse the existing face_recognition_changed NOTIFY trigger — the
-- trigger fires on ANY UPDATE of the singleton, so worker services
-- (embedder, event-manager) hot-reload when detector_key changes too.
-- No new trigger needed.
