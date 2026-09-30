-- Static-phantom suppression: a persistent, per-camera registry of WHERE
-- tracks are born and whether anything born there ever actually moved.
--
-- The problem (pool camera, 2026-07-16): a garden hose + shrub reads as
-- "person" every few seconds, forever. Nothing in the pipeline could see
-- it for what it is. `birth_eligible` (migration 034 + 056) is a stateless
-- scalar — confidence >= min_confidence — so it cannot tell "55% once, on a
-- real person" from "55% at the same pixels for six months". Raising the
-- per-camera threshold is the only existing lever and it costs recall on
-- distant/dark real people symmetrically. The operator's alternative is to
-- hand-draw an ignore zone per phantom per camera, forever.
--
-- The discriminator that costs nothing: a phantom is born at the same
-- pixels over and over and NOTHING born there ever travels. A real subject
-- arrives — it crosses the frame, so it clears the tracker's real-mover
-- latch (lifetime centroid extent >= 1x its own bbox diagonal). The tracker
-- already computes exactly this (`MotionTracker._REAL_MOVER_RATIO`, wired
-- out as `TrackWire.ever_moved`) and already uses it, but only through the
-- in-memory parked-ghost registry: 5-minute TTL, keyed by track_id, wiped
-- on any movement, gone on restart. Right idea, four wrong properties. This
-- table is that idea at the timescale the evidence actually lives on.
--
-- Suppression requires ALL THREE, and the third is the one that matters:
--
--   births >= N            enough evidence to be sure
--   movers  = 0            nothing born here has EVER travelled
--   last_birth_at - first_birth_at >= SPAN   the births are spread over
--                          HOURS
--
-- Without the span condition this subsystem would break BABA's core
-- promise (reliable stationary/loitering person detection — no motion
-- gating, ever). A person who enters from cover and stands perfectly still
-- can flicker out N births with movers=0 in a couple of minutes and would
-- be suppressed while motionless — the exact Frigate failure we exist to
-- not have. Nobody stands on one pixel for two hours; a hose does it for
-- months. The span is what separates inventory from people, and it is why
-- the count alone is not enough.
--
-- Self-healing in both directions, by construction:
--   * one single track from a spot travels -> movers > 0 -> that spot can
--     never suppress again, permanently. Recall wins ties.
--   * the phantom is removed (hose coiled up) -> no more births -> the row
--     ages out of the prune window.
-- A suppressed track is still tracked internally: if it ever moves, it is
-- emitted from that tick AND banks the mover, so a wrong suppression
-- repairs itself the first time it is wrong.

CREATE TABLE IF NOT EXISTS track_birth_spots (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id      uuid         NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    -- Stabilized class (majority vote), matching the parked-ghost registry:
    -- a one-frame class flip must not dodge or wrongly claim a spot.
    class_name     text         NOT NULL,
    -- [x1, y1, x2, y2] normalised to [0,1]. Normalised, not pixels: the
    -- ingestor's adaptive downscale and per-camera resolution changes must
    -- not orphan a registry built at another size. Matched by IoU (same
    -- 0.5 as the parked-ghost registry), not by grid cell — a phantom's
    -- box jitters between tall/short hypotheses (~0.3-0.4 diag) and a grid
    -- keyed on any single anchor point smears that jitter across cells.
    bbox           real[]       NOT NULL,
    -- Tracks born at this spot, ever.
    births         integer      NOT NULL DEFAULT 0,
    -- Of those, how many ever cleared the real-mover latch. Any value > 0
    -- disqualifies the spot from suppression permanently.
    movers         integer      NOT NULL DEFAULT 0,
    first_birth_at timestamptz  NOT NULL DEFAULT now(),
    last_birth_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now()
);

-- The tracker's hot path: match a newborn bbox against same-camera,
-- same-class spots. Cardinality per (camera, class) is small — a handful of
-- real phantoms plus transient spots that age out.
CREATE INDEX IF NOT EXISTS track_birth_spots_lookup_idx
    ON track_birth_spots(camera_id, class_name);

-- Prune scan.
CREATE INDEX IF NOT EXISTS track_birth_spots_last_birth_idx
    ON track_birth_spots(last_birth_at);

DROP TRIGGER IF EXISTS track_birth_spots_touch ON track_birth_spots;
CREATE TRIGGER track_birth_spots_touch
    BEFORE UPDATE ON track_birth_spots
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Operator-tunable, global layer (Settings -> Detection reads/writes
-- app_settings.tracking_defaults; per-camera overrides are NOT offered
-- here on purpose — the mechanism is meant to need no configuration, and
-- an operator who wants a spot gone regardless of evidence has the ignore
-- zone for that).
--
-- Defaults: 20 births is unreachable by accident. The 24 h span is the
-- anti-motion-gating guard described above, and it is deliberately a whole
-- day rather than the couple of hours the evidence for a HOSE would need:
-- a parked car is a real object that flickers exactly like a phantom (live
-- on .11: west/car, 9 births in 2 min, zero movers) and banks its mover only
-- when it finally DRIVES. A daily-driven car always proves itself inside 24 h
-- and its spot is then immune forever; a shorter window would blank any car
-- left overnight. Both values are floors on evidence, so raising them only
-- ever makes the subsystem more cautious.
INSERT INTO app_settings (key, value)
VALUES (
    'tracking_defaults',
    jsonb_build_object(
        'phantom_min_births', 20,
        'phantom_min_span_s', 86400
    )
)
ON CONFLICT (key) DO UPDATE
SET value = app_settings.value
          || jsonb_build_object(
                'phantom_min_births', COALESCE(app_settings.value->'phantom_min_births', '20'::jsonb),
                'phantom_min_span_s', COALESCE(app_settings.value->'phantom_min_span_s', '86400'::jsonb)
             );
