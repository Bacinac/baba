-- TOTP 2FA support.
--
-- `totp_secret` is the base32-encoded shared secret an authenticator
-- app uses to generate 6-digit codes. NULL until the user enrolls. It
-- stays NULL after disable too — disabling wipes the secret so a
-- re-enrolment generates a fresh one (no value in reusing the old).
--
-- `totp_enabled` gates the login flow: with secret set + enabled true,
-- /auth/login demands a `totp_code` field in the body in addition to
-- the password. With secret set + enabled false, the user is in the
-- middle of setup (the secret is provisioned but unconfirmed) and the
-- login flow ignores it.
--
-- Secret is stored plaintext for v1 — Fernet-encrypting it adds nothing
-- meaningful: the api process needs to decrypt on every login anyway
-- and an attacker with DB read access also has the Fernet key (both
-- come from BABA_SECRET_KEY). Real defence is filesystem perms on
-- ${BABA_STATE_HOST}/postgres and an off-host backup of secrets.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS totp_secret  text,
    ADD COLUMN IF NOT EXISTS totp_enabled boolean NOT NULL DEFAULT false;
