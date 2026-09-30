-- Operator-curated cover photo for an identity. Overrides the
-- auto-picked latest_crop in the detail/list endpoints. NULL = fall
-- back to the default "most recent track's crop_path" heuristic.
--
-- The path is stored as a media-root-relative string (e.g.
-- "crops/<sample_id>.jpg" or "reference_photos/<photo_id>.jpg") with
-- no FK on either source table. Operator may set it to any visible
-- crop in the gallery; if the underlying file is later deleted (track
-- pruned, reference photo removed), the detail endpoint just falls
-- back to the auto-pick — better than dragging in delete-cascade
-- semantics across two unrelated tables.
ALTER TABLE identity_labels
  ADD COLUMN IF NOT EXISTS cover_photo_path TEXT;
