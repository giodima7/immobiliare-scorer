-- Migration 002: widen `listings` to cover every dashboard-read field.
--
-- The initial schema (in SUPABASE_SETUP.md) only included ~65 columns —
-- the dashboard reads ~35 more (agency leaderboard, photo gallery, score
-- breakdown bullets, sales-side comps, investor estimate metadata, the
-- nuda-proprietà / auction flags, etc.). Without these columns the sync
-- silently drops those fields, so listings render with blank tooltips
-- and missing panels.
--
-- Every statement uses `IF NOT EXISTS` / `IF EXISTS` so this is safe to
-- re-run. Paste into Supabase → SQL editor → Run.

ALTER TABLE listings
  -- Display
  ADD COLUMN IF NOT EXISTS city                  TEXT,
  ADD COLUMN IF NOT EXISTS photos                TEXT[],
  ADD COLUMN IF NOT EXISTS photo_count           INTEGER,

  -- Physical
  ADD COLUMN IF NOT EXISTS bedrooms              INTEGER,
  ADD COLUMN IF NOT EXISTS floor                 TEXT,
  ADD COLUMN IF NOT EXISTS is_auction            BOOLEAN,
  ADD COLUMN IF NOT EXISTS is_nuda_proprieta     BOOLEAN,

  -- Location / proximity
  ADD COLUMN IF NOT EXISTS geo_score             INTEGER,

  -- OMI
  ADD COLUMN IF NOT EXISTS vs_omi_label          TEXT,

  -- Scoring
  ADD COLUMN IF NOT EXISTS score_physical        INTEGER,
  ADD COLUMN IF NOT EXISTS score_geo             INTEGER,
  ADD COLUMN IF NOT EXISTS score_reasons         TEXT[],
  ADD COLUMN IF NOT EXISTS score_was_capped      BOOLEAN,
  ADD COLUMN IF NOT EXISTS ldi_bonus             NUMERIC(6,2),

  -- Rental comps annotations
  ADD COLUMN IF NOT EXISTS comps_conf_label      TEXT,
  ADD COLUMN IF NOT EXISTS comps_label           TEXT,
  ADD COLUMN IF NOT EXISTS comps_adjusted        BOOLEAN,

  -- Sales comps (parallel set, only populated on sale listings)
  ADD COLUMN IF NOT EXISTS comps_sale_median     NUMERIC(10,2),
  ADD COLUMN IF NOT EXISTS comps_sale_n          INTEGER,
  ADD COLUMN IF NOT EXISTS comps_sale_source     TEXT,
  ADD COLUMN IF NOT EXISTS comps_sale_confidence INTEGER,
  ADD COLUMN IF NOT EXISTS comps_sale_conf_label TEXT,
  ADD COLUMN IF NOT EXISTS comps_sale_delta_pct  NUMERIC(8,2),
  ADD COLUMN IF NOT EXISTS comps_sale_label      TEXT,
  ADD COLUMN IF NOT EXISTS comps_sale_adjusted   BOOLEAN,
  ADD COLUMN IF NOT EXISTS comps_sale_comp_ids   TEXT[],

  -- Investor estimate metadata (sales)
  ADD COLUMN IF NOT EXISTS estimated_rent_n_comps    INTEGER,
  ADD COLUMN IF NOT EXISTS estimated_rent_confidence INTEGER,
  ADD COLUMN IF NOT EXISTS estimated_rent_method     TEXT,
  ADD COLUMN IF NOT EXISTS estimated_rent_comp_ids   TEXT[],

  -- Agency (Stats-tab leaderboard + agency-pin filter)
  ADD COLUMN IF NOT EXISTS agency_id              TEXT,
  ADD COLUMN IF NOT EXISTS agency_name            TEXT,
  ADD COLUMN IF NOT EXISTS agency_type            TEXT,
  ADD COLUMN IF NOT EXISTS agency_url             TEXT,

  -- Lifecycle
  ADD COLUMN IF NOT EXISTS published_date         TEXT;

-- Trigram index on agency_name for the leaderboard's fuzzy search.
CREATE INDEX IF NOT EXISTS idx_listings_agency_name_trgm
  ON listings USING gin(agency_name gin_trgm_ops);

-- The "city" column joins the existing geo columns. Useful for the
-- city-filter dropdown that currently silently filters off undefined.
CREATE INDEX IF NOT EXISTS idx_listings_city ON listings(city);
