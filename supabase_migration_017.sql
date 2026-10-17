-- 017: the rest of a listing's monthly cost, read from listing pages
-- (listing_details.py): heating billed apart (Immobiliare's costs block —
-- "€ 2.400/anno" is €200 a month), whether the rent already covers the
-- condo fees, and the deposit. condominium_fees already exists.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS heating_mo      INTEGER,
  ADD COLUMN IF NOT EXISTS condo_included  BOOLEAN,
  ADD COLUMN IF NOT EXISTS deposit_months  SMALLINT,
  ADD COLUMN IF NOT EXISTS deposit_eur     INTEGER;
