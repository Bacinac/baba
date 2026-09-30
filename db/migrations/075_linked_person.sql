-- Whose vehicle this is — the one fact the system was missing while it watched
-- Ana's car drive off and kept her presence episode open on the patio.
--
-- The link works in ONE direction only, and the asymmetry is the point:
--   * a departing linked vehicle may CLOSE its person's presence (the person
--     went quiet right as their car left — they left with it);
--   * an arriving vehicle must never NAME a person. Anyone can drive the car,
--     and "probably its owner" is the same class of weak evidence that keeps
--     body appearance from ever naming a face identity.
ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS linked_person uuid;

COMMENT ON COLUMN identity_labels.linked_person IS
    'For vehicle identities: the person identity (global_id) this vehicle '
    'belongs to. Lets a departure close the person''s presence episodes; '
    'never used to name anyone.';

-- The new close reason. The CHECK carries an auto-generated name, so it is
-- dropped by that name and re-created with the extended list.
ALTER TABLE presence_episodes
    DROP CONSTRAINT IF EXISTS presence_episodes_closed_by_check;
ALTER TABLE presence_episodes
    ADD CONSTRAINT presence_episodes_closed_by_check
    CHECK (closed_by IN ('elsewhere', 'absence', 'stale', 'vehicle'));
