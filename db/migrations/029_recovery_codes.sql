-- Recovery codes for 2FA TOTP.
--
-- One-shot codes the operator can use in place of the TOTP code if
-- they lose access to their authenticator (lost phone, broken app).
-- Generated as a batch of 10 at /auth/2fa/enable time + on operator-
-- triggered regenerate; the operator MUST copy them down then (only
-- chance — backend never returns them again).
--
-- Stored as argon2 hash (same hasher as users.password_hash) so a DB
-- read alone doesn't yield usable codes. used_at NULL = unused; on
-- successful login-with-recovery the row is stamped, never deleted —
-- leaves a forensic trail of "which codes have been spent".

CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id        uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id   uuid         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- argon2 hash of the plaintext code. Verifying is O(login-attempt)
    -- which bounds brute-force fine for the 6-char-base32 codespace.
    code_hash text         NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at    timestamptz
);

-- The login path needs "give me all unused codes for this user" — a
-- partial index on user_id WHERE used_at IS NULL is exactly that.
CREATE INDEX IF NOT EXISTS user_recovery_codes_unused_idx
    ON user_recovery_codes (user_id) WHERE used_at IS NULL;
