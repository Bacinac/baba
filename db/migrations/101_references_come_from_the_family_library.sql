-- Reference photographs of people can now come from OPUS · Library, the
-- household's own photo catalogue, beside Immich.
--
-- Immich was the first external source and stays one: an installation that
-- keeps its photographs there configures it and imports as before. This
-- household's catalogue moved to OPUS in 2026-08 — names there are a person's
-- decision written on the face, not a cluster's guess — so the columns that
-- carried one library's ids now carry any library's.
--
-- What changes here is provenance, not the photographs. References already
-- imported from Immich are pixels on disk in the active model's space and
-- stay exactly as useful; their `origin_ref` keeps the Immich asset uuid it
-- always held, under `source = 'immich'`. OPUS references write
-- `face:<library face id>`.

ALTER TABLE identity_reference_photos
    RENAME COLUMN immich_asset_id TO origin_ref;
ALTER INDEX identity_reference_photos_immich_asset_idx
    RENAME TO identity_reference_photos_origin_ref_idx;

COMMENT ON COLUMN identity_reference_photos.source IS
    'Enrollment origin: upload | from-tracks | auto-select | from-face-samples | immich | opus';
COMMENT ON COLUMN identity_reference_photos.origin_ref IS
    'The record in the external library this photo was imported from — an '
    'Immich asset uuid for source=immich, face:<id> for source=opus; NULL for '
    'local origins. Unique per identity so a re-import is idempotent.';

-- Which library person an identity stands for. Set when the identity is
-- created from the library or first enrolled from it; then "more references"
-- needs no picking, and the picker can say which people are already here.
-- One identity per person: a second identity for the same person is the
-- over-merge problem in reverse and is refused at the index.
ALTER TABLE identity_labels
    ADD COLUMN IF NOT EXISTS opus_person_id integer;
CREATE UNIQUE INDEX IF NOT EXISTS identity_labels_opus_person_idx
    ON identity_labels (opus_person_id)
    WHERE opus_person_id IS NOT NULL;
COMMENT ON COLUMN identity_labels.opus_person_id IS
    'OPUS Library photo_people.id this identity stands for; NULL when not linked.';
