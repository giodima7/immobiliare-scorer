-- 020: what Sonnet saw in a listing's photos, plan and description that the
-- listing doesn't state (photo_eval rubric v5, 2026-10-25): features
-- (bathrooms, balcony, air conditioning, kitchen type, separate living room,
-- floors, view…), up to 8 tags from a fixed list (big living room, old
-- windows, windowless bathroom, damp, plan not matching the photos…) and the
-- bedrooms it counted. Shown in Details ("Seen in the photos") and used by For you.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS photo_features JSONB,
  ADD COLUMN IF NOT EXISTS photo_tags     TEXT[],
  ADD COLUMN IF NOT EXISTS photo_bedrooms SMALLINT;
