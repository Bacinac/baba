-- Continuous per-camera pipeline telemetry ("flight recorder") + the
-- incidents the watchers open over it.
--
-- camera_telemetry: one row per (camera, source, minute) — detector
-- publishes per-class confidence/size quantiles and rule-drop counters,
-- tracker publishes track lifecycle counters, ingestor publishes adaptive-
-- rate duty cycles. Small (~10k rows/day for 7 cameras), retained 30 days
-- by the TelemetrySink's own sweeper. This is the evidence base for "did
-- the rate go up for a reason?" and for AI setup optimization — instead of
-- reconstructing history from logs after the fact.
CREATE TABLE camera_telemetry (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at timestamptz NOT NULL,
    camera_slug text NOT NULL,
    source text NOT NULL,
    payload jsonb NOT NULL
);
CREATE INDEX camera_telemetry_cam_at ON camera_telemetry (camera_slug, at DESC);
CREATE INDEX camera_telemetry_at ON camera_telemetry (at);

-- Watcher findings. A deterministic watcher opens a row when a pattern
-- trips (rate pinned active with only parked tracks, rule-drop spike, flip
-- storm, track churn); phase-3 analysis fills verdict/suggestion and the
-- operator applies or dismisses.
CREATE TABLE telemetry_incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_slug text NOT NULL,
    kind text NOT NULL,
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    verdict text,
    suggestion jsonb,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'analyzed', 'applied', 'dismissed', 'closed'))
);
CREATE INDEX telemetry_incidents_cam ON telemetry_incidents (camera_slug, opened_at DESC);
CREATE INDEX telemetry_incidents_open ON telemetry_incidents (status) WHERE status = 'open';
