-- Where a vehicle came to rest, so a place can bind to a car that stopped IN it.
--
-- `claim_place` relates a track to a place by nothing at all: the most recently
-- ended vehicle track on any camera wins. On 22.08 the shed track came to rest
-- over the car standing in P2 and was a perfectly legal occupant of P1 — the
-- name was wrong because the binding was, and no amount of care about plates
-- could have saved it.
--
-- Normalised [x1,y1,x2,y2], the same space `scene_regions.polygon` uses, so the
-- comparison needs no conversion layer. Written only for a track that actually
-- came to rest; a car that drove through leaves this NULL and can never be
-- offered as an occupant.
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS rest_bbox real[];

CREATE INDEX IF NOT EXISTS tracks_rest_idx
    ON tracks (ended_at DESC) WHERE rest_bbox IS NOT NULL;
