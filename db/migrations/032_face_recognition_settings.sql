-- Face recognition: UI-driven model selection + per-row provenance.
--
-- Two changes:
--   1) Singleton settings table (face_recognition_settings, id=1) that
--      stores the currently active embedder model key and its match
--      threshold (cosine distance). Embedder + event-manager read this
--      at startup and refresh on the `face_recognition_changed` NOTIFY.
--      Previously the same state lived in BABA_FACE_RECOGNITION_MODEL
--      and BABA_EVENT_REID_FACE_COSINE_THRESHOLD env vars; the env
--      values are still honoured at first-run when the singleton
--      defaults to 'auraface'.
--
--   2) Per-row `face_embedding_model` columns on identity_labels,
--      identity_reference_photos and track_embedding_samples. AuraFace
--      and TopoFR both produce 512-d L2-normalised vectors but in
--      different embedding spaces — a cosine similarity computed
--      across spaces is noise. By tagging each stored vector with its
--      origin model, the matcher can filter `WHERE face_embedding_model
--      = $active` and refuse to match across spaces. This also gives
--      the UI a precise "needs recompute" hint when the active model
--      differs from the dominant tag of an identity's references.
--
-- The NOTIFY trigger fires on any insert/update of the singleton so
-- worker services can hot-reload without a container restart.

CREATE TABLE IF NOT EXISTS face_recognition_settings (
    -- Singleton: only one row allowed, id pinned to 1 by CHECK.
    id              integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- Registry key from baba_core.face_models. Validated by the API
    -- before write; left untyped here so adding new models to the
    -- registry doesn't require another migration.
    model_key       text    NOT NULL DEFAULT 'auraface',
    -- Cosine *distance* threshold (lower = stricter match). Existing
    -- env default was 0.40; AuraFace works well at 0.40 but TopoFR
    -- has tighter intra-class clusters and 0.55-0.60 is typical.
    -- Calibrated per model by the operator via the Settings UI.
    match_threshold real    NOT NULL DEFAULT 0.40
        CHECK (match_threshold >= 0.0 AND match_threshold <= 2.0),
    updated_at      timestamp with time zone NOT NULL DEFAULT now(),
    updated_by      uuid REFERENCES users(id) ON DELETE SET NULL
);

INSERT INTO face_recognition_settings (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

-- Notify channel so the embedder + event-manager can refresh on change.
-- Mirror the cameras_changed/zones_changed pattern already in use.
CREATE OR REPLACE FUNCTION notify_face_recognition_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'face_recognition_changed',
        json_build_object(
            'model_key', NEW.model_key,
            'match_threshold', NEW.match_threshold,
            'updated_at', NEW.updated_at
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS face_recognition_settings_notify ON face_recognition_settings;
CREATE TRIGGER face_recognition_settings_notify
    AFTER INSERT OR UPDATE ON face_recognition_settings
    FOR EACH ROW EXECUTE FUNCTION notify_face_recognition_changed();


-- Per-row provenance. NULL on legacy rows is treated as "unknown /
-- pre-tagging" by the matcher, which treats it as the current active
-- model for back-compat (no flood of unmatchable rows after upgrade).
-- After the post-deploy recompute pass, all rows get tagged so the
-- NULL path is only legacy data the operator hasn't refreshed yet.
ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS face_embedding_model text;

ALTER TABLE identity_reference_photos
    ADD COLUMN IF NOT EXISTS face_embedding_model text;

ALTER TABLE track_embedding_samples
    ADD COLUMN IF NOT EXISTS face_embedding_model text;

-- Index to support the matcher's WHERE face_embedding_model = $active
-- filter without a sequential scan once the table grows past a few
-- hundred K rows.
CREATE INDEX IF NOT EXISTS track_embedding_samples_face_model_idx
    ON track_embedding_samples (face_embedding_model)
    WHERE face_embedding IS NOT NULL;


-- Async recompute job tracking. The UI's "Recompute references" button
-- starts a background job (API spawns a Python worker); the row is the
-- progress mailbox the UI polls. Only one job runs at a time — a UNIQUE
-- partial index keeps the API honest about not stacking jobs.
CREATE TABLE IF NOT EXISTS face_recompute_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_key       text NOT NULL,
    started_at      timestamp with time zone NOT NULL DEFAULT now(),
    finished_at     timestamp with time zone,
    -- 'running' | 'done' | 'failed' | 'cancelled'
    status          text NOT NULL DEFAULT 'running',
    total           integer NOT NULL DEFAULT 0,
    processed       integer NOT NULL DEFAULT 0,
    succeeded       integer NOT NULL DEFAULT 0,
    no_face         integer NOT NULL DEFAULT 0,
    missing_file    integer NOT NULL DEFAULT 0,
    error_message   text,
    started_by      uuid REFERENCES users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS face_recompute_one_running
    ON face_recompute_jobs ((true))
    WHERE status = 'running';


COMMENT ON TABLE face_recognition_settings IS
    'Singleton (id=1) holding the active face recognition model and its match threshold. Edited via UI; broadcast via face_recognition_changed NOTIFY for hot-reload.';

COMMENT ON COLUMN identity_labels.face_embedding_model IS
    'Registry key (baba_core.face_models) of the embedder model that produced this face_embedding. NULL = legacy / unknown.';

COMMENT ON TABLE face_recompute_jobs IS
    'Background job tracking for bulk re-computation of stored face embeddings after model switch. UNIQUE partial index limits to one running job at a time.';
