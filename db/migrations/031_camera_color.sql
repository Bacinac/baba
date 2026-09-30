-- Per-camera color, used in the storage breakdown bar today and
-- meant to spread to the live mosaic, event chips, and analytics
-- legends later. Stored as a #RRGGBB string so the frontend can use
-- it directly in inline style / Tailwind arbitrary values without
-- a runtime conversion step.
--
-- Existing rows get a round-robin backfill from a fixed 12-color
-- palette in creation order. Round-robin (as opposed to a
-- hash(slug)-based pick) guarantees N distinct colors for the first
-- N cameras instead of risking palette collisions — important
-- because a 12-color palette and 5 cameras still has a meaningful
-- collision probability under uniform hashing. Operators can
-- override per row in the camera settings.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS color text NOT NULL DEFAULT '#f59e0b'
        CHECK (color ~ '^#[0-9a-fA-F]{6}$');

WITH ranked AS (
    SELECT id,
           (ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) - 1)::int AS rn
      FROM cameras
)
UPDATE cameras c
   SET color = (ARRAY[
       '#f59e0b',  -- amber
       '#ef4444',  -- red
       '#ec4899',  -- pink
       '#a855f7',  -- purple
       '#6366f1',  -- indigo
       '#3b82f6',  -- blue
       '#06b6d4',  -- cyan
       '#14b8a6',  -- teal
       '#10b981',  -- emerald
       '#84cc16',  -- lime
       '#eab308',  -- yellow
       '#f97316'   -- orange
   ])[(r.rn % 12) + 1]
  FROM ranked r
 WHERE c.id = r.id;
