-- Whether the subject was standing still when this crop was taken.
--
-- A vehicle's canonical body embedding is the highest-confidence sample of its
-- track, and confidence says nothing about what is in the frame. A car that
-- came to rest overlapping its neighbour produces its sharpest, most confident
-- crops exactly there — the same resting box that read the neighbour's plate
-- on 22.08 embeds the neighbour just as willingly, and that embedding is then
-- what every appearance comparison about this car uses.
--
-- NULL for every sample written before this column existed: unknown, not
-- false, so history is never mistaken for evidence of movement.
ALTER TABLE track_embedding_samples ADD COLUMN IF NOT EXISTS at_rest boolean;

CREATE INDEX IF NOT EXISTS track_embedding_samples_moving_idx
    ON track_embedding_samples (track_id, confidence DESC)
 WHERE at_rest IS NOT TRUE;
