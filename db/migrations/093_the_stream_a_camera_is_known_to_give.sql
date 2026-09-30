-- The resolution a camera has been seen to deliver, so we notice when it stops.
--
-- go2rtc carries a main stream and a substream for the same camera, in that
-- order, and falls back to the second when the first will not answer. That
-- fallback is sticky: on the night of 04.09 the cameras dropped off the network
-- at 00:49, west came back on `Preview_01_sub`, and the recorder went on
-- copying it — 1536x432 H.264 where the camera gives 4096x1152 HEVC. Nothing
-- noticed for nine hours.
--
-- What it cost is measurable. A plate crossing the ALPR zone that morning was
-- 33 px wide and the reader produced eight different strings from it, none of
-- them the registration; the same plate on the main stream the evening before
-- read cleanly at four times the size. Every detection, crop and face on that
-- camera spent the night at a third of the linear resolution.
--
-- Per CAMERA and not per recording: migration 061 dropped
-- recordings.width/height as speculative — nothing read them, and the rule it
-- left behind is to cache a fact in the DB when it is read in bulk and to probe
-- the file otherwise. This is neither; it is what the camera IS, learned from
-- what it gave, and the only thing that can tell a wrong stream from a right
-- one without a second source of truth to keep in step.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS stream_width int;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS stream_height int;

COMMENT ON COLUMN cameras.stream_width IS
    'Widest video this camera has been seen to deliver. Learned by the '
    'recorder from its own segments; a session that comes in materially '
    'smaller is the substream and is refused.';
