-- Migration 023 — the nearby range for sales (2026-10-30)
--
-- The Details price scale now draws the nearby flats' middle (p40–p60) as
-- its band, with OMI as a dashed line. Rentals have comps_p40 / comps_p60
-- since migration 014; sales never had theirs, so online every sale showed
-- only the median tick. supabase_sync reads the live columns, so the next
-- push fills them (each changed row's hash differs once the columns exist).
-- Run in the Supabase SQL editor. Idempotent.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS comps_sale_p40 NUMERIC,
  ADD COLUMN IF NOT EXISTS comps_sale_p60 NUMERIC;

NOTIFY pgrst, 'reload schema';
