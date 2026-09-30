-- Drop schema nothing reads.
--
-- Verified against the whole tree before writing this (services/, core/, web/src,
-- scripts/): each object below has zero readers, and the two with rows to lose
-- have none — identity_suggestions and cameras.hw_decode were both empty.
--
--  * identity_suggestions — added in 038 for a "Suggestions UI" that was never
--    built. No Python, no TypeScript, no Svelte ever touched it; migration 040
--    still ran an UPDATE against a table nobody read.
--  * cameras.hw_decode — the camera form offers it, the API stores it, and the
--    ingestor NEVER reads it: the decoder is chosen process-wide from
--    BABA_DECODER_BACKENDS (see baba_ingestor/config.py). An operator could
--    type 'h264_cuvid', save, and change nothing. Removing the column with the
--    API field and the UI input is honest; a per-camera decoder override can
--    come back as a real feature if it is ever wanted.
--  * tracks.face_negations — written by nothing, read by nothing since 018.
--  * tracks.face_confirmations — incremented in two places and never SELECTed.
--    018 justified it as "keeps the signal even when a later crop disagrees",
--    but nothing consumes the signal, so the increments were pure write cost.

DROP TABLE IF EXISTS identity_suggestions;

ALTER TABLE cameras DROP COLUMN IF EXISTS hw_decode;

ALTER TABLE tracks
    DROP COLUMN IF EXISTS face_negations,
    DROP COLUMN IF EXISTS face_confirmations;
