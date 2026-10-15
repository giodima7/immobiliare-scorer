-- Migration 015: relistings — the same flat listed before at another rent
--
--   relisted_from_rent  rent of the earlier, now archived listing of the same
--                       flat (same rooms/size/floor/building/street,
--                       comps_engine._same_flat)
--   relisted_from_date  when that listing was last seen
--   relisted_from_id    its listing id
--
-- (Price drops on the same listing reuse the existing previous_price /
-- price_changed_date columns; the scanners now record and keep them.)
-- supabase_sync reads the live columns, so pushes work before and after
-- this runs. Idempotent.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS relisted_from_rent NUMERIC,
  ADD COLUMN IF NOT EXISTS relisted_from_date DATE,
  ADD COLUMN IF NOT EXISTS relisted_from_id   TEXT;

NOTIFY pgrst, 'reload schema';
