-- ─────────────────────────────────────────────────────────────────────────
-- Migration 011 — Retro-prune Hidden Gem / Great Value badges
-- The badge thresholds in scoring_settings.json got tightened on
-- 2026-05-20 after a Guastalla 1-bed (45 m², 10 min from Crocetta M1,
-- -18 % vs comps) was flagged as a Hidden Gem. Existing rows in Supabase
-- still carry the badges from the older, looser scoring, so the
-- dashboard keeps showing the bad gems until each row gets re-scored.
-- This sweeps the obvious offenders now so users don't have to wait.
--
-- New Hidden Gem gates (must satisfy ALL):
--   score_total          ≥ 80
--   ldi_score            ≥ 70
--   comps_delta_pct      ≤ -12
--   score_property       ≥ 62
--   score_location       ≥ 62
--   comps_confidence     ≥ 60
--   score_penalty        ≥ 75
--   metro_nearest_dist_m ≤ 800       (NEW — physical-walkability gate)
--   sqm / rooms          ≥ 22        (NEW — anti-cramped gate)
--
-- New Great Value gates (must satisfy ALL):
--   score_total          ≥ 70
--   ldi_score            ≥ 50
--   comps_delta_pct      ≤ -7
--   score_property       ≥ 50
--   score_location       ≥ 50
--   comps_confidence     ≥ 45
--   score_penalty        ≥ 60
-- ─────────────────────────────────────────────────────────────────────────

-- ── Hidden Gem: clear flag where the listing no longer qualifies ─────────
UPDATE listings
SET hidden_gem = FALSE
WHERE listing_type = 'rental'
  AND hidden_gem   = TRUE
  AND (
       COALESCE(score_total,      0) <  80
    OR COALESCE(ldi_score,        0) <  70
    OR comps_delta_pct IS NULL
    OR comps_delta_pct               > -12
    OR COALESCE(score_property, score_physical, 0) < 62
    OR COALESCE(score_location,   0) <  62
    OR COALESCE(comps_confidence, 0) <  60
    OR COALESCE(score_penalty,    0) <  75
    OR metro_nearest_dist_m IS NULL
    OR metro_nearest_dist_m          > 800
    OR (rooms IS NOT NULL AND rooms > 0 AND sqm IS NOT NULL
        AND (sqm::DOUBLE PRECISION / rooms) < 22)
  );

-- ── Great Value: clear flag where the listing no longer qualifies ────────
UPDATE listings
SET good_value = FALSE
WHERE listing_type = 'rental'
  AND good_value   = TRUE
  AND (
       COALESCE(score_total,      0) <  70
    OR COALESCE(ldi_score,        0) <  50
    OR comps_delta_pct IS NULL
    OR comps_delta_pct               > -7
    OR COALESCE(score_property, score_physical, 0) < 50
    OR COALESCE(score_location,   0) <  50
    OR COALESCE(comps_confidence, 0) <  45
    OR COALESCE(score_penalty,    0) <  60
  );

-- ── Verify ────────────────────────────────────────────────────────────────
SELECT
  COUNT(*) FILTER (WHERE hidden_gem = TRUE AND listing_type = 'rental')  AS rental_gems_remaining,
  COUNT(*) FILTER (WHERE good_value = TRUE AND listing_type = 'rental')  AS rental_gv_remaining
FROM listings;
