#!/usr/bin/env python3
"""
slim_json.py
─────────────
Strips fields the dashboard doesn't read from the listings JSONs and
re-writes them as compact JSON (no whitespace).

Cuts rentals_latest.json from ~30MB to ~16MB so it fits comfortably
under Cloudflare Pages' 25MB per-file cap.

Run after scoring, before the git commit step in daily_scan.yml:
    python3 slim_json.py

Idempotent — safe to re-run on already-slimmed files.
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DASH_DIR = BASE_DIR / "dashboard"

# Fields the dashboard reads. Anything else gets dropped.
# Underscore-prefixed flags are kept because card render functions read
# them directly (e.g. _room_efficiency_flag).
KEEP_FIELDS: frozenset[str] = frozenset({
    # Identity
    "id", "source",
    # Display
    "title", "address", "neighbourhood", "url", "thumbnail", "photos",
    # Price
    "price", "ask_psqm", "ask_psqm_rent", "ask_psqm_effective",
    "rent_mo", "effective_rent_mo",
    # Physical
    "sqm", "rooms", "bedrooms", "floor", "floor_n", "floor_label",
    "elevator", "has_balcony", "has_parking", "furnished", "condition",
    "year_built", "energy_class", "bathrooms",
    "spese_condominiali", "condominium_fees",
    "heating_type", "is_external", "is_below_ground", "is_ground_floor",
    "is_auction", "is_nuda_proprieta",
    # Location
    "latitude", "longitude", "city", "city_key",
    "omi_zona", "omi_fascia", "omi_descr",
    "metro_walk_min", "metro_nearest_name", "metro_nearest_line",
    "metro_nearest_dist_m", "metro_walk_routed",
    "park_nearest_dist_m", "supermarket_nearest_dist_m",
    "university_nearest_dist_m", "tram_nearest_dist_m",
    "geo_score", "score_geo",
    # OMI benchmarks
    "omi_compr_mid", "omi_compr_min", "omi_compr_max",
    "omi_loc_mid", "omi_loc_min", "omi_loc_max",
    "omi_rmin", "omi_rmax",
    "omi_source", "omi_fallback",
    "vs_omi_label", "vs_omi_pct", "vs_omi_rent_pct",
    # Scoring
    "score_total", "score_price", "score_property",
    "score_location", "score_penalty", "score_physical", "score_rent",
    "score_value", "score_fascia", "score_reasons", "score_was_capped",
    "ldi_score", "ldi_bonus",
    "comps_delta_pct", "comps_n", "comps_median", "comps_p40", "comps_p60",
    "comps_confidence", "comps_conf_label", "comps_source", "comps_label",
    "comps_radius_m", "comps_condition_group", "comps_adjusted",
    "comps_sale_median", "comps_sale_p40", "comps_sale_p60",
    "comps_sale_n", "comps_sale_radius_m", "comps_sale_source",
    "comps_sale_confidence", "comps_sale_conf_label",
    "comps_sale_delta_pct", "comps_sale_label",
    "comps_sale_condition_group", "comps_sale_adjusted",
    "comps_sale_comp_ids", "comps_ids",
    "hidden_gem", "good_value",
    "boosted_price_score", "is_corporate_rental",
    "suggested_rent_mo", "suggested_rent_psqm",
    # Internal flags the dashboard reads despite leading underscore
    "_room_efficiency_flag", "_absolute_value_gate_applied",
    # Investment / sales
    "estimated_rent_mo", "estimated_rent_psqm",
    "estimated_rent_n_comps", "estimated_rent_confidence",
    "estimated_rent_method", "estimated_rent_comp_ids",
    "estimated_yield_pct",
    # Staleness
    "first_seen_date", "last_seen_date",
    "is_stale", "days_since_seen", "days_on_market",
    "fetched_at", "published_date",
    # Agency
    "agency_id", "agency_name", "agency_type", "agency_url",
    # Photo metadata
    "photo_count",
    # Area tracking (used by area-stale-removal logic)
    "_fetched_area",
})

FILES = [DASH_DIR / "rentals_latest.json", DASH_DIR / "sales_latest.json"]


def slim_one(path: Path) -> tuple[float, float, int, int]:
    """Slim one file. Returns (before_mb, after_mb, fields_before, fields_after)."""
    if not path.exists():
        return 0.0, 0.0, 0, 0

    before_mb = path.stat().st_size / 1024 / 1024
    data      = json.loads(path.read_text())

    fields_before = sum(len(l) for l in data) if data else 0
    slimmed       = [{k: v for k, v in l.items() if k in KEEP_FIELDS} for l in data]
    fields_after  = sum(len(l) for l in slimmed) if slimmed else 0

    # Compact JSON — no whitespace. ~5-10% smaller than indented form.
    path.write_text(
        json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
    )
    after_mb = path.stat().st_size / 1024 / 1024
    return before_mb, after_mb, fields_before, fields_after


def main() -> int:
    print("[slim_json] Slimming dashboard JSONs for Cloudflare Pages")
    total_saved = 0.0
    for path in FILES:
        if not path.exists():
            print(f"  {path.name}: not found, skipping")
            continue
        before, after, fb, fa = slim_one(path)
        saved = before - after
        total_saved += saved
        pct = (1 - after / before) * 100 if before else 0
        print(f"  {path.name}: {before:5.1f} MB → {after:5.1f} MB "
              f"(-{pct:.0f}% · -{saved:.1f} MB)  "
              f"fields {fb:,} → {fa:,}")
    print(f"[slim_json] Total saved: {total_saved:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
