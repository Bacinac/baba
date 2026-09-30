-- Raise phantom_min_span_s from 2 h to 24 h on deployments that already ran
-- migration 062. (062 itself now seeds 86400, so a fresh install never sees
-- 7200 and this is a no-op there — it exists because .11 and .200 had already
-- applied 062 when the evidence below came in.)
--
-- Why, from the live registry on .11 minutes after 062 went out:
--
--   slug  | class | births | movers | span_s
--   west  | car   |      9 |      0 |     55
--
-- That is the carport car. It is REAL, it is parked, and it flickers exactly
-- like the garden hose this subsystem was built for: same pixels, over and
-- over, nothing ever moving. The phantom discriminator cannot tell them apart
-- while the car sits still, because a parked car banks its mover only when it
-- finally DRIVES.
--
-- The span is what resolves it, and 24 h is the number that makes the
-- difference structural rather than lucky. A car in daily use always moves
-- within a day, banks its mover, and permanently immunises its spot (movers>0
-- disqualifies a spot forever). At 2 h, any car left overnight would have been
-- blanked before it ever got the chance — a real vehicle silently missing from
-- Live, which is precisely the class of failure BABA exists to not have.
--
-- The cost is that a true phantom now needs a day of evidence instead of two
-- hours. That is the right trade: being slow to hide a hose is an
-- inconvenience, being quick to hide a car is a bug.
--
-- An operator-tuned value is left alone — only the shipped 7200 default is
-- moved.
UPDATE app_settings
SET value = value || jsonb_build_object('phantom_min_span_s', 86400)
WHERE key = 'tracking_defaults'
  AND value->>'phantom_min_span_s' = '7200';
