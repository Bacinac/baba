-- Per-delivery audit for notification fan-out.
--
-- `notification_channels.last_used_at`/`last_error` and
-- `notification_rules.last_fired_at`/`last_error` are last-write-wins
-- — useful for the channel listing page but no history. Operators
-- investigating "did Slack actually get the 3am alarm" need the full
-- timeline. One row per attempted delivery, no retries (the v1
-- dispatcher does single-shot per channel, see rules_dispatcher.py),
-- keeps the model simple.
--
-- Retention: NOT capped by this migration — `samples_retention_days`
-- style sweeper can be added once the table grows beyond comfort.
-- Estimated row volume: per-event × matching-rules × channels. A
-- realistic 8-camera fleet at 100 events/day with one rule firing
-- two channels = 200 rows/day = 73k rows/year. Trivial.

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    at            timestamptz  NOT NULL DEFAULT now(),
    -- Both FKs are SET NULL on delete so deliveries survive even if
    -- the operator later removes the channel or rule. The audit value
    -- is the historical record, not the live config snapshot.
    rule_id       uuid                  REFERENCES notification_rules(id) ON DELETE SET NULL,
    channel_id    uuid                  REFERENCES notification_channels(id) ON DELETE SET NULL,
    event_id      uuid                  REFERENCES events(id) ON DELETE SET NULL,
    -- Mirrors event.kind at delivery time. Stored on the delivery row
    -- so filtering "all telegram alerts for zone_enter last week" stays
    -- a single-table scan even after the source event is purged by a
    -- future retention sweep.
    event_kind    text         NOT NULL,
    -- 'webhook' / 'slack' / 'telegram' / 'smtp'. Same enum as
    -- notification_channels.kind; duplicated here so the UI doesn't
    -- need a join for the most common filter.
    channel_kind  text         NOT NULL,
    ok            boolean      NOT NULL,
    error         text,
    -- Wall-clock latency of the dispatch() call in ms. Useful for
    -- "is the SMTP relay degrading?" Grafana-style trend lines.
    duration_ms   integer
);

CREATE INDEX IF NOT EXISTS notification_deliveries_at_idx
    ON notification_deliveries (at DESC);

CREATE INDEX IF NOT EXISTS notification_deliveries_channel_at_idx
    ON notification_deliveries (channel_id, at DESC);

CREATE INDEX IF NOT EXISTS notification_deliveries_rule_at_idx
    ON notification_deliveries (rule_id, at DESC);

CREATE INDEX IF NOT EXISTS notification_deliveries_failed_at_idx
    ON notification_deliveries (at DESC) WHERE NOT ok;
