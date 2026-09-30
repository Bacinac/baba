-- Who stands in which place, recorded when it happens rather than guessed when
-- asked.
--
-- Until now nothing stored this. `parked_assignments` derived it on every read
-- by pairing each occupied place with whichever named vehicle's track had
-- ended nearest that place's `present` timestamp. That is a guess about time,
-- not a claim about identity, and it degrades in exactly the way a guess does:
-- on 2026-07-29 the Shed view of P1 had been stuck at `present` since the 27th,
-- so the nearest track end to that stale hour belonged to the other household
-- car — and P1 was reported as Ana's while Marko's stood in it. Nothing was
-- corrupt; the answer simply changed as other cars came and went, and would
-- have changed back the next morning.
--
-- One row per EPISODE of occupancy, not one per place: `released_at IS NULL`
-- is the current state, and a closed row answers "how long was it parked"
-- without asking a track how long it lived. That separation is the point —
-- a track is a span of movement, occupancy is a property of the place.
CREATE TABLE IF NOT EXISTS place_occupancy (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Matches scene_regions.place: the physical spot, which several cameras
    -- may view. Free text for the same reason it is free text there.
    place          text        NOT NULL,
    -- Who. NULL is a first-class answer: "occupied, and we do not know by
    -- whom". The old derivation could not say that, so it always named
    -- somebody — which is how a wrong name got published with no evidence
    -- behind it. A blank field is worth more than a confident guess.
    global_id      uuid,
    -- What backs the claim, strongest first:
    --   'plate'     — the registration was read. The only real proof.
    --   'body'      — appearance matched an enrolled reference. Weak: "dark
    --                 estate car seen from above" describes half the street.
    --   'inherited' — carried from another camera's view of the same place.
    --   'unknown'   — occupied, unidentified. global_id is NULL.
    evidence       text        NOT NULL
                   CHECK (evidence IN ('plate', 'body', 'inherited', 'unknown')),
    -- The track that took the spot, for audit: it lets an operator see which
    -- visit the claim came from. Soft reference — retention may prune the
    -- track long before the car moves.
    track_id       uuid,
    occupied_since timestamptz NOT NULL DEFAULT now(),
    released_at    timestamptz
);

-- A place holds at most one car at a time. The partial unique index makes that
-- an invariant rather than an intention: a second view of the same place
-- reporting `present` cannot open a second episode, so the two cameras that
-- watch one spot agree by construction instead of by ordering.
CREATE UNIQUE INDEX IF NOT EXISTS place_occupancy_open_idx
    ON place_occupancy (place) WHERE released_at IS NULL;

-- "Where has this vehicle been parked" and the retention sweep both read by
-- identity and time.
CREATE INDEX IF NOT EXISTS place_occupancy_gid_idx
    ON place_occupancy (global_id, occupied_since DESC) WHERE global_id IS NOT NULL;

-- Seed the places that are occupied right now, WITHOUT names.
--
-- Carrying the old derivation over one last time was tried and rejected: run
-- against live data it put the same car in both P1 and P2, because it lacked
-- the one-car-per-place pass the reader did at the end. That is the whole
-- reason this table exists, and seeding it with a guess would have started the
-- registry off asserting something it could not defend.
--
-- So the spots start occupied and unidentified, which is true: nobody recorded
-- who took them, and that cannot be recovered after the fact. They get a name
-- the first time each car actually arrives.
INSERT INTO place_occupancy (place, evidence, occupied_since)
SELECT sr.place, 'unknown', max(s.current_state_since)
FROM scene_regions sr
JOIN scene_region_status s ON s.region_id = sr.id
WHERE sr.place IS NOT NULL AND s.current_state = 'present'
GROUP BY sr.place
ON CONFLICT DO NOTHING;

COMMENT ON TABLE place_occupancy IS
    'One row per episode of a place being occupied. released_at IS NULL is the '
    'current state; a closed row gives the parked duration.';
COMMENT ON COLUMN place_occupancy.global_id IS
    'Identity of the occupying vehicle, or NULL when unidentified — which is a '
    'valid answer, not a gap to be filled by guessing.';
