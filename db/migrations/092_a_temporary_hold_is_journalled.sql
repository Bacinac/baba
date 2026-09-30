-- A temporary profile hold is a thing that happened to a camera, so the journal
-- has to be able to say it.
--
-- `camera_setting_changes.source` was a closed set of four, written when the
-- only things that touched a camera's settings were an incident, an operator, a
-- band switch and a revert. The plate profile — swapped in for a few seconds
-- when a headlight drowns the ALPR zone — is none of those, and so could not be
-- recorded at all: the row was rejected by the CHECK.
--
-- The cost of that was a question nobody could answer. Ana's car crossed
-- west's plate zone on 02.09 with the zone 48 to 77 per cent saturated for
-- twenty-one seconds and no plate was read, and whether the swap ever fired
-- could only be guessed at from frames afterwards.
--
-- `hold` and `release` rather than the reason itself ('headlight', 'plate'),
-- because a reason is a thing that grows and this column is a closed domain —
-- the next reason would fail the same way. Which profile it was is in the row's
-- own `after`.
ALTER TABLE camera_setting_changes DROP CONSTRAINT IF EXISTS camera_setting_changes_source_check;
ALTER TABLE camera_setting_changes ADD CONSTRAINT camera_setting_changes_source_check
    CHECK (source IN ('incident_apply', 'manual', 'profile_switch', 'revert',
                      'hold', 'release'));
