-- Notification channels (outbound delivery endpoints).
--
-- Each row = one place to send notifications to (an SMTP recipient, a
-- Slack webhook, a Telegram chat, a generic JSON POST endpoint). Rule
-- binding (which event triggers which channel) comes in a follow-up
-- migration once the rules engine lands — for now the table is the
-- inventory the UI manages and a future fire-on-event task will read.
--
-- Sensitive config (api keys, SMTP passwords, telegram bot tokens) is
-- encrypted via the same Fernet wrapper that protects ai_settings.
-- `config_encrypted` holds the per-kind config blob as bytea so a
-- table dump leaks nothing — operators must export through the API.
-- Non-sensitive presentation hints (display name, target preview) live
-- in plaintext columns so the listing UI doesn't need decrypt access.

CREATE TABLE IF NOT EXISTS notification_channels (
    id                uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text         NOT NULL,
    -- 'webhook', 'slack', 'telegram', 'smtp'. New kinds add a CHECK
    -- entry + a backend adapter; the column type stays text so adding
    -- one is a single-migration affair.
    kind              text         NOT NULL CHECK (kind IN (
        'webhook', 'slack', 'telegram', 'smtp'
    )),
    enabled           boolean      NOT NULL DEFAULT true,
    -- Encrypted config bag. Decrypted by the api at send time; never
    -- returned to the UI in raw form. Shape per kind:
    --   webhook:  {url, headers?, method?}
    --   slack:    {url}                          (incoming-webhook url)
    --   telegram: {bot_token, chat_id}
    --   smtp:     {host, port, username, password, from, to, use_tls}
    config_encrypted  bytea        NOT NULL,
    -- Presentation-only preview (e.g. "smtp ops@example.com" or "slack
    -- #alerts"). Operator-supplied at create time; never derived from
    -- decrypted config so a leak of this column is harmless.
    target_preview    text,
    last_used_at      timestamptz,
    last_error        text,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notification_channels_kind_idx
    ON notification_channels (kind);

CREATE INDEX IF NOT EXISTS notification_channels_enabled_idx
    ON notification_channels (enabled) WHERE enabled;

DROP TRIGGER IF EXISTS notification_channels_touch ON notification_channels;
CREATE TRIGGER notification_channels_touch
    BEFORE UPDATE ON notification_channels
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
