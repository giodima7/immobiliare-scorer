-- ─────────────────────────────────────────────────────────────────────────
-- Migration 009 — Nuda proprietà + auction completeness
-- Adds excluded_reason + description columns, then retro-flags every
-- listing already in the table whose title or description mentions
-- "nuda proprietà" (or related usufrutto language) so the dashboard
-- hides them via the existing applySaleFilters check before the next
-- full scan reaches them.
-- ─────────────────────────────────────────────────────────────────────────

-- ── Step 1: schema additions (idempotent) ─────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS excluded_reason TEXT,
  ADD COLUMN IF NOT EXISTS description     TEXT;

-- The is_nuda_proprieta column was already added by an earlier migration
-- (007 / pre-008); the IF NOT EXISTS makes this safe to re-run.
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS is_nuda_proprieta BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS listings_nuda_idx
  ON listings(is_nuda_proprieta)
  WHERE is_nuda_proprieta = TRUE;


-- ── Step 2: retro-flag existing nuda proprietà listings ───────────────────
-- Title + description text-match. Same tokens fetch_listings.py /
-- fetch_idealista.py use, kept loose enough to catch the abbreviated
-- and usufrutto-equivalent variants real agents use.

UPDATE listings
SET is_nuda_proprieta = TRUE,
    excluded          = TRUE,
    excluded_reason   = 'Nuda proprietà — usufruct retained by seller',
    is_stale          = TRUE,
    last_seen_date    = NOW()::DATE
WHERE listing_type = 'sale'
  AND is_nuda_proprieta IS NOT TRUE
  AND (
       LOWER(COALESCE(title, ''))       LIKE '%nuda propriet%'
    OR LOWER(COALESCE(title, ''))       LIKE '%nuda prop%'
    OR LOWER(COALESCE(title, ''))       LIKE '%usufrutto%'
    OR LOWER(COALESCE(title, ''))       LIKE '%diritto di abitazione%'
    OR LOWER(COALESCE(description, '')) LIKE '%nuda propriet%'
    OR LOWER(COALESCE(description, '')) LIKE '%nuda prop%'
    OR LOWER(COALESCE(description, '')) LIKE '%usufrutto%'
    OR LOWER(COALESCE(description, '')) LIKE '%diritto di abitazione%'
  );


-- ── Step 3: backfill excluded_reason for previously-flagged nuda rows ─────
-- Rows that already had is_nuda_proprieta=TRUE from Idealista's parser
-- never picked up an excluded_reason; populate it now so the dashboard's
-- Data-Quality view has a consistent message.
UPDATE listings
SET excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason,
                              'Nuda proprietà — usufruct retained by seller'),
    is_stale        = TRUE,
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND is_nuda_proprieta = TRUE
  AND excluded IS NOT TRUE;


-- ── Step 4: same for the auction rows from migration 008 ─────────────────
-- migration 008 marked sub-floor sales as excluded without filling in
-- excluded_reason because the column didn't exist then. Backfill now.
UPDATE listings
SET excluded_reason = COALESCE(excluded_reason, price_floor_reason)
WHERE excluded = TRUE
  AND excluded_reason IS NULL
  AND price_floor_reason IS NOT NULL;

UPDATE listings
SET excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason,
                              'Auction listing (asta giudiziaria)'),
    is_stale        = TRUE,
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND is_auction = TRUE
  AND excluded IS NOT TRUE;


-- ── Step 5: verify ─────────────────────────────────────────────────────────
SELECT
  COUNT(*) FILTER (WHERE is_nuda_proprieta = TRUE) AS nuda_total,
  COUNT(*) FILTER (WHERE is_auction        = TRUE) AS auction_total,
  COUNT(*) FILTER (WHERE excluded          = TRUE) AS excluded_total
FROM listings
WHERE listing_type = 'sale';

SELECT city,
       COUNT(*) FILTER (WHERE is_nuda_proprieta = TRUE) AS nuda,
       COUNT(*) FILTER (WHERE is_auction        = TRUE) AS auctions,
       COUNT(*) FILTER (WHERE excluded          = TRUE) AS excluded
FROM listings
WHERE listing_type = 'sale'
GROUP BY city
ORDER BY city;
