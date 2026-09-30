-- Name the physical thing a region is looking at, so two cameras can look at it.
--
-- A region today belongs to one camera and means nothing outside it. But P1 is
-- not a rectangle on west, it is a parking space in the yard, and the shed
-- camera can see the same space from the other side. Without a way to say that,
-- every consumer has to guess: the departure rule cannot tell whether "a spot
-- emptied" is one spot seen twice or two spots, and a car identified by its
-- plate while driving cannot be tied to the parked track that follows it,
-- because time overlap is not evidence of place and naming a car by it is
-- exactly how the wrong vehicle got named on 2026-07-27.
--
-- Free text rather than a places table: a place has no properties of its own
-- worth storing, and a join table would add a second thing to keep in sync with
-- the regions that define it. Two regions with the same non-null `place` are
-- views of one physical spot; that is the whole contract.
ALTER TABLE scene_regions
    ADD COLUMN IF NOT EXISTS place text;

COMMENT ON COLUMN scene_regions.place IS
    'Physical thing this region observes, e.g. ''P1''. Regions on different '
    'cameras sharing a place are views of the same spot. NULL = this region '
    'stands for itself.';

CREATE INDEX IF NOT EXISTS scene_regions_place_idx
    ON scene_regions (place) WHERE place IS NOT NULL;

-- The existing west regions already name their spots; adopt those names as
-- places so the link is there the moment a second camera claims one.
UPDATE scene_regions
   SET place = name
 WHERE place IS NULL
   AND name ~ '^P[0-9]+$';
