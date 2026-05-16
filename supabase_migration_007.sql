-- Migration 007: multi-city support
--
-- 1. Adds `city` column to listings (default 'milano' so existing rows
--    don't break).
-- 2. Rebuilds the active query indexes with city as the leading column.
-- 3. New `cities` table — per-city metadata (centre, bbox, appreciation
--    rates, search-term mapping for the scrapers) consumed by both the
--    dashboard (default map view, currency, metro filter visibility) and
--    the Python scoring/fetch pipeline.
-- 4. Seeds 4 cities: Milano, Roma, Napoli, La Maddalena.
-- 5. Extends digest_filters with a `cities TEXT[]` column so users can
--    subscribe to alerts that span multiple cities.
--
-- Every statement is idempotent (IF NOT EXISTS / ON CONFLICT). Safe to
-- re-run.

-- ── Step 1: add city column ─────────────────────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT 'milano';

UPDATE listings SET city = 'milano' WHERE city IS NULL OR city = '';


-- ── Step 2: rebuild query indexes around city ──────────────────────────────
-- Primary query pattern: city + type + score (the dashboard's main fetch)
CREATE INDEX IF NOT EXISTS idx_listings_city_type_score
  ON listings(city, listing_type, score_total DESC)
  WHERE is_stale = false;

-- City + zone (comps engine)
CREATE INDEX IF NOT EXISTS idx_listings_city_zona
  ON listings(city, omi_zona)
  WHERE is_stale = false;

-- City + gem (filtered listing query)
CREATE INDEX IF NOT EXISTS idx_listings_city_gem
  ON listings(city, hidden_gem)
  WHERE hidden_gem = true AND is_stale = false;

-- City + first_seen_date (digest "new today" query)
CREATE INDEX IF NOT EXISTS idx_listings_city_new
  ON listings(city, first_seen_date)
  WHERE is_stale = false;

-- Drop the old non-city indexes — superseded.
DROP INDEX IF EXISTS idx_listings_type_score;
DROP INDEX IF EXISTS idx_listings_gem;
DROP INDEX IF EXISTS idx_listings_new_today;


-- ── Step 3: cities config table ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cities (
  code              TEXT PRIMARY KEY,
  comune_code       TEXT NOT NULL,
  display_name_it   TEXT NOT NULL,
  display_name_en   TEXT NOT NULL,
  province          TEXT NOT NULL,
  country           TEXT NOT NULL DEFAULT 'IT',
  active            BOOLEAN NOT NULL DEFAULT true,
  scan_enabled      BOOLEAN NOT NULL DEFAULT true,
  center_lat        NUMERIC(10,7) NOT NULL,
  center_lng        NUMERIC(10,7) NOT NULL,
  default_zoom      INTEGER NOT NULL DEFAULT 13,
  bbox_south        NUMERIC(10,7) NOT NULL,
  bbox_west         NUMERIC(10,7) NOT NULL,
  bbox_north        NUMERIC(10,7) NOT NULL,
  bbox_east         NUMERIC(10,7) NOT NULL,
  immobiliare_city  TEXT NOT NULL,
  idealista_city    TEXT NOT NULL,
  has_metro         BOOLEAN NOT NULL DEFAULT true,
  metro_system_name TEXT,
  currency          TEXT NOT NULL DEFAULT 'EUR',
  appr_rate_a       NUMERIC(5,4) DEFAULT 0.030,
  appr_rate_b       NUMERIC(5,4) DEFAULT 0.025,
  appr_rate_c       NUMERIC(5,4) DEFAULT 0.020,
  appr_rate_d       NUMERIC(5,4) DEFAULT 0.015,
  appr_rate_e       NUMERIC(5,4) DEFAULT 0.010,
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Reuse the update_updated_at() function defined in migration 001.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cities_updated_at'
  ) THEN
    CREATE TRIGGER trg_cities_updated_at
      BEFORE UPDATE ON cities
      FOR EACH ROW EXECUTE FUNCTION update_updated_at();
  END IF;
END $$;


-- ── Step 4: seed cities ────────────────────────────────────────────────────
INSERT INTO cities (
  code, comune_code, display_name_it, display_name_en, province,
  center_lat, center_lng, default_zoom,
  bbox_south, bbox_west, bbox_north, bbox_east,
  immobiliare_city, idealista_city,
  has_metro, metro_system_name,
  appr_rate_a, appr_rate_b, appr_rate_c, appr_rate_d, appr_rate_e
) VALUES
  ('milano',       'F205', 'Milano',       'Milan',        'MI',
   45.4642,  9.1900, 13,  45.38,  9.04, 45.54,  9.28,
   'Milano', 'milano',
   true,  'Metro Milano',
   0.030, 0.025, 0.020, 0.015, 0.010),

  ('roma',         'H501', 'Roma',         'Rome',         'RM',
   41.9028, 12.4964, 12, 41.75, 12.30, 42.00, 12.70,
   'Roma',   'roma',
   true,  'Metro Roma',
   0.025, 0.020, 0.015, 0.010, 0.008),

  ('napoli',       'F839', 'Napoli',       'Naples',       'NA',
   40.8518, 14.2681, 13, 40.78, 14.14, 40.92, 14.38,
   'Napoli', 'napoli',
   true,  'Metro Napoli',
   0.020, 0.015, 0.012, 0.010, 0.008),

  ('la_maddalena', 'E425', 'La Maddalena', 'La Maddalena', 'SS',
   41.2133,  9.4063, 14, 41.17,  9.35, 41.25,  9.47,
   'La Maddalena', 'la-maddalena',
   false, NULL,
   0.015, 0.010, 0.008, 0.005, 0.005)

ON CONFLICT (code) DO UPDATE SET
  display_name_it = EXCLUDED.display_name_it,
  display_name_en = EXCLUDED.display_name_en,
  center_lat      = EXCLUDED.center_lat,
  center_lng      = EXCLUDED.center_lng,
  bbox_south      = EXCLUDED.bbox_south,
  bbox_west       = EXCLUDED.bbox_west,
  bbox_north      = EXCLUDED.bbox_north,
  bbox_east       = EXCLUDED.bbox_east,
  appr_rate_a     = EXCLUDED.appr_rate_a,
  appr_rate_b     = EXCLUDED.appr_rate_b,
  appr_rate_c     = EXCLUDED.appr_rate_c,
  appr_rate_d     = EXCLUDED.appr_rate_d,
  appr_rate_e     = EXCLUDED.appr_rate_e,
  updated_at      = NOW();


-- ── Step 5: digest_filters.cities ──────────────────────────────────────────
-- Multi-city alert subscriptions. Default keeps existing single-city rows
-- working (they continue to behave as Milan-only).
ALTER TABLE digest_filters
  ADD COLUMN IF NOT EXISTS cities TEXT[] DEFAULT ARRAY['milano'];


-- ── Step 6: RLS on cities ──────────────────────────────────────────────────
ALTER TABLE cities ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'cities' AND policyname = 'Public read cities') THEN
    CREATE POLICY "Public read cities"
      ON cities FOR SELECT TO anon USING (true);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_policies
                 WHERE tablename = 'cities' AND policyname = 'Service role full access on cities') THEN
    CREATE POLICY "Service role full access on cities"
      ON cities FOR ALL TO service_role
      USING (true) WITH CHECK (true);
  END IF;
END $$;


-- ── Step 7: verify ─────────────────────────────────────────────────────────
SELECT code, display_name_en, comune_code, center_lat, center_lng,
       appr_rate_b AS fascia_b_rate
FROM cities ORDER BY code;

SELECT city, COUNT(*) AS listings
FROM listings
GROUP BY city
ORDER BY listings DESC;
