-- Hot-path indexes + audit-integrity fix (from the 2026-07-11 review).
--
-- On the live .200 these indexes were built CONCURRENTLY out-of-band first
-- (a plain CREATE INDEX on track_embedding_samples — 31 GB — would take an
-- exclusive lock and stall the embedder + api boot). The IF NOT EXISTS makes
-- this migration a no-op there and a normal (instant, empty-table) build on a
-- fresh install. Keep the index NAMES identical to the manual builds.

-- events: the default /events page (ORDER BY at DESC LIMIT with a kind<>… filter),
-- every analytics endpoint, and the hourly retention DELETE all filter/sort on
-- bare `at`. (kind, at) can't serve a leading `at`, so they seq-scanned the
-- whole table. A single (at DESC) index serves all of them.
CREATE INDEX IF NOT EXISTS events_at_idx ON events (at DESC);

-- track_embedding_samples: the recorder's active-camera probe runs
-- `WHERE captured_at > now()-6min` every 300 s (288×/day) and the hourly age
-- purge runs `WHERE captured_at < now()-30d` — both seq-scanned the 31 GB table.
CREATE INDEX IF NOT EXISTS track_embedding_samples_captured_at_idx
    ON track_embedding_samples (captured_at);

-- tracks: /sightings without a camera filter sorts by started_at; the re-ID
-- window queries (finalize face-kNN, body-cluster, resweep) all filter on
-- ended_at. Neither was indexed on its own.
CREATE INDEX IF NOT EXISTS tracks_started_at_idx ON tracks (started_at DESC);
CREATE INDEX IF NOT EXISTS tracks_ended_at_idx ON tracks (ended_at DESC);

-- notification_deliveries.event_id is an FK with ON DELETE SET NULL but had no
-- index, so every DELETE FROM events (hourly sweep, tombstones, static
-- suppression, identity delete) triggered a per-row referential seq-scan here.
CREATE INDEX IF NOT EXISTS notification_deliveries_event_id_idx
    ON notification_deliveries (event_id) WHERE event_id IS NOT NULL;

-- admin_audit CHECK constraints were pinned to the original four resource_types
-- and three ops and never widened. Every newer audit write (scene_region,
-- notification_*, detection_rule_*, face_recognition; ops rename_slug/upsert/
-- capture/prototype_delete/purge) raised CheckViolationError, which write_audit
-- swallows — so most of the audit trail was silently dropped. These columns are
-- only ever written by write_audit with app-controlled values, so drop the
-- CHECKs entirely rather than re-pinning a list that will rot again.
ALTER TABLE admin_audit DROP CONSTRAINT IF EXISTS admin_audit_resource_type_check;
ALTER TABLE admin_audit DROP CONSTRAINT IF EXISTS admin_audit_op_check;

-- events takes hourly bulk deletes (retention sweep) but kept the default 20%
-- autovacuum trigger, so it bloated between passes. Tighten it like migration
-- 045 did for track_embedding_samples.
ALTER TABLE events SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.05
);
