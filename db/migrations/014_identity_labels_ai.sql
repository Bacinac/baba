-- Extends identity_labels with AI-assistance + plate fields, and widens
-- identity_audit to log AI describe operations.
--
-- `source` tracks where the row came from so the UI can distinguish
-- "operator-typed" from "AI-suggested, not yet reviewed". An auto-fill
-- worker sets 'ai' on insert; the PUT /label endpoint promotes 'ai' to
-- 'ai_reviewed' on first user edit. Pure-manual edits stay 'manual'.
--
-- `plate` is a dedicated column (instead of just a tag) because plate
-- lookups are a near-certain future feature ("show me every appearance
-- of ZG-1234-AB"). Indexed case-insensitive for that query.

ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'ai', 'ai_reviewed'));

ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS plate text;

ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS ai_described_at timestamptz;

-- Case-insensitive plate lookup. Partial index keeps it small — most
-- rows are people / pets without a plate.
CREATE INDEX IF NOT EXISTS identity_labels_plate_idx
    ON identity_labels (lower(plate))
    WHERE plate IS NOT NULL;

-- Identity-level audit op set widens: ai_describe captures every VLM
-- call (manual via "Ask AI" button, automatic via the lifespan loop)
-- with the parsed suggestion in payload.
ALTER TABLE identity_audit
    DROP CONSTRAINT IF EXISTS identity_audit_op_check;
ALTER TABLE identity_audit
    ADD CONSTRAINT identity_audit_op_check
    CHECK (op IN ('merge', 'split', 'auto_match', 'ai_describe'));
