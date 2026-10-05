-- A region that cannot tell its states apart HOLDS its last committed state, so
-- a dusk or a passing car never files a transition. Held for minutes, that
-- state is no longer something BABA can vouch for: on 05.10 the gate read
-- `unknown` from 09:17 and DIDA showed it open over a closed gate until the
-- region was re-taught at 09:27.
--
-- current_state stays exactly as it was — the place registry votes on it — and
-- what goes off-box is published_state: `unknown` while the evaluator has
-- marked the region unsure, the committed state otherwise. One rule, read by
-- the NOTIFY below and by the snapshot's seed alike.
ALTER TABLE scene_region_status
    ADD COLUMN IF NOT EXISTS unsure_since timestamptz,
    ADD COLUMN IF NOT EXISTS unsure boolean NOT NULL DEFAULT false;

ALTER TABLE scene_region_status
    ADD COLUMN IF NOT EXISTS published_state text
        GENERATED ALWAYS AS (CASE WHEN unsure THEN 'unknown' ELSE current_state END) STORED;

CREATE OR REPLACE FUNCTION scene_status_notify() RETURNS trigger AS $$
BEGIN
    -- Only a change in what consumers see is worth waking them for; the
    -- evaluator rewrites last_eval_at/last_distance on every tick.
    IF TG_OP = 'UPDATE' AND NEW.published_state IS NOT DISTINCT FROM OLD.published_state THEN
        RETURN NEW;
    END IF;
    PERFORM pg_notify(
        'scene_status_changed',
        jsonb_build_object('region_id', NEW.region_id, 'state', NEW.published_state)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
