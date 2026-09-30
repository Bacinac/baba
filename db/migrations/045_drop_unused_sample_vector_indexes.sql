-- track_embedding_samples carried two HNSW vector indexes that re-ID never
-- queries: it does similarity on `tracks` + `identity_labels`, and only ever
-- reads samples BY track_id (to aggregate a track's canonical embedding at
-- finalize) — never by vector distance. On the live .200 these grew to 24 GB
-- (embedding) + 174 MB (face) with 0 index scans — pure dead weight on the
-- fast SSD state tier. Drop them; the by-track lookup indexes stay.
DROP INDEX IF EXISTS track_embedding_samples_embedding_idx;
DROP INDEX IF EXISTS track_embedding_samples_face_idx;

-- This is a high-churn table (samples inserted continuously, bulk-deleted at
-- the 30-day retention). Make autovacuum keep the heap + TOAST tight so it
-- doesn't re-bloat between the default 20%-dead triggers.
ALTER TABLE track_embedding_samples SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.05,
    toast.autovacuum_vacuum_scale_factor = 0.05
);
