-- Per-lighting-band setting profiles ("setpoint schedule") + settings
-- change journal.
--
-- Detection thresholds tuned for bright daylight are wrong at dusk, in
-- heavy overcast, and under night IR. The correlation table below maps
-- (camera, illumination band) → validated settings; the event-manager
-- re-applies the stored profile when the measured light jumps to another
-- band. The table is filled INCIDENT-DRIVEN, as conditions actually
-- occur: watcher incident → AI verdict → apply → the applied settings
-- are stamped under the camera's current band (manual tuning in a band
-- updates it too). No pre-population — a band nobody has tuned yet
-- simply keeps whatever settings are live, and the next incident under
-- those conditions teaches it.
--
-- Bands: ir (monochrome IR mode — its own regime regardless of level),
-- then by measured luma: dark (<40), dim (40-90), normal (90-150),
-- bright (>150). Boundaries live in baba_core.light_profiles; profiles
-- are keyed by band NAME so the cut points can be re-tuned later.
CREATE TABLE camera_setting_profiles (
    camera_id uuid NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    condition text NOT NULL CHECK (condition IN ('ir', 'dark', 'dim', 'normal', 'bright')),
    -- Complete snapshot: {"stillness_ratio": 0.15,
    --   "rules": {"person": {"min_confidence": 0.4, "enabled": true,
    --             "min_box_pct": 0}, ...}}
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, condition)
);

-- Current measured illumination band (event-manager writes it from the
-- ingestor's frame-measured luma/IR telemetry). Read by the api when
-- stamping tuning actions into the right profile, and by the UI badge.
ALTER TABLE cameras ADD COLUMN light_condition text NOT NULL DEFAULT 'normal'
    CHECK (light_condition IN ('ir', 'dark', 'dim', 'normal', 'bright'));

-- Journal of every settings mutation: who changed what, under which
-- conditions, with full before/after snapshots — the operator's one-click
-- revert reads `before`, and reverts are journaled too.
CREATE TABLE camera_setting_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id uuid NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('incident_apply', 'manual', 'profile_switch', 'revert')),
    incident_id uuid,
    light_condition text NOT NULL,
    before jsonb NOT NULL,
    after jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX camera_setting_changes_cam ON camera_setting_changes (camera_id, created_at DESC);
CREATE INDEX camera_setting_changes_incident ON camera_setting_changes (incident_id)
    WHERE incident_id IS NOT NULL;

-- One-click revert of an applied incident gets its own terminal status.
ALTER TABLE telemetry_incidents DROP CONSTRAINT telemetry_incidents_status_check;
ALTER TABLE telemetry_incidents ADD CONSTRAINT telemetry_incidents_status_check
    CHECK (status IN ('open', 'analyzed', 'applied', 'dismissed', 'closed', 'reverted'));
