-- 019: the same flat listed more than once — on two sites or by two
-- agencies (scoring → listing_details.stamp_dup_groups: comps_engine's
-- duplicate groups, same rent / m² / rooms / floor within 300 m). The key is
-- the lowest listing id of the group; the dashboard shows one card per group
-- with links to the other copies. NULL when the flat is listed once.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS dup_group TEXT;

-- The photo reading's 1–10 grade (photo_eval rubric v2) and the user's own
-- verdict on the photos (photo_feedback_milano.json), which wins over Sonnet's.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS photo_grade      REAL,
  ADD COLUMN IF NOT EXISTS photo_by_user    BOOLEAN,
  ADD COLUMN IF NOT EXISTS photo_user_note  TEXT,
  ADD COLUMN IF NOT EXISTS photo_sonnet_deal TEXT;
