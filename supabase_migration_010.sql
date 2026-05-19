-- ─────────────────────────────────────────────────────────────────────────
-- Migration 010 — Fake/foreign listings + extreme-underpricing flag
-- Adds the columns supabase_sync.py writes when the new fraud-detection
-- gates fire, then retro-flags existing rubbish in the table so the
-- dashboard hides them before the next full scan reaches them.
-- ─────────────────────────────────────────────────────────────────────────

-- ── Step 1: schema additions (idempotent) ─────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS is_fake                      BOOLEAN          DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS extreme_underpricing         BOOLEAN          DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS extreme_underpricing_delta   DOUBLE PRECISION;

-- Partial indexes — same pattern as is_auction / excluded. Keeps the
-- indexes tiny since the flagged rows are a minority.
CREATE INDEX IF NOT EXISTS listings_fake_idx
  ON listings(is_fake)
  WHERE is_fake = TRUE;
CREATE INDEX IF NOT EXISTS listings_extreme_underpricing_idx
  ON listings(extreme_underpricing)
  WHERE extreme_underpricing = TRUE;


-- ── Step 2: retro-flag existing fakes via title text search ──────────────
-- Catches the Albania / Dubai / Montenegro / Turkey / etc. bait
-- listings that slipped through before fetch_listings.py learned to
-- detect them. Sets is_fake + excluded + is_stale so the dashboard's
-- existing applySaleFilters check hides them immediately. excluded_reason
-- gives the Data-Quality panel a human-readable label.
UPDATE listings
SET is_fake         = TRUE,
    excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason,
                              'Fake / foreign-property bait listing'),
    is_stale        = TRUE,
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE is_fake IS NOT TRUE
  AND (
       LOWER(COALESCE(title, ''))       LIKE '%albania%'
    OR LOWER(COALESCE(title, ''))       LIKE '%tirana%'
    OR LOWER(COALESCE(title, ''))       LIKE '%durres%'
    OR LOWER(COALESCE(title, ''))       LIKE '%saranda%'
    OR LOWER(COALESCE(title, ''))       LIKE '%dubai%'
    OR LOWER(COALESCE(title, ''))       LIKE '%emirati arabi%'
    OR LOWER(COALESCE(title, ''))       LIKE '%montenegro%'
    OR LOWER(COALESCE(title, ''))       LIKE '%podgorica%'
    OR LOWER(COALESCE(title, ''))       LIKE '%turchia%'
    OR LOWER(COALESCE(title, ''))       LIKE '%istanbul%'
    OR LOWER(COALESCE(title, ''))       LIKE '%marocco%'
    OR LOWER(COALESCE(title, ''))       LIKE '%marrakech%'
    OR LOWER(COALESCE(title, ''))       LIKE '%shop like a billionaire%'
    OR LOWER(COALESCE(description, '')) LIKE '%shop like a billionaire%'
    OR LOWER(COALESCE(description, '')) LIKE '%invest abroad%'
    OR LOWER(COALESCE(description, '')) LIKE '%acquista all''estero%'
    OR LOWER(COALESCE(description, '')) LIKE '%investi all''estero%'
  );

-- Sea-view keywords ONLY for inland cities (Milan / Rome). Napoli +
-- La Maddalena legitimately use "vista mare" etc.
UPDATE listings
SET is_fake         = TRUE,
    excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason,
                              'Sea-view amenity impossible for inland city'),
    is_stale        = TRUE,
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE is_fake IS NOT TRUE
  AND city NOT IN ('napoli', 'la_maddalena')
  AND (
       LOWER(COALESCE(title, ''))       LIKE '%spiaggia privata%'
    OR LOWER(COALESCE(title, ''))       LIKE '%marina privata%'
    OR LOWER(COALESCE(title, ''))       LIKE '%vista mare%'
    OR LOWER(COALESCE(title, ''))       LIKE '%fronte mare%'
    OR LOWER(COALESCE(description, '')) LIKE '%vista mare%'
    OR LOWER(COALESCE(description, '')) LIKE '%spiaggia privata%'
    OR LOWER(COALESCE(description, '')) LIKE '%marina privata%'
    OR LOWER(COALESCE(description, '')) LIKE '%fronte mare%'
  );


-- ── Step 3: extreme underpricing (>60 % below comps) — cap the score ─────
-- These are NOT excluded (a genuine distressed sale at -65 % vs comps
-- can exist, e.g. urgent settlement) but their score is capped at 55
-- and gem badges stripped so they never headline the grid. The
-- dashboard renders a "verify this listing" banner when
-- extreme_underpricing = TRUE.
--
-- Sales: comps_sale_delta_pct lives in the listings table.
-- Rentals: comps_delta_pct ditto.
UPDATE listings
SET extreme_underpricing       = TRUE,
    extreme_underpricing_delta = comps_sale_delta_pct,
    hidden_gem                 = FALSE,
    good_value                 = FALSE,
    score_total                = LEAST(score_total, 55)
WHERE listing_type             = 'sale'
  AND extreme_underpricing IS NOT TRUE
  AND comps_sale_delta_pct < -60
  AND COALESCE(is_fake, FALSE)    = FALSE      -- don't double-process
  AND COALESCE(is_auction, FALSE) = FALSE
  AND COALESCE(is_nuda_proprieta, FALSE) = FALSE
  AND COALESCE(excluded, FALSE)   = FALSE;

UPDATE listings
SET extreme_underpricing       = TRUE,
    extreme_underpricing_delta = comps_delta_pct,
    hidden_gem                 = FALSE,
    good_value                 = FALSE,
    score_total                = LEAST(score_total, 55)
WHERE listing_type             = 'rental'
  AND extreme_underpricing IS NOT TRUE
  AND comps_delta_pct < -60
  AND COALESCE(is_fake, FALSE)    = FALSE
  AND COALESCE(is_auction, FALSE) = FALSE
  AND COALESCE(is_nuda_proprieta, FALSE) = FALSE
  AND COALESCE(excluded, FALSE)   = FALSE;


-- ── Step 4: verify ─────────────────────────────────────────────────────────
SELECT
  COUNT(*) FILTER (WHERE is_fake = TRUE)              AS fake_total,
  COUNT(*) FILTER (WHERE extreme_underpricing = TRUE) AS extreme_total,
  COUNT(*) FILTER (WHERE excluded = TRUE)             AS excluded_total
FROM listings;

SELECT city,
       COUNT(*) FILTER (WHERE is_fake = TRUE)              AS fakes,
       COUNT(*) FILTER (WHERE extreme_underpricing = TRUE) AS extremes
FROM listings
GROUP BY city
ORDER BY city;
