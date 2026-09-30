-- Human-meaningful metadata attached to a `global_id`.
--
-- An identity by itself is just a UUID grouping similar embeddings.
-- This table is where the operator turns "0203e240…" into "Susjed Mate"
-- or "Octavia ZG-1234-AB", tags it ("delivery", "family", "watch"), and
-- optionally enrolls reference photos so the re-ID pipeline can match
-- future sightings to the curated embedding instead of relying purely
-- on the most-recent organic track.
--
-- One row per identity. Deleting the identity (which means deleting all
-- its tracks because there's no `identities` table — `global_id` lives
-- on `tracks`) cascades through `tracks.global_id` → label removal here
-- via the foreign-key-less convention; we just rely on the app to
-- delete this row when the user wipes an identity from the UI.

CREATE TABLE IF NOT EXISTS identity_labels (
    -- Same uuid space as tracks.global_id; not a foreign key because
    -- (a) tracks.global_id has no unique index (one identity = many tracks)
    -- (b) enrolled-from-photo identities won't have any tracks yet.
    global_id           uuid         PRIMARY KEY,

    name                text         NOT NULL,
    -- Loose taxonomy chosen by the user: 'person', 'vehicle', 'pet',
    -- 'package', 'unknown'… we don't enforce a list because users
    -- inevitably want a category we didn't predict.
    kind                text,
    -- Free-form tags. Used by the gallery filter and rule engine
    -- ("alert when any 'watch'-tagged identity appears between 22:00
    -- and 06:00"). Stored as text[] rather than a join table because
    -- the cardinality is tiny per row and we want one-roundtrip writes.
    tags                text[]       NOT NULL DEFAULT '{}',
    notes               text,

    -- Mean DINOv2 embedding across uploaded reference photos. NULL until
    -- the operator runs the photo-enrollment flow. When set, event-manager
    -- searches this column alongside `tracks.embedding` at re-ID time so
    -- newly observed tracks can match a curated identity before they've
    -- accumulated a track history. Same 384-dim vector(384) family as
    -- tracks/embedding so kNN works on either column with the same query.
    reference_embedding vector(384),
    -- How many photos were folded into the average. Lets the UI show
    -- "5 reference photos" and the operator decide whether to add more.
    reference_count     integer      NOT NULL DEFAULT 0 CHECK (reference_count >= 0),

    -- Bookkeeping. created_by NULL on system-enrolled identities (none
    -- today; reserved for future bulk-import).
    created_by          uuid                  REFERENCES users(id) ON DELETE SET NULL,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now()
);

-- Touch updated_at on every modification (matches the cameras table pattern).
DROP TRIGGER IF EXISTS identity_labels_touch ON identity_labels;
CREATE TRIGGER identity_labels_touch
    BEFORE UPDATE ON identity_labels
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- HNSW for kNN search across reference embeddings. Conditional so empty
-- (un-enrolled) rows don't bloat the index. Cosine ops matches the rest
-- of the pipeline.
CREATE INDEX IF NOT EXISTS identity_labels_reference_embedding_idx
    ON identity_labels USING hnsw (reference_embedding vector_cosine_ops)
    WHERE reference_embedding IS NOT NULL;

-- Lookup by tag for the gallery filter / rule engine.
CREATE INDEX IF NOT EXISTS identity_labels_tags_idx
    ON identity_labels USING gin (tags);
