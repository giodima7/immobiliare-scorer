-- ─────────────────────────────────────────────────────────────────────────
-- Migration 008 — Sanity gates for the sales feed
-- Adds the columns supabase_sync.py writes when scoring rejects a listing
-- below the per-city minimum €/m², plus a one-shot cleanup pass that marks
-- the rubbish already in the table as stale so the dashboard hides them
-- before the next full scan.
-- ─────────────────────────────────────────────────────────────────────────

-- ── Step 1: schema additions (idempotent) ─────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS excluded                  BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS price_floor_gate_applied  BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS price_floor_reason        TEXT;

-- Index so the dashboard's "WHERE NOT excluded" filter is index-only.
-- Partial index keeps the index tiny (only the rare excluded rows live in
-- it, while every other listing query bypasses it entirely).
CREATE INDEX IF NOT EXISTS listings_excluded_idx
  ON listings(excluded)
  WHERE excluded = TRUE;


-- ── Step 2: cleanup — mark sub-floor sales as excluded ────────────────────
-- Same per-city floors scoring.py and fetch_listings.py use:
--   milano       < €800/m²  → almost certainly an auction or data error
--   roma         < €600/m²  → "
--   napoli       < €400/m²  → "
--   la_maddalena < €800/m²  → tourist market, no floor sales
-- Also catches the €30/m² mislabelled-rental case (Naples ID 128655148).

UPDATE listings
SET excluded                 = TRUE,
    price_floor_gate_applied = TRUE,
    price_floor_reason       = '€' || ROUND(ask_psqm)::text
                              || '/m² below minimum €800/m² for milano',
    is_stale                 = TRUE,
    last_seen_date           = NOW()::DATE
WHERE listing_type = 'sale'
  AND city         = 'milano'
  AND ask_psqm IS NOT NULL
  AND ask_psqm < 800
  AND excluded IS NOT TRUE;

UPDATE listings
SET excluded                 = TRUE,
    price_floor_gate_applied = TRUE,
    price_floor_reason       = '€' || ROUND(ask_psqm)::text
                              || '/m² below minimum €600/m² for roma',
    is_stale                 = TRUE,
    last_seen_date           = NOW()::DATE
WHERE listing_type = 'sale'
  AND city         = 'roma'
  AND ask_psqm IS NOT NULL
  AND ask_psqm < 600
  AND excluded IS NOT TRUE;

UPDATE listings
SET excluded                 = TRUE,
    price_floor_gate_applied = TRUE,
    price_floor_reason       = '€' || ROUND(ask_psqm)::text
                              || '/m² below minimum €400/m² for napoli',
    is_stale                 = TRUE,
    last_seen_date           = NOW()::DATE
WHERE listing_type = 'sale'
  AND city         = 'napoli'
  AND ask_psqm IS NOT NULL
  AND ask_psqm < 400
  AND excluded IS NOT TRUE;

UPDATE listings
SET excluded                 = TRUE,
    price_floor_gate_applied = TRUE,
    price_floor_reason       = '€' || ROUND(ask_psqm)::text
                              || '/m² below minimum €800/m² for la_maddalena',
    is_stale                 = TRUE,
    last_seen_date           = NOW()::DATE
WHERE listing_type = 'sale'
  AND city         = 'la_maddalena'
  AND ask_psqm IS NOT NULL
  AND ask_psqm < 800
  AND excluded IS NOT TRUE;


-- ── Step 3: verify ─────────────────────────────────────────────────────────
SELECT city,
       COUNT(*)                                              AS total_excluded,
       MIN(ask_psqm)::numeric(10,2)                          AS min_psqm,
       MAX(ask_psqm)::numeric(10,2)                          AS max_psqm
FROM listings
WHERE listing_type = 'sale' AND excluded = TRUE
GROUP BY city
ORDER BY city;
