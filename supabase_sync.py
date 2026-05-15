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
    # Italian source data uses `spese_condominiali`; the DB stores the
    # unified `condominium_fees` column. The dashboard JS still reads
    # `spese_condominiali`, so SupabaseClient.fetchAll aliases it back.
    "spese_condominiali":           "condominium_fees",
}

# Columns the Supabase schema actually has (per the prompt's CREATE TABLE).
# Anything else gets dropped on the way in so Supabase doesn't reject the
# upsert with "column does not exist".
SCHEMA_COLUMNS: frozenset[str] = frozenset({
    # ── Identity ────────────────────────────────────────────────────────
    "id", "source", "listing_type",
    # ── Display ─────────────────────────────────────────────────────────
    "title", "address", "neighbourhood", "city",
    "url", "thumbnail", "photos", "photo_count",
    # ── Price ───────────────────────────────────────────────────────────
    "price", "ask_psqm", "ask_psqm_rent",
    # ── Physical ────────────────────────────────────────────────────────
    "sqm", "rooms", "bedrooms", "floor", "floor_n", "floor_label",
    "elevator", "has_balcony", "has_parking", "furnished", "condition",
    "year_built", "energy_class", "bathrooms", "condominium_fees",
    "heating_type", "is_external", "is_below_ground", "is_ground_floor",
    "is_auction", "is_nuda_proprieta",
    # ── Location / proximity ────────────────────────────────────────────
    "latitude", "longitude",
    "omi_zona", "omi_fascia", "omi_descr",
    "metro_walk_min", "metro_nearest_name", "metro_nearest_line",
    "metro_nearest_dist_m",
    "park_nearest_dist_m", "supermarket_nearest_dist_m",
    "university_nearest_dist_m", "tram_nearest_dist_m",
    "geo_score",
    # ── OMI benchmarks ──────────────────────────────────────────────────
    "omi_compr_mid", "omi_compr_min", "omi_compr_max",
    "omi_loc_mid",   "omi_loc_min",   "omi_loc_max",
    "omi_source", "omi_fallback",
    "vs_omi_pct", "vs_omi_label",
    # ── Scoring ─────────────────────────────────────────────────────────
    "score_total", "score_price", "score_property", "score_physical",
    "score_location", "score_penalty", "score_geo",
    "score_reasons", "score_was_capped",
    "ldi_score", "ldi_bonus",
    # ── Comps (rentals) ─────────────────────────────────────────────────
    "comps_delta_pct", "comps_n", "comps_median",
    "comps_confidence", "comps_conf_label", "comps_source", "comps_label",
    "comps_adjusted", "comps_ids",
    # ── Comps (sales) ───────────────────────────────────────────────────
    "comps_sale_median", "comps_sale_n",
    "comps_sale_source", "comps_sale_confidence",
    "comps_sale_conf_label", "comps_sale_delta_pct", "comps_sale_label",
    "comps_sale_adjusted", "comps_sale_comp_ids",
    # ── Flags ───────────────────────────────────────────────────────────
    "hidden_gem", "good_value",
    "boosted_price_score", "is_corporate_rental",
    "room_efficiency_flag", "absolute_value_gate_applied",
    # ── Pricing suggestions ─────────────────────────────────────────────
    "suggested_rent_mo", "suggested_rent_psqm",
    # ── Investor metrics (sales) ────────────────────────────────────────
    "estimated_rent_mo", "estimated_rent_psqm", "estimated_yield_pct",
    "estimated_rent_n_comps", "estimated_rent_confidence",
    "estimated_rent_method", "estimated_rent_comp_ids",
    # ── Agency (rentals leaderboard) ────────────────────────────────────
    "agency_id", "agency_name", "agency_type", "agency_url",
    # ── Staleness / lifecycle ───────────────────────────────────────────
    "first_seen_date", "last_seen_date", "published_date",
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

    # The DB has a single unified `price` column for both listing types, but
    # rental scrapers store the monthly rent in `rent_mo`. Without this
    # remap, every rental row lands with price=NULL and the dashboard's
    # `listing.rent_mo` accesses come back undefined.
    if listing_type == "rental":
        if row.get("price") in (None, ""):
            row["price"] = _normalise(listing.get("rent_mo"))
        # Rentals' €/m²/mo column is `ask_psqm_rent`; some sources only
        # populate `ask_psqm`, so fall back to it when needed.
        if row.get("ask_psqm_rent") in (None, ""):
            row["ask_psqm_rent"] = _normalise(listing.get("ask_psqm"))

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


def _normalise_batch_keys(rows: list[dict]) -> list[dict]:
    """
    PostgREST requires every object in a bulk POST to share the same key set
    (PGRST102: "All object keys must match"). Different listings in the JSON
    have different fields populated — e.g. one might carry `year_built`
    while the next omits it. Build the union of keys across the batch and
    rebuild each row with the same shape, filling absent keys with None.
    """
    if not rows:
        return rows
    all_keys: set[str] = set()
    for r in rows:
        all_keys.update(r.keys())
    return [{k: r.get(k) for k in all_keys} for r in rows]


def upsert_batch(rows: list[dict], idx: int, total: int) -> bool:
    """POST a batch to /rest/v1/listings with merge-duplicates semantics."""
    rows = _normalise_batch_keys(rows)
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


def _alias_for_local_use(row: dict, listing_type: str) -> dict:
    """
    Reverse the listing_to_row() column aliases so a row pulled from
    Supabase looks like the dict shape the scanner / scoring code emit.
    Mirrors the aliasing the dashboard's SupabaseClient.fetchAll() does
    on the JS side.
    """
    is_rental = listing_type == "rental"
    if is_rental:
        if row.get("rent_mo") is None and row.get("price") is not None:
            row["rent_mo"] = row["price"]
        if row.get("ask_psqm_rent") is None and row.get("ask_psqm") is not None:
            row["ask_psqm_rent"] = row["ask_psqm"]
    if row.get("spese_condominiali") is None and row.get("condominium_fees") is not None:
        row["spese_condominiali"] = row["condominium_fees"]
    if row.get("_room_efficiency_flag") is None and row.get("room_efficiency_flag") is not None:
        row["_room_efficiency_flag"] = row["room_efficiency_flag"]
    if row.get("_absolute_value_gate_applied") is None and row.get("absolute_value_gate_applied") is not None:
        row["_absolute_value_gate_applied"] = row["absolute_value_gate_applied"]
    return row


def hydrate_local_json(path: Path, listing_type: str, page_size: int = 1000) -> bool:
    """
    Materialise the local JSON snapshot from Supabase if it doesn't
    already exist on disk. Used by api.py routes (geo-enrich, rescore,
    data-quality, digest) so they keep working on a machine that hasn't
    run a scan yet — Supabase is the system of record now.

    Pages with limit+offset to bypass PostgREST's 1000-row default cap.
    Falls back to the anon key when SUPABASE_SERVICE_KEY isn't set, which
    works for non-stale reads (RLS allows them).

    Returns True if `path` is now readable, False otherwise.
    """
    if path.exists():
        return True

    base = (os.environ.get("SUPABASE_URL") or SUPABASE_URL).rstrip("/")
    key  = (os.environ.get("SUPABASE_SERVICE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
            or SUPABASE_KEY)
    if not base or not key:
        print(f"[hydrate] SUPABASE_URL / key not set — can't fetch {path.name}")
        return False

    rows: list[dict] = []
    offset = 0
    while True:
        params = (
            f"select=*&listing_type=eq.{listing_type}"
            f"&is_stale=eq.false&order=id&limit={page_size}&offset={offset}"
        )
        url = f"{base}/rest/v1/listings?{params}"
        req = urllib.request.Request(url, headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Accept":        "application/json",
            "Prefer":        "count=none",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                page = json.loads(resp.read())
        except Exception as exc:
            print(f"[hydrate] page @ offset {offset} failed: {exc}")
            return False
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    for r in rows:
        _alias_for_local_use(r, listing_type)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False))
    print(f"[hydrate] wrote {len(rows)} {listing_type}s from Supabase → {path.name}")
    return True


def push_local_json(path: Path, listing_type: str) -> bool:
    """Upsert the local JSON's contents back to Supabase. Wraps sync_file
    so callers can refresh the DB after a local mutation (e.g. enrichment
    appended geo fields, rescore recomputed scores)."""
    if not path.exists():
        return False
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[push] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping push")
        return False
    try:
        return sync_file(path, listing_type)
    except Exception as exc:
        print(f"[push] sync_file failed: {exc}")
        return False


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
