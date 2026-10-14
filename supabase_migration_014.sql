-- Migration 014: fields behind the 2026-10-14 scoring/dashboard changes
--
--   price_z            discount in units of its uncertainty (drives labels)
--   price_suspect      discount beyond the label floor → neutral price score
--   comps_p40/p60      spread of the nearby flats (the dashboard's range)
--   comps_radius_m     how far the comparable flats reach
--   score_cap_reason   why a score was capped (breakdown no longer "doesn't add up")
--   twin_filled        fields copied from the same flat on the other site
--   detail_fetched_at  listing page read (listing_details.py)
--   gone_checked       listing page said "no longer available"
--   contract_type      4+4 / 3+2 / transitorio / studenti (Immobiliare page)
--   availability       e.g. "libero" (Immobiliare page)
--   air_conditioning   Idealista page
--
-- supabase_sync reads the live columns, so pushes work before and after
-- this runs; the fields simply start syncing once it has. Idempotent.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS price_z           NUMERIC,
  ADD COLUMN IF NOT EXISTS price_suspect     BOOLEAN,
  ADD COLUMN IF NOT EXISTS comps_p40         NUMERIC,
  ADD COLUMN IF NOT EXISTS comps_p60         NUMERIC,
  ADD COLUMN IF NOT EXISTS comps_radius_m    NUMERIC,
  ADD COLUMN IF NOT EXISTS score_cap_reason  TEXT,
  ADD COLUMN IF NOT EXISTS twin_filled       TEXT[],
  ADD COLUMN IF NOT EXISTS detail_fetched_at DATE,
  ADD COLUMN IF NOT EXISTS gone_checked      DATE,
  ADD COLUMN IF NOT EXISTS contract_type     TEXT,
  ADD COLUMN IF NOT EXISTS availability      TEXT,
  ADD COLUMN IF NOT EXISTS air_conditioning  BOOLEAN;

-- PostgREST caches the schema; reload so the new columns are writable now.
NOTIFY pgrst, 'reload schema';
