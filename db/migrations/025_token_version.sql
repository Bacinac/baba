-- Per-user JWT revocation counter.
--
-- The api's stateless JWT cookie was previously revocable only by
-- rotating BABA_SECRET_KEY (logs everyone out). This counter gives
-- per-user precision: bump on password change / admin reset / role
-- change and every previously-issued token is rejected on next call.
--
-- Default 0 + NOT NULL so the existing row population works without
-- an UPDATE. The encode/decode helpers stamp the current value into
-- the JWT's `tv` claim; current_user compares against the live
-- DB value and rejects on mismatch.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0;
