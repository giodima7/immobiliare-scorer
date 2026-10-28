-- Migration 022 — index for the dashboard's main fetch (2026-10-28)
--
-- The online dashboard loads rentals 1,000 at a time:
--   city = 'milano' AND listing_type = 'rental' AND is_stale = false
--   ORDER BY score_total DESC, id ASC  OFFSET n LIMIT 1000
-- Pages took 5–8 s and several in parallel hit the 8 s statement timeout,
-- so the site showed ~1,800 of ~10,800 rentals. Migration 007's index has no
-- id, so every page sorts the score ties again; this one matches the order.
-- Run in the Supabase SQL editor.

CREATE INDEX IF NOT EXISTS idx_listings_city_type_score_id
  ON listings (city, listing_type, score_total DESC, id)
  WHERE is_stale = false;

ANALYZE listings;
