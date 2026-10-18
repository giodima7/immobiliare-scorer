-- 018: how a listing was placed on the map (fetch_subito.py). Subito.it
-- private listings carry coordinates only ~half the time; the rest are
-- placed from their text: "map" (the site's own pin), "civico" / "near"
-- (a house number), "street" (a short street's centre), "metro" (the metro
-- station the listing names — ~330 m off, median). NULL for the other sites.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS geo_precision TEXT;
