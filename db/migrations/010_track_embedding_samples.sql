-- Continuous embeddings produced by the embedder during a track's lifetime.
--
-- The embedder doesn't know the final `tracks.id` yet — that uuid only
-- exists after event-manager finalizes the track on timeout. So we key
-- samples by (camera_id, local_track_id) — the ByteTrack id, which is
-- stable for the track's lifetime within one camera process. At finalize,
-- event-manager fills `track_id` on matching samples and picks the best
-- one (highest detection confidence) to copy into `tracks.embedding`.
--
-- We keep the samples afterwards for re-ID debugging and mid-track
-- analytics; a background job can prune entries older than the retention
-- window.

CREATE TABLE IF NOT EXISTS track_embedding_samples (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id      uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    -- ByteTrack id scoped per camera process. Stable during a track's
    -- lifetime, will eventually roll over — that's why event-manager has
    -- to claim samples by (camera_id, local_track_id, time window).
    local_track_id integer      NOT NULL,
    -- NULL until event-manager finalizes the parent track and claims its
    -- samples. SET NULL on track delete so we keep the embedding history.
    track_id       uuid                  REFERENCES tracks(id) ON DELETE SET NULL,
    captured_at    timestamptz  NOT NULL DEFAULT now(),
    -- Source-decoder PTS at the moment of capture. Used by the recordings
    -- UI to jump straight to "the frame where this embedding was taken".
    pts_ns         bigint       NOT NULL,
    -- Detector frame sequence — same coordinate space as bbox below.
    sequence       integer      NOT NULL,
    -- Detector confidence on the bbox we cropped. The finalize step picks
    -- the sample with the highest confidence as the canonical embedding.
    confidence     real         NOT NULL,
    -- Bbox in source-frame pixel coordinates (the same space tracks emit).
    -- Stored as a fixed-length real[4] = [x1, y1, x2, y2].
    bbox           real[]       NOT NULL CHECK (array_length(bbox, 1) = 4),
    -- DINOv2 ViT-S/14 → 384 dims. Same model family as tracks.embedding so
    -- they can be compared directly without a re-projection layer.
    embedding      vector(384)  NOT NULL
);

-- Lookup pattern at finalize: "all samples for (cam, local_track_id) within
-- the track's time window". The captured_at component bounds the rollover
-- risk on local_track_id.
CREATE INDEX IF NOT EXISTS track_embedding_samples_lookup_idx
    ON track_embedding_samples (camera_id, local_track_id, captured_at);

-- Once claimed, finalized samples are queried by track_id (e.g. UI shows
-- "all observations of this identity").
CREATE INDEX IF NOT EXISTS track_embedding_samples_track_idx
    ON track_embedding_samples (track_id)
    WHERE track_id IS NOT NULL;

-- HNSW for similarity search across samples (mid-track re-ID, "find
-- similar people across the day").
CREATE INDEX IF NOT EXISTS track_embedding_samples_embedding_idx
    ON track_embedding_samples USING hnsw (embedding vector_cosine_ops);
