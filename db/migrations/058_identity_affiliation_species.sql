-- Structured identity attributes: affiliation + resident + species.
--
-- WHY NOT TAGS. All three lived in the free-text `tags` array (the UI's "role"
-- chips wrote Croatian tags: stanar/susjed/obitelj/gost/dostava/servis/
-- sluzbeno/nepoznato), and it had already drifted after six identities: Ana
-- carried BOTH `obitelj` and `family` — one concept, two languages — while Marko
-- carried only `obitelj`, so an automation filtering "family" matched her and
-- silently missed him. Tags have no vocabulary, so anything an automation keys
-- on cannot live there. Tags stay for what they're good at: free description
-- (blonde, tattoo, "Mazda CX-5"), including family nuance like kći/sestra that
-- no automation should ever branch on.
--
-- WHY TWO COLUMNS, NOT ONE. The old chips were multi-select and mixed two
-- INDEPENDENT axes, which is exactly why they overlapped (Ana was both
-- `stanar` AND `obitelj`). Nika settles it: she is family but no longer lives
-- here. Relationship and residency are orthogonal — collapsing them into one
-- "household" value leaves her nowhere to go:
--     Marko/Ana → family + resident      Nika  → family, NOT resident
--     neighbour → neighbour, not resident  lodger → resident, not family
-- So: `affiliation` = what someone is TO US (single value, automations key on
-- it), `resident` = whether they live here (drives arm/disarm). No inner-vs-
-- extended family split: residency already separates who comes home daily from
-- who visits, and no automation would branch on it.
--
-- Vocabulary is English in the DB (technical identifier — the UI localises it),
-- superset of the old chips so nothing is lost: `service` covers servis,
-- `official` covers sluzbeno. `unknown` is the default so a new or AI-created
-- identity never silently claims to be family — and the AI is never asked to
-- guess affiliation/resident at all: it cannot know that from an image.
--
-- `species` settles cat/dog: the nano detector calls Franka (a cat) a DOG in
-- 19 of her 21 tracks. That flip is the very reason cat+dog are pooled into
-- PET_GROUP for re-ID — i.e. we already decided the detector is NOT the source
-- of truth for what an animal is. The identity is; once re-ID matches a track,
-- the identity's species wins over the per-frame label.

ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS affiliation text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS resident boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS species text;

ALTER TABLE identity_labels DROP CONSTRAINT IF EXISTS identity_labels_affiliation_check;
ALTER TABLE identity_labels
    ADD CONSTRAINT identity_labels_affiliation_check
    CHECK (affiliation IN ('family', 'friend', 'neighbour', 'guest',
                           'delivery', 'service', 'official', 'unknown'));

CREATE INDEX IF NOT EXISTS identity_labels_affiliation_idx
    ON identity_labels(affiliation) WHERE affiliation <> 'unknown';
CREATE INDEX IF NOT EXISTS identity_labels_resident_idx
    ON identity_labels(resident) WHERE resident;

-- ---------------------------------------- seed from the old role/species tags

-- Relationship. Ordered most- to least-specific so a multi-tagged identity
-- lands on the strongest claim (Ana is stanar+obitelj+family → family).
UPDATE identity_labels SET affiliation = 'family'
 WHERE affiliation = 'unknown' AND tags && ARRAY['obitelj', 'family'];
UPDATE identity_labels SET affiliation = 'neighbour'
 WHERE affiliation = 'unknown' AND tags && ARRAY['susjed', 'neighbour'];
UPDATE identity_labels SET affiliation = 'guest'
 WHERE affiliation = 'unknown' AND tags && ARRAY['gost', 'guest'];
UPDATE identity_labels SET affiliation = 'delivery'
 WHERE affiliation = 'unknown' AND tags && ARRAY['dostava', 'delivery'];
UPDATE identity_labels SET affiliation = 'service'
 WHERE affiliation = 'unknown' AND tags && ARRAY['servis', 'service'];
UPDATE identity_labels SET affiliation = 'official'
 WHERE affiliation = 'unknown' AND tags && ARRAY['sluzbeno', 'službeno', 'official'];

-- Residency: the old `stanar` chip meant exactly this.
UPDATE identity_labels SET resident = true
 WHERE NOT resident AND tags && ARRAY['stanar', 'ukućanin'];

-- Species.
UPDATE identity_labels SET species = 'cat'
 WHERE species IS NULL AND tags && ARRAY['cat', 'mačka', 'macka'];
UPDATE identity_labels SET species = 'dog'
 WHERE species IS NULL AND tags && ARRAY['dog', 'pas'];

-- ------------------------------------- strip promoted tags (one fact, one place)

UPDATE identity_labels
   SET tags = ARRAY(
           SELECT t FROM unnest(tags) AS t
            WHERE t <> ALL (ARRAY[
                'stanar', 'ukućanin', 'obitelj', 'family', 'susjed', 'neighbour',
                'gost', 'guest', 'dostava', 'delivery', 'servis', 'service',
                'sluzbeno', 'službeno', 'official', 'nepoznato', 'unknown',
                'cat', 'mačka', 'macka', 'dog', 'pas'
            ])
       )
 WHERE tags && ARRAY[
           'stanar', 'ukućanin', 'obitelj', 'family', 'susjed', 'neighbour',
           'gost', 'guest', 'dostava', 'delivery', 'servis', 'service',
           'sluzbeno', 'službeno', 'official', 'nepoznato', 'unknown',
           'cat', 'mačka', 'macka', 'dog', 'pas'
       ];
