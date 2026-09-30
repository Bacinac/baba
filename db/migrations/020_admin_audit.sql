-- Audit log for admin CRUD actions on cameras / users / zones / ai
-- settings. Identity ops have their own dedicated audit table (see
-- 012_identity_audit.sql) because that timeline is shown directly in
-- the Identities UI; this one is operator-oriented (forensics, "who
-- changed the recording retention on cam03 last Tuesday").
--
-- Kept separate from `events` (which carries pipeline signals) and
-- from `identity_audit` (identity-scoped) so the three timelines stay
-- queryable independently. A future "audit viewer" page can union all
-- three for a god-view if needed.

CREATE TABLE IF NOT EXISTS admin_audit (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    at            timestamptz  NOT NULL DEFAULT now(),
    -- Who did it. NULL for system-initiated changes (bootstrap admin
    -- create, future scheduled jobs). SET NULL on user delete so the
    -- log survives account removal.
    user_id       uuid                  REFERENCES users(id) ON DELETE SET NULL,
    -- What kind of object: 'camera', 'user', 'zone', 'ai_settings', etc.
    -- Free-form text (with a CHECK to keep typos out) instead of an
    -- enum so adding new resource types doesn't need a migration.
    resource_type text         NOT NULL CHECK (resource_type IN (
        'camera', 'user', 'zone', 'ai_settings'
    )),
    -- The id of the affected row when applicable; NULL for ops that
    -- don't have a stable id (e.g. an ai_settings provider key).
    resource_id   uuid,
    -- 'create', 'update', 'delete'. Same shape as standard CRUD verbs;
    -- a 'restore' or similar can be added by widening the CHECK later.
    op            text         NOT NULL CHECK (op IN ('create', 'update', 'delete')),
    -- Free-form bag. For 'update' we store {before: {...}, after: {...}}
    -- with only the diffed fields so the row stays small even if the
    -- resource has lots of unchanged columns.
    payload       jsonb        NOT NULL DEFAULT '{}'::jsonb
);

-- Per-resource history ("show me all changes to camera X")
CREATE INDEX IF NOT EXISTS admin_audit_resource_idx
    ON admin_audit (resource_type, resource_id, at DESC);

-- Global recent-activity feed
CREATE INDEX IF NOT EXISTS admin_audit_at_idx
    ON admin_audit (at DESC);

-- Per-user activity ("what did this operator do?")
CREATE INDEX IF NOT EXISTS admin_audit_user_at_idx
    ON admin_audit (user_id, at DESC) WHERE user_id IS NOT NULL;
