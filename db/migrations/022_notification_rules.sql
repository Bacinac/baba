-- Notification rules — bind events to channels.
--
-- A rule answers: "which events should fire which channels, with what
-- filters". Lives next to notification_channels (021); the api service
-- LISTENs on `events_new` (already fired by the events_notify trigger
-- in migration 002) and for each event evaluates active rules in turn.
--
-- The filter shape is intentionally schemaless jsonb — adding a new
-- filter dimension (e.g. zone-name match once the zone rule engine
-- lands) only needs an evaluator update, no migration. v1 filters:
--   { "camera_ids": [uuid, ...], "class_ids": [int, ...] }
--
-- A rule with no filter matches every event of the configured kind.

CREATE TABLE IF NOT EXISTS notification_rules (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text         NOT NULL,
    enabled         boolean      NOT NULL DEFAULT true,
    -- Event kind to match. NULL = match all kinds (rare; most operators
    -- want a specific signal). 'track_finalized' is the canonical event
    -- the pipeline emits; new kinds (zone_enter, reid_match, ...) hook
    -- in here without a migration.
    event_kind      text,
    -- Free-form filter. See notes above; evaluator in
    -- baba_api.rules_dispatcher handles unknown keys conservatively
    -- (unknown filter key → skip the rule rather than dispatch on
    -- something we don't understand).
    filter          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    -- Channels to fan out to. ON DELETE: when a channel goes away the
    -- rule's list is cleaned up in app code (a direct array element
    -- constraint requires either PG 16+ array FK support or a join
    -- table; the latter is overkill for v1's expected scale). The
    -- dispatcher silently skips channel ids that no longer exist.
    channel_ids     uuid[]       NOT NULL DEFAULT '{}'::uuid[],
    -- Operator-facing diagnostics (last delivery success + last error).
    -- Set by the dispatcher after every fire — keeps the UI honest
    -- about whether the rule is actually working.
    last_fired_at   timestamptz,
    last_error      text,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- "Find me all rules that match THIS event_kind, enabled only" — the
-- hot path for the dispatcher. Partial index keeps it small even when
-- the operator stockpiles many disabled rules for testing.
CREATE INDEX IF NOT EXISTS notification_rules_kind_idx
    ON notification_rules (event_kind) WHERE enabled;

DROP TRIGGER IF EXISTS notification_rules_touch ON notification_rules;
CREATE TRIGGER notification_rules_touch
    BEFORE UPDATE ON notification_rules
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
