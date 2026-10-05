ALTER TABLE face_recompute_jobs
    ADD COLUMN detector_key text,
    ADD COLUMN settings_revision bigint;

CREATE UNIQUE INDEX face_recompute_one_pending
    ON face_recompute_jobs ((true))
    WHERE status = 'pending';
