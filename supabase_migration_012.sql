-- ─────────────────────────────────────────────────────────────────────────
-- Migration 012 — Misrepresented-address detection
-- A fresh fraud pattern surfaced 2026-05-20: agencies post a Milan title
-- but the actual property sits in an adjacent comune (Opera, Rozzano,
-- San Donato Milanese, etc.). Comps then read the listing as a bargain
-- on the Milan curve when it's normally priced for the real location.
-- Example: listing 127837188 — title "via Ripamonti, Carrobbio, Milano",
-- description says "Opera (MI) - In Via San Francesco d'Assisi".
--
-- Two new columns + retro-flagging of existing rows so the dashboard
-- updates the moment the migration applies, without waiting for a
-- full re-scan.
-- ─────────────────────────────────────────────────────────────────────────

-- ── Step 1: schema additions (idempotent) ─────────────────────────────────
ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS is_misrepresented_address BOOLEAN          DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_outside_city           BOOLEAN          DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS misrep_reason             TEXT;

CREATE INDEX IF NOT EXISTS listings_misrep_addr_idx
  ON listings(is_misrepresented_address)
  WHERE is_misrepresented_address = TRUE;
CREATE INDEX IF NOT EXISTS listings_outside_city_idx
  ON listings(is_outside_city)
  WHERE is_outside_city = TRUE;


-- ── Step 2: retro-flag Milan sales whose description mentions a nearby
--           comune with its (MI) province sticker. Lower(description)
--           is enough — agencies write the sticker explicitly when they
--           want the buyer to find it, which is the smoking gun.
UPDATE listings
SET is_misrepresented_address = TRUE,
    is_fake                   = TRUE,
    is_stale                  = TRUE,
    excluded                  = TRUE,
    excluded_reason           = COALESCE(excluded_reason, 'Misrepresented address — listed as Milan, located in adjacent comune'),
    misrep_reason             = COALESCE(misrep_reason,   'Description names an adjacent comune with (MI) sticker'),
    last_seen_date            = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND city         = 'milano'
  AND COALESCE(is_misrepresented_address, FALSE) = FALSE
  AND description IS NOT NULL
  AND (
       LOWER(description) LIKE '%opera (mi)%'
    OR LOWER(description) LIKE '%opera(mi)%'
    OR LOWER(description) LIKE '%opera, mi%'
    OR LOWER(description) LIKE '%rozzano (mi)%'
    OR LOWER(description) LIKE '%rozzano, mi%'
    OR LOWER(description) LIKE '%locate triulzi%'
    OR LOWER(description) LIKE '%locate di triulzi%'
    OR LOWER(description) LIKE '%pieve emanuele%'
    OR LOWER(description) LIKE '%san donato milanese%'
    OR LOWER(description) LIKE '%san giuliano milanese%'
    OR LOWER(description) LIKE '%segrate (mi)%'
    OR LOWER(description) LIKE '%segrate, mi%'
    OR LOWER(description) LIKE '%cologno monzese%'
    OR LOWER(description) LIKE '%sesto san giovanni%'
    OR LOWER(description) LIKE '%cinisello balsamo%'
    OR LOWER(description) LIKE '%cusano milanino%'
    OR LOWER(description) LIKE '%paderno dugnano%'
    OR LOWER(description) LIKE '%buccinasco%'
    OR LOWER(description) LIKE '%corsico%'
    OR LOWER(description) LIKE '%cesano boscone%'
    OR LOWER(description) LIKE '%trezzano sul naviglio%'
    OR LOWER(description) LIKE '%assago%'
    OR LOWER(description) LIKE '%pioltello%'
    OR LOWER(description) LIKE '%vimodrone%'
    OR LOWER(description) LIKE '%cernusco sul naviglio%'
    OR LOWER(description) LIKE '%settimo milanese%'
    OR LOWER(description) LIKE '%novate milanese%'
    OR LOWER(description) LIKE '%baranzate%'
  );


-- ── Step 3: distance-disclaimer phrase ("a soli X km da Milano").
--           Catches listings that don't use the (MI) sticker but admit
--           the property is a measurable distance from the claimed
--           address.
UPDATE listings
SET is_misrepresented_address = TRUE,
    is_fake                   = TRUE,
    is_stale                  = TRUE,
    excluded                  = TRUE,
    excluded_reason           = COALESCE(excluded_reason, 'Description admits property is several km from claimed location'),
    misrep_reason             = COALESCE(misrep_reason,   'Distance-disclaimer phrase in description'),
    last_seen_date            = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND city         = 'milano'
  AND COALESCE(is_misrepresented_address, FALSE) = FALSE
  AND description IS NOT NULL
  AND (
       LOWER(description) ~ 'a\s+(solo|soli)\s+\d+\s*km'
    OR LOWER(description) ~ 'a\s+\d+\s*km\s+(ca\.?|circa)\s+da'
  );


-- ── Step 4: coordinate-based proof. The Milan comune bounding box per
--           ISTAT — anything outside is definitively not in Milan.
--           Strongest signal of the three; runs last so listings already
--           flagged by Steps 2/3 don't get an empty UPDATE.
UPDATE listings
SET is_outside_city = TRUE,
    is_fake         = TRUE,
    is_stale        = TRUE,
    excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason, 'Coordinates outside Milan comune'),
    misrep_reason   = COALESCE(misrep_reason,   'Coordinates outside Milan comune'),
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND city         = 'milano'
  AND COALESCE(is_outside_city, FALSE) = FALSE
  AND latitude  IS NOT NULL
  AND longitude IS NOT NULL
  AND (
       latitude  < 45.388 OR latitude  > 45.535
    OR longitude <  9.065 OR longitude >  9.280
  );

-- Roma comune bbox (ISTAT)
UPDATE listings
SET is_outside_city = TRUE,
    is_fake         = TRUE,
    is_stale        = TRUE,
    excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason, 'Coordinates outside Rome comune'),
    misrep_reason   = COALESCE(misrep_reason,   'Coordinates outside Rome comune'),
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND city         = 'roma'
  AND COALESCE(is_outside_city, FALSE) = FALSE
  AND latitude  IS NOT NULL
  AND longitude IS NOT NULL
  AND (
       latitude  < 41.755 OR latitude  > 42.085
    OR longitude < 12.235 OR longitude > 12.730
  );

-- Napoli comune bbox (ISTAT)
UPDATE listings
SET is_outside_city = TRUE,
    is_fake         = TRUE,
    is_stale        = TRUE,
    excluded        = TRUE,
    excluded_reason = COALESCE(excluded_reason, 'Coordinates outside Naples comune'),
    misrep_reason   = COALESCE(misrep_reason,   'Coordinates outside Naples comune'),
    last_seen_date  = COALESCE(last_seen_date, NOW()::DATE)
WHERE listing_type = 'sale'
  AND city         = 'napoli'
  AND COALESCE(is_outside_city, FALSE) = FALSE
  AND latitude  IS NOT NULL
  AND longitude IS NOT NULL
  AND (
       latitude  < 40.782 OR latitude  > 40.920
    OR longitude < 14.140 OR longitude > 14.380
  );


-- ── Step 5: re-apply the tighter extreme-underpricing gate (now -40 %).
--           Migration 010 set the threshold at -60 %; that let
--           misrepresented-address listings at -54 % slip through with
--           a "Great Value" badge intact. Repeat the prune for both
--           sales and rentals.
UPDATE listings
SET extreme_underpricing       = TRUE,
    extreme_underpricing_delta = comps_sale_delta_pct,
    hidden_gem                 = FALSE,
    good_value                 = FALSE,
    score_total                = LEAST(score_total, 55)
WHERE listing_type           = 'sale'
  AND COALESCE(extreme_underpricing, FALSE) = FALSE
  AND comps_sale_delta_pct < -40
  AND COALESCE(is_fake,            FALSE) = FALSE
  AND COALESCE(is_auction,         FALSE) = FALSE
  AND COALESCE(is_nuda_proprieta,  FALSE) = FALSE
  AND COALESCE(excluded,           FALSE) = FALSE;

UPDATE listings
SET extreme_underpricing       = TRUE,
    extreme_underpricing_delta = comps_delta_pct,
    hidden_gem                 = FALSE,
    good_value                 = FALSE,
    score_total                = LEAST(score_total, 55)
WHERE listing_type           = 'rental'
  AND COALESCE(extreme_underpricing, FALSE) = FALSE
  AND comps_delta_pct < -40
  AND COALESCE(is_fake,            FALSE) = FALSE
  AND COALESCE(is_auction,         FALSE) = FALSE
  AND COALESCE(is_nuda_proprieta,  FALSE) = FALSE
  AND COALESCE(excluded,           FALSE) = FALSE;


-- ── Step 6: verify ────────────────────────────────────────────────────────
SELECT
  COUNT(*) FILTER (WHERE is_misrepresented_address = TRUE) AS misrep_total,
  COUNT(*) FILTER (WHERE is_outside_city           = TRUE) AS outside_total,
  COUNT(*) FILTER (WHERE extreme_underpricing      = TRUE) AS extreme_total,
  COUNT(*) FILTER (WHERE is_fake                   = TRUE) AS fake_total,
  COUNT(*) FILTER (WHERE excluded                  = TRUE) AS excluded_total
FROM listings;

SELECT city, listing_type,
       COUNT(*) FILTER (WHERE is_misrepresented_address = TRUE) AS misrep,
       COUNT(*) FILTER (WHERE is_outside_city           = TRUE) AS outside,
       COUNT(*) FILTER (WHERE extreme_underpricing      = TRUE) AS extremes
FROM listings
WHERE listing_type = 'sale'
GROUP BY city, listing_type
ORDER BY city;
