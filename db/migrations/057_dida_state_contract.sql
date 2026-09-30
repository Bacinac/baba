-- Groundwork for the authoritative per-camera state snapshot (baba.state.<uuid>).
--
-- DIDA became a pure mirror: BABA owns the frames, tracker and zones, so BABA
-- owns the truth AND all the hysteresis. DIDA dropped its stale-track sweep,
-- its 45 s zone off-delay and its PIR cooldown. Three gaps had to close first:
--
-- 1. `cameras.doorbell` — which cameras are doorbells lived ONLY in the
--    doorbell service's env (BABA_DOORBELL_SLUGS). The roster (and therefore
--    DIDA's catalog) had no way to know. The camera set is authoritative in
--    Postgres, not in an env var — same rule the detector already follows — so
--    the flag becomes a column. The doorbell service syncs it from its
--    configured slugs on startup, so the env stays the operator knob and the DB
--    stays the single source consumers read.
--
-- 2. Scene state labels were part Croatian: the gate carried
--    {otvoreno,zatvoreno} while the parking regions added later use
--    {present,empty}. Scene STATE labels are technical identifiers and stay
--    English + generic (the region NAME is what gets localised in the UI), so
--    the gate is migrated to {open,closed}. This also makes the snapshot's
--    `scenes` read as the spec's example intends without any translation layer.
--
-- 3. `scene_region_status` had no NOTIFY. Region CONFIG changes fire
--    `scene_regions_changed`, and state transitions become `events` rows, but
--    nothing told a live consumer "this region's state is now X". The snapshot
--    publisher needs exactly that, so status changes now fire
--    `scene_status_changed` with {region_id, state}.

-- ------------------------------------------------------------- 1. doorbell

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS doorbell boolean NOT NULL DEFAULT false;

-- Seed the documented default (BABA_DOORBELL_SLUGS defaults to "doorbell").
-- The doorbell service re-syncs from its own config on every start, so a
-- deployment using a different slug converges on its next boot.
UPDATE cameras SET doorbell = true WHERE slug = 'doorbell' AND NOT doorbell;

-- ------------------------------------------------- 2. gate labels → English

UPDATE scene_regions
   SET states = array_replace(array_replace(states, 'otvoreno', 'open'),
                              'zatvoreno', 'closed')
 WHERE states && ARRAY['otvoreno', 'zatvoreno'];

UPDATE scene_region_prototypes
   SET state_label = CASE state_label
                         WHEN 'otvoreno' THEN 'open'
                         WHEN 'zatvoreno' THEN 'closed'
                         ELSE state_label
                     END
 WHERE state_label IN ('otvoreno', 'zatvoreno');

UPDATE scene_region_status
   SET current_state = CASE current_state
                           WHEN 'otvoreno' THEN 'open'
                           WHEN 'zatvoreno' THEN 'closed'
                           ELSE current_state
                       END,
       last_label_raw = CASE last_label_raw
                            WHEN 'otvoreno' THEN 'open'
                            WHEN 'zatvoreno' THEN 'closed'
                            ELSE last_label_raw
                        END
 WHERE current_state IN ('otvoreno', 'zatvoreno')
    OR last_label_raw IN ('otvoreno', 'zatvoreno');

-- --------------------------------------------- 3. scene status live NOTIFY

CREATE OR REPLACE FUNCTION scene_status_notify() RETURNS trigger AS $$
BEGIN
    -- Only a real transition is worth waking consumers for; the evaluator
    -- rewrites last_eval_at/last_distance on every tick.
    IF TG_OP = 'UPDATE' AND NEW.current_state IS NOT DISTINCT FROM OLD.current_state THEN
        RETURN NEW;
    END IF;
    PERFORM pg_notify(
        'scene_status_changed',
        jsonb_build_object('region_id', NEW.region_id, 'state', NEW.current_state)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS scene_region_status_notify ON scene_region_status;
CREATE TRIGGER scene_region_status_notify
    AFTER INSERT OR UPDATE ON scene_region_status
    FOR EACH ROW EXECUTE FUNCTION scene_status_notify();
