-- Three of the five incident kinds re-derived a judgement the pipeline had
-- already made. The tracker counts births and flips per camera; the
-- phantom-spot registry suppresses a birth spot that never moves. Measured
-- here: patio/car stood at 1538 births with zero movers across 443 h, held
-- down by the registry the whole time, while birth_starved reopened over that
-- same object every day and could act on none of it.
--
-- 605 rows in those kinds, every one of them `closed` — no operator ever
-- applied or dismissed one. The guard is on status, not on kind alone, so an
-- incident someone did act on survives even in a kind being retired.
DELETE FROM telemetry_incidents
WHERE kind IN ('birth_starved', 'flip_spike', 'track_churn')
  AND status = 'closed';

-- A retired kind can no longer be evaluated, so an open one would never find
-- its closing condition again.
UPDATE telemetry_incidents
SET closed_at = COALESCE(closed_at, now()), status = 'closed'
WHERE kind IN ('birth_starved', 'flip_spike', 'track_churn')
  AND status IN ('open', 'analyzed');
