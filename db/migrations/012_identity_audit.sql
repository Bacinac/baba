-- Audit log for identity-level operations. Captures both human actions
-- (merge / split from the UI) and automatic re-ID hits from event-manager,
-- so the Identities UI can show a complete history of how a global_id
-- came to be what it is.
--
-- Doubles as the source-of-truth for live updates: every INSERT fires a
-- pg_notify('identities_changed', …) so WS clients can refetch without
-- polling. Reusing the events table for this would have meant making
-- events.camera_id nullable (merge/split aren't tied to a camera), and
-- it would mix pipeline signals with admin actions in the same query
-- path. A dedicated table is cleaner.

CREATE TABLE IF NOT EXISTS identity_audit (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    at           timestamptz  NOT NULL DEFAULT now(),
    -- Who did it. NULL for system-initiated ops (auto_match). SET NULL
    -- on user delete so the audit history survives account removal.
    user_id      uuid                  REFERENCES users(id) ON DELETE SET NULL,
    -- 'merge'      — user fused two identities (payload: from, into, count)
    -- 'split'      — user broke one track out (payload: from_gid, track_id, new_gid)
    -- 'auto_match' — re-ID at finalize linked a new track to an existing
    --                identity (payload: track_id, into_gid, dist)
    op           text         NOT NULL CHECK (op IN ('merge', 'split', 'auto_match')),
    -- Op-specific bag. Always includes a `gid` field where applicable so
    -- the per-identity history view can filter by `payload->>'gid'`.
    payload      jsonb        NOT NULL DEFAULT '{}'::jsonb
);

-- Most queries are "show me the history for THIS identity, newest first".
-- Index on (gid, at desc) using a jsonb expression. We index the payload's
-- 'gid' key as a uuid for efficient equality + ordering. Postgres can use
-- this for index-only scans of the timeline view.
CREATE INDEX IF NOT EXISTS identity_audit_gid_at_idx
    ON identity_audit (((payload->>'gid')::uuid), at DESC);

-- Global recent-activity feed (system-level overview).
CREATE INDEX IF NOT EXISTS identity_audit_at_idx
    ON identity_audit (at DESC);

-- Live updates. Payload is small enough (op + at + gid) to embed in the
-- NOTIFY message itself; full row fetched on demand if needed.
CREATE OR REPLACE FUNCTION identity_audit_notify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('identities_changed', jsonb_build_object(
        'id', NEW.id,
        'op', NEW.op,
        'at', NEW.at,
        'payload', NEW.payload
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS identity_audit_notify ON identity_audit;
CREATE TRIGGER identity_audit_notify
    AFTER INSERT ON identity_audit
    FOR EACH ROW EXECUTE FUNCTION identity_audit_notify();
