#!/usr/bin/env python3
"""
supabase_sync.py
─────────────────
Upserts the slimmed dashboard JSONs into a Supabase `listings` table.
Runs alongside the JSON commit in daily_scan.yml — both stay in sync.

Usage:
    python3 supabase_sync.py --type rental   # only rentals_latest.json
    python3 supabase_sync.py --type sale     # only sales_latest.json
    python3 supabase_sync.py                 # both

Env vars (set in GitHub Actions secrets):
    SUPABASE_URL          — project URL, e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  — service_role key (write access)

If either is missing the script exits 0 — never block the workflow.

The CI step wrapping this should also have `continue-on-error: true`
so that any Supabase outage cannot break the daily scan.

Dependencies: standard library only (urllib + json). No `supabase-py`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

BASE_DIR = Path(__file__).parent
DASH_DIR = BASE_DIR / "dashboard"

# Renames from listing-dict key → DB column. Underscore-prefixed source
# fields can't survive a Postgres column name (those would clash with
# system identifiers), so we strip the prefix on the way in.
FIELD_RENAME: dict[str, str] = {
    "_room_efficiency_flag":        "room_efficiency_flag",
    "_absolute_value_gate_applied": "absolute_value_gate_applied",
}

# Columns the Supabase schema actually has (per the prompt's CREATE TABLE).
# Anything else gets dropped on the way in so Supabase doesn't reject the
# upsert with "column does not exist".
SCHEMA_COLUMNS: frozenset[str] = frozenset({
    "id", "source", "listing_type",
    "title", "address", "neighbourhood", "url", "thumbnail",
    "price", "ask_psqm", "ask_psqm_rent",
    "sqm", "rooms", "floor_n", "floor_label", "elevator",
    "has_balcony", "has_parking", "furnished", "condition",
    "year_built", "energy_class", "bathrooms", "condominium_fees",
    "heating_type", "is_external", "is_below_ground", "is_ground_floor",
    "latitude", "longitude",
    "omi_zona", "omi_fascia", "omi_descr",
    "metro_walk_min", "metro_nearest_name", "metro_nearest_line",
    "metro_nearest_dist_m",
    "park_nearest_dist_m", "supermarket_nearest_dist_m",
    "university_nearest_dist_m", "tram_nearest_dist_m",
    "omi_compr_mid", "omi_compr_min", "omi_compr_max",
    "omi_loc_mid",   "omi_loc_min",   "omi_loc_max",
    "omi_source", "omi_fallback",
    "score_total", "score_price", "score_property",
    "score_location", "score_penalty",
    "ldi_score", "comps_delta_pct", "comps_n", "comps_median",
    "comps_confidence", "comps_source", "comps_ids",
    "hidden_gem", "good_value",
    "vs_omi_pct", "boosted_price_score", "is_corporate_rental",
    "suggested_rent_mo", "suggested_rent_psqm",
    "room_efficiency_flag", "absolute_value_gate_applied",
    "estimated_rent_mo", "estimated_rent_psqm", "estimated_yield_pct",
    "first_seen_date", "last_seen_date",
    "is_stale", "days_since_seen", "days_on_market",
})

# Postgres rejects empty strings for INTEGER / NUMERIC columns. Treat the
# usual JSON sentinels (empty string / list / dict) as NULL on the wire.
def _normalise(v):
    if v == "" or v == [] or v == {}:
        return None
    return v


def listing_to_row(listing: dict, listing_type: str) -> dict:
    """Map a listing dict to a Supabase row, dropping unknown columns."""
    row: dict = {"listing_type": listing_type}
    for k, v in listing.items():
        col = FIELD_RENAME.get(k, k)
        if col not in SCHEMA_COLUMNS:
            continue
        row[col] = _normalise(v)
    return row


def _http_post(url: str, body_bytes: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as exc:
        return 0, str(exc)


def upsert_batch(rows: list[dict], idx: int, total: int) -> bool:
    """POST a batch to /rest/v1/listings with merge-duplicates semantics."""
    headers = {
        "Content-Type":  "application/json",
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    url  = f"{SUPABASE_URL}/rest/v1/listings"
    body = json.dumps(rows).encode("utf-8")
    code, msg = _http_post(url, body, headers)
    ok = code in (200, 201, 204)
    if ok:
        print(f"  batch {idx:3d}/{total}: {len(rows):3d} rows ✓")
    else:
        print(f"  batch {idx:3d}/{total}: FAILED (HTTP {code}) — {msg[:200]}")
    return ok


def sync_file(path: Path, listing_type: str, batch_size: int = 200) -> bool:
    if not path.exists():
        print(f"[supabase_sync] {path.name} not found — skipping")
        return True

    with open(path) as fh:
        listings = json.load(fh)

    rows    = [listing_to_row(l, listing_type) for l in listings if l.get("id")]
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    print(f"[supabase_sync] {listing_type}s: {len(rows)} rows in {len(batches)} batches")

    failed = 0
    for i, b in enumerate(batches, 1):
        if not upsert_batch(b, i, len(batches)):
            failed += 1
        time.sleep(0.1)   # gentle rate limit — Supabase free tier is fine
    print(f"[supabase_sync] {listing_type}s done: {len(batches) - failed}/{len(batches)} batches succeeded")
    return failed == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["rental", "sale"],
                        help="Which listing type to sync (default: both)")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[supabase_sync] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping (this is fine)")
        return 0

    t0 = time.time()
    if args.type in (None, "rental"):
        sync_file(DASH_DIR / "rentals_latest.json", "rental")
    if args.type in (None, "sale"):
        sync_file(DASH_DIR / "sales_latest.json",  "sale")
    print(f"[supabase_sync] total time: {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
