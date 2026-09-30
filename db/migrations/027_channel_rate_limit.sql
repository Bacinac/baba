-- Per-channel outbound rate limit.
--
-- A zone_enter rule fired on a busy doorway can fan out 100+
-- notifications/min to the same Slack channel before anyone notices.
-- This column caps deliveries per channel per minute; the dispatcher
-- holds a token bucket per (channel_id, minute) and drops excess
-- attempts with `error="rate_limited"` so the audit row records why
-- the operator stopped getting alerts.
--
-- 0 = unlimited (default — backward compatible with existing rows).
-- Typical good values: 6 for Slack/Telegram (one every 10s), 60 for
-- email batches, 1000 for webhook integrations that handle volume.

ALTER TABLE notification_channels
    ADD COLUMN IF NOT EXISTS rate_limit_per_min integer NOT NULL DEFAULT 0;
