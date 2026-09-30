-- One car stands in one place — now an invariant, not an intention.
--
-- The registry guaranteed one car PER PLACE (073's partial unique) but nothing
-- guaranteed one place PER CAR, and the gap was found the honest way: a
-- visitor whose plate read ZG8778JB was body-matched to "Ana's Car" and
-- claimed P4 while her actual car stood at P2 under its read plate — the same
-- vehicle open in two places, on two grades of evidence, at once.
--
-- Self-heal before the index: wherever one vehicle holds several open
-- episodes, the strongest evidence keeps the name (ties: the older episode);
-- the rest stay occupied but unidentified, which is what they truthfully are.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY global_id
               ORDER BY CASE evidence WHEN 'plate' THEN 2 WHEN 'body' THEN 1
                        ELSE 0 END DESC,
                        occupied_since ASC
           ) AS rn
    FROM place_occupancy
    WHERE released_at IS NULL AND global_id IS NOT NULL
)
UPDATE place_occupancy o
SET global_id = NULL, evidence = 'unknown'
FROM ranked r
WHERE o.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS place_occupancy_open_vehicle_idx
    ON place_occupancy (global_id)
    WHERE released_at IS NULL AND global_id IS NOT NULL;
