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
import datetime
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
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
    # Price-floor sanity gate (migration 008): mirrors the absolute_value_gate
    # naming convention so the DB column is camel-free.
    "_excluded":                    "excluded",
    "_excluded_reason":             "excluded_reason",
    "_price_floor_gate_applied":    "price_floor_gate_applied",
    "_price_floor_reason":          "price_floor_reason",
    # Extreme-underpricing flag (migration 010): score capped at 55 +
    # gem badges stripped + warning banner shown on the card. The
    # camel-free DB column drops the leading underscore.
    "_extreme_underpricing":        "extreme_underpricing",
    "_extreme_underpricing_delta":  "extreme_underpricing_delta",
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
    # ── Multi-city (migration 007) ─────────────────────────────────────
    # City code (e.g. 'milano', 'roma'). Defaults to 'milano' on the DB
    # side, but every fetcher should stamp listing['city'] explicitly.
    "city",
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
    # ── Fake / foreign-property bait (migration 010) ────────────────────
    "is_fake",
    # ── Misrepresented-address detection (migration 012) ────────────────
    # Listings titled as {city} but actually located in a nearby comune
    # (Opera / Rozzano / San Donato for Milan, etc.). is_outside_city is
    # the geometric proof; is_misrepresented_address is the text signal.
    # Either sets is_fake too so the existing exclusion path filters them.
    "is_misrepresented_address", "is_outside_city", "misrep_reason",
    # ── Sanity gates (migration 008 + 009) ──────────────────────────────
    "excluded", "excluded_reason",
    "price_floor_gate_applied", "price_floor_reason",
    # ── Extreme-underpricing flag (migration 010) ───────────────────────
    "extreme_underpricing", "extreme_underpricing_delta",
    "description",
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
    # ── Price history (migration 004) ───────────────────────────────────
    # Populated by detect_price_drops() during sync. previous_price is
    # the asking price the listing carried on the prior visible day; the
    # daily digest reads these to surface price-drop cards.
    "previous_price", "price_changed_date",
})

# Postgres rejects empty strings for INTEGER / NUMERIC columns. Treat the
# usual JSON sentinels (empty string / list / dict) as NULL on the wire.
def _normalise(v):
    if v == "" or v == [] or v == {}:
        return None
    return v


_live_columns: tuple[float, frozenset[str]] | None = None


def schema_columns() -> frozenset[str]:
    """Columns the live `listings` table actually has, from PostgREST's
    OpenAPI description (refreshed every 10 min). A field added locally
    before its migration ran is then skipped instead of failing every batch
    with "column not found", and starts syncing once the migration is in.
    Falls back to SCHEMA_COLUMNS when the description can't be read."""
    global _live_columns
    if _live_columns and time.time() - _live_columns[0] < 600:
        return _live_columns[1]
    base = (os.environ.get("SUPABASE_URL") or SUPABASE_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or SUPABASE_KEY
    cols = SCHEMA_COLUMNS
    if base and key:
        try:
            req = urllib.request.Request(f"{base}/rest/v1/", headers={
                "apikey": key, "Authorization": f"Bearer {key}",
                "Accept": "application/openapi+json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                spec = json.loads(resp.read())
            live = (spec.get("definitions") or {}).get("listings", {}).get("properties") or {}
            if live:
                cols = frozenset(live)
        except Exception:
            pass
    _live_columns = (time.time(), cols)
    return cols


def listing_to_row(listing: dict, listing_type: str) -> dict:
    """Map a listing dict to a Supabase row, dropping unknown columns."""
    row: dict = {"listing_type": listing_type}
    columns = schema_columns()
    for k, v in listing.items():
        col = FIELD_RENAME.get(k, k)
        if col not in columns:
            continue
        row[col] = _normalise(v)

    # The dashboard shows rows with `is_stale=eq.false` — a NULL is hidden.
    # Locally a missing is_stale means "visible" (`!l.is_stale`), and
    # Idealista rentals never carried the field, so ~2.2k of them were
    # invisible online only. Unknown staleness → visible, same as local.
    row["is_stale"] = bool(listing.get("is_stale"))
    # Sent even when empty: an upsert leaves a column it isn't given as it
    # was, so a cap reason gone locally stayed online (sale 96022006,
    # 2026-10-25: the reason repeated 13 times online, empty locally).
    if "score_cap_reason" in columns:
        row["score_cap_reason"] = _normalise(listing.get("score_cap_reason"))

    # The photos' 1–5 scales are SMALLINT columns (migration 016) and Sonnet
    # sometimes answers a half (4.5): one failed its whole 200-row batch
    # (HTTP 400, 2026-10-25), so online fell behind. Rounded half up.
    for col in ("photo_condition", "photo_kitchen", "photo_bathroom", "photo_light"):
        v = row.get(col)
        if isinstance(v, float):
            row[col] = int(v + 0.5)

    # Multi-city safety net: the listings.city column stores the lowercase
    # code ('milano', 'roma') — match the cities table PK. Old scanner
    # output occasionally writes the display label ("Milano"); fall back
    # to city_key when the value doesn't look like a code.
    city_val = row.get("city")
    if isinstance(city_val, str) and city_val:
        if city_val != city_val.lower() or " " in city_val:
            fallback = listing.get("city_key") or city_val.lower().replace(" ", "_")
            row["city"] = fallback

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


def fetch_current_prices(listing_ids: list[str]) -> dict[str, int | None]:
    """
    Fetch the currently-stored price for each ID via PostgREST's `id=in.(...)`
    filter. Returns {id: price}. Missing IDs are absent (interpreted as
    "first time we've seen this listing"). On any HTTP/network error we
    return {} and the caller treats the batch as having no prior prices —
    safer than misclassifying a fresh listing as a price drop.
    """
    if not listing_ids:
        return {}
    # Quote each ID so values containing commas / parentheses don't break
    # the in-filter syntax (real listing IDs are numeric or "id_xxxxx", but
    # this is cheap insurance).
    quoted = ",".join(f'"{i}"' for i in listing_ids)
    url    = (f"{SUPABASE_URL}/rest/v1/listings?"
              f"select=id,price&id=in.({quoted})&limit={len(listing_ids)}")
    req = urllib.request.Request(url, headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept":        "application/json",
        "Prefer":        "count=none",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read())
        return {r["id"]: r.get("price") for r in rows if r.get("id")}
    except Exception as exc:
        print(f"  [price check] fetch failed: {exc}")
        return {}


def detect_price_drops(rows: list[dict]) -> list[dict]:
    """
    Compare each row's incoming price against the price currently stored
    in Supabase. When the new price is strictly lower, annotate the row
    with `previous_price` (the old value) and `price_changed_date` (today)
    so the daily digest can surface the drop.

    Drops only — increases are ignored. Landlords sometimes re-list at a
    higher price after a market test; flagging that as a "change" would
    spam users with non-actionable noise.
    """
    today = datetime.date.today().isoformat()
    ids = [r["id"] for r in rows if r.get("id") and r.get("price") is not None]
    if not ids:
        return rows

    current_prices = fetch_current_prices(ids)
    drops = 0

    for row in rows:
        # The scanners now record drops locally (write_output) and carry
        # them forward — keep those instead of re-deriving (which nulled
        # every drop on the next push).
        if row.get("previous_price") is not None:
            continue
        lid       = row.get("id")
        new_price = row.get("price")
        old_price = current_prices.get(lid)

        if (old_price is not None and new_price is not None
                and isinstance(old_price, (int, float))
                and isinstance(new_price, (int, float))
                and new_price < old_price):
            row["previous_price"]     = int(old_price)
            row["price_changed_date"] = today
            drops += 1
        else:
            # Only set NULL placeholders when the row doesn't already
            # carry price-history fields (preserves history written by an
            # earlier run inside the same scan, in case rows are batched
            # across passes).
            row.setdefault("previous_price", None)
            row.setdefault("price_changed_date", None)

    if drops:
        print(f"  [price drop] detected {drops} drop(s) in this batch")
    return rows


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
    # Detect price drops BEFORE shape-normalising so the new
    # previous_price / price_changed_date keys end up in the union of
    # batch keys and survive the upsert.
    rows = detect_price_drops(rows)
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


# What push_local_json last sent, per row: {"day": date of the last full
# push, "rows": {id: content hash}}. The spread scan rewrites the file ~18
# times a day, and a full upsert of ~21k rows took 2–4 min (one batch timed
# out on 2026-10-17 with two pushes running at once), so pushes send only
# rows whose content changed. The first push of each day is full, which
# repairs anything changed online behind our back.
def _pushed_state_path(path: Path) -> Path:
    return BASE_DIR / f".pushed_{path.stem}.json"


def _row_hash(row: dict) -> str:
    return hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]


def sync_file(path: Path, listing_type: str, batch_size: int = 200,
              only_changed: bool = False) -> bool:
    if not path.exists():
        print(f"[supabase_sync] {path.name} not found — skipping")
        return True

    with open(path) as fh:
        listings = json.load(fh)

    rows = [listing_to_row(l, listing_type) for l in listings if l.get("id")]
    total = len(rows)
    state = None
    if only_changed:
        today = datetime.date.today().isoformat()
        try:
            state = json.loads(_pushed_state_path(path).read_text())
        except (OSError, ValueError):
            state = {}
        if state.get("day") != today:
            state = {"day": today, "rows": {}}   # first push of the day: full
        hashes = {str(r["id"]): _row_hash(r) for r in rows}
        sent = {i: h for i, h in state["rows"].items() if i in hashes}   # drop trimmed ids
        rows = [r for r in rows if sent.get(str(r["id"])) != hashes[str(r["id"])]]
        state["rows"] = sent
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    print(f"[supabase_sync] {listing_type}s: {len(rows)} rows in {len(batches)} batches"
          + (f" (changed, of {total})" if only_changed else ""))

    failed = 0
    for i, b in enumerate(batches, 1):
        if upsert_batch(b, i, len(batches)):
            if state is not None:
                for r in b:
                    state["rows"][str(r["id"])] = hashes[str(r["id"])]
        else:
            failed += 1
        time.sleep(0.1)   # gentle rate limit — Supabase free tier is fine
    if state is not None:
        tmp = _pushed_state_path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")))
        tmp.replace(_pushed_state_path(path))
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


# Legacy single-city snapshots (May 2026) — never pushed: their rows overwrite
# current Milan rows and come back online as live (incidents on 2026-10-14).
LEGACY_FILES = {"rentals_latest.json", "sales_latest.json"}

# path → mtime of the version last pushed (in this process), so api.py's
# autosync watcher doesn't push again what a rescore/enrichment just pushed.
PUSHED_MTIME: dict[str, float] = {}
# One push at a time (api.py pushes from several threads): two at once timed
# out a batch, and each must diff against what the other just sent.
_PUSH_LOCK = threading.Lock()


def archive_missing_online(path: Path, listing_type: str) -> int:
    """Mark online rows stale when the local file no longer has them.

    Pushes are upserts and never delete: a listing the scanners trim from
    the file after it went stale (trim_stale_listings) stayed *live* online
    forever — 566 such rows after one night (2026-10-15). Returns the count."""
    try:
        rows = json.loads(path.read_text())
    except Exception:
        return 0
    if not rows:
        return 0
    city = (rows[0].get("city") or rows[0].get("city_key") or "").lower()
    if not city:
        return 0
    local = {str(r.get("id")) for r in rows}
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json"}
    online, offset = set(), 0
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/listings?select=id&listing_type=eq.{listing_type}"
               f"&city=eq.{urllib.parse.quote(city)}&is_stale=eq.false&order=id.asc&limit=1000&offset={offset}")
        page = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    page = json.loads(r.read())
                break
            except Exception:
                time.sleep(10)
        if page is None:
            print("[archive] could not list online rows — skipped")
            return 0
        online.update(str(x["id"]) for x in page)
        if len(page) < 1000:
            break
        offset += 1000
    missing = sorted(online - local)
    for i in range(0, len(missing), 100):
        ids = ",".join(f'"{m}"' for m in missing[i:i + 100])
        url = (f"{SUPABASE_URL}/rest/v1/listings?listing_type=eq.{listing_type}"
               f"&id=in.({urllib.parse.quote(ids)})")
        body = json.dumps({"is_stale": True}).encode()
        for attempt in range(4):
            status, _ = _http_patch(url, body, {**headers, "Prefer": "return=minimal"})
            if 200 <= status < 300:
                break
            time.sleep(10)
    if missing:
        print(f"[archive] {len(missing)} online-only {city} {listing_type}s marked stale")
    return len(missing)


def _http_patch(url: str, body_bytes: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as exc:
        return 0, str(exc)


def push_local_json(path: Path, listing_type: str) -> bool:
    """Upsert the local JSON's contents back to Supabase. Wraps sync_file
    so callers can refresh the DB after a local mutation (e.g. enrichment
    appended geo fields, rescore recomputed scores)."""
    if not path.exists():
        return False
    if path.name in LEGACY_FILES:
        print(f"[push] {path.name} is a legacy snapshot — not pushed")
        return False
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[push] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping push")
        return False
    with _PUSH_LOCK:
        try:
            PUSHED_MTIME[str(path)] = path.stat().st_mtime
            ok = sync_file(path, listing_type, only_changed=True)
            archive_missing_online(path, listing_type)
            return ok
        except Exception as exc:
            print(f"[push] sync_file failed: {exc}")
            return False


def _input_path(city: str, listing_type: str) -> Path:
    """
    Per-city, per-type input file. Multi-city expansion (migration 007)
    moved away from the bare rentals_latest.json / sales_latest.json
    names so multiple cities' scans can co-exist on disk.

    Falls back to the legacy name when the new one is missing — keeps
    the very first Milan run after the rename working without manual
    file shuffling.
    """
    suffix = "rentals" if listing_type == "rental" else "sales"
    new    = DASH_DIR / f"{city}_{suffix}_latest.json"
    if new.exists():
        return new
    legacy = DASH_DIR / f"{suffix}_latest.json"
    return legacy if legacy.exists() else new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="milano",
                        help="City code (e.g. milano, roma, napoli, la_maddalena)")
    parser.add_argument("--type", choices=["rental", "sale"],
                        help="Which listing type to sync (default: both)")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[supabase_sync] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping (this is fine)")
        return 0

    t0 = time.time()
    print(f"[supabase_sync] city={args.city}")
    if args.type in (None, "rental"):
        sync_file(_input_path(args.city, "rental"), "rental")
    if args.type in (None, "sale"):
        sync_file(_input_path(args.city, "sale"),   "sale")
    print(f"[supabase_sync] total time: {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
