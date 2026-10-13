"""
detail_backfill.py — one-shot Supabase backfill for stale-data rows.

A small slice of our Immobiliare listings sit in Supabase with NULL
rooms / bedrooms / bathrooms because they were scraped at a time when
either the agent hadn't filled the field or our parser had a bug we've
since fixed. The current parser (post-2026-05-22 _parse_count_plus fix)
handles every shape Immobiliare emits today, but those legacy rows
never get refreshed because the listing isn't on the search-results
page anymore (pagination depth, delisted, etc.).

This script:
  1. Queries Supabase for `listing_type=rental, source=immobiliare,
     is_stale=false, (rooms IS NULL OR bedrooms IS NULL OR bathrooms IS NULL)`
  2. For each match, hits the listing's detail URL via the same nodriver
     browser the scanner uses (bypasses Cloudflare).
  3. Parses the detail-page __NEXT_DATA__ via the existing parse_rental()
     — the detail page's `detailData.realEstate` is the same shape as a
     search-results item's `realEstate`, so no special adapter needed.
  4. PATCHes ONLY the recovered fields back into Supabase. Enrichment
     (metro, OMI, scores) is left alone.
  5. If the listing exists in the local *_rentals_latest.json file,
     updates it there too so the localhost dashboard stays consistent.

Usage:
    # Dry run — list candidates without fetching/writing
    python3 detail_backfill.py --dry-run

    # Limit to N listings (smoke test)
    python3 detail_backfill.py --limit 5

    # Real run, default 300 listings cap
    python3 detail_backfill.py

    # Sales instead of rentals
    python3 detail_backfill.py --listing-type sale

Requires SUPABASE_URL + SUPABASE_SERVICE_KEY in env (.env is loaded by
api.py — source it or export inline before running).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

import nodriver as uc

from browser_session import BrowserSession

BASE_DIR  = Path(__file__).resolve().parent
EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

# Reuse the existing parsers + Supabase column-mapping verbatim — no
# duplication of field rename / schema logic.
sys.path.insert(0, str(BASE_DIR))
from fetch_rentals   import parse_rental
from fetch_listings  import parse_listing
from supabase_sync   import listing_to_row, SCHEMA_COLUMNS, FIELD_RENAME

# Source-of-truth fields we want to overwrite from a fresh detail-page
# parse. Names below are the LISTING-DICT keys (pre-rename); they get
# routed through listing_to_row() before hitting Supabase, which handles
# the rent_mo→price + ask_psqm→ask_psqm_rent aliases plus drops anything
# not in SCHEMA_COLUMNS.
#
# Everything else (enrichment, scoring, comps_*, geo_*, metro_*) is
# intentionally excluded — a partial PATCH must NOT clobber data the
# detail-page parse doesn't know about.
RECOVERABLE_KEYS = frozenset({
    "rooms", "bedrooms", "bathrooms",
    "sqm", "ask_psqm", "rent_mo", "price",
    "floor", "floor_n", "floor_label", "is_below_ground", "is_ground_floor",
    "elevator", "energy_class", "year_built", "condition",
    "title", "description", "neighbourhood", "address",
    "latitude", "longitude",
    "photo_count", "thumbnail",
    "spese_condominiali", "condominium_fees",
})


def _env_or_die(name: str) -> str:
    """Pull env var, error out with a clear hint if missing."""
    v = os.environ.get(name)
    if not v:
        sys.exit(f"  [error] {name} not set. Source your .env first, e.g.\n"
                 f"    set -a; source .env; set +a")
    return v


def _supabase_get(url: str, headers: dict, timeout: int = 60):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _supabase_patch(url: str, body: dict, headers: dict, timeout: int = 30):
    data = json.dumps(body).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")


def query_candidates(listing_type: str, limit: int) -> list[dict]:
    """Find Supabase rows with at least one critical field missing.

    PostgREST caps responses at 1000 rows by default regardless of the
    `limit=` query param. We paginate via the `Range` header — each
    request covers up to 1000 rows, and we keep going until we hit the
    caller's overall `limit` or the result set runs out.
    """
    base = _env_or_die("SUPABASE_URL").rstrip("/")
    key  = _env_or_die("SUPABASE_SERVICE_KEY")
    # PostgREST OR clause — three real backfill triggers:
    #   1. rooms IS NULL          (e.g. '5+' parse failures from legacy
    #                              scans + Appartamento with no rooms field)
    #   2. bathrooms IS NULL       (same '3+' pattern or agent-omitted)
    #   3. rooms > 1 AND bedrooms IS NULL
    #                              (real missing bedroom data — Bilocale+)
    # We deliberately EXCLUDE rooms=1 with only bedrooms missing because
    # that's a Monolocale (studio) where bedRoomsNumber is correctly
    # absent — ~713 of the 4832 raw candidates fall into this bucket and
    # would just waste HTTP calls (parse_rental would re-emit None for
    # them and minimal_update would skip).
    select = "id,source,rooms,bedrooms,bathrooms,sqm,title,neighbourhood"
    params = {
        "select":       select,
        "listing_type": f"eq.{listing_type}",
        "source":       "eq.immobiliare",
        "is_stale":     "eq.false",
        "or":           "(rooms.is.null,bathrooms.is.null,and(rooms.gt.1,bedrooms.is.null))",
        "order":        "id",
    }
    qs   = urllib.parse.urlencode(params, safe="(),.")
    page_size = 1000          # PostgREST hard cap
    rows: list[dict] = []
    offset = 0
    while len(rows) < limit:
        want = min(page_size, limit - len(rows))
        url  = f"{base}/rest/v1/listings?{qs}&limit={want}&offset={offset}"
        if offset == 0:
            print(f"  query: {url}")
        headers = {
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Accept":        "application/json",
        }
        page = _supabase_get(url, headers)
        rows.extend(page)
        if len(page) < want:
            break       # ran out
        offset += want
    print(f"  → {len(rows)} candidate(s)")
    return rows


def parse_detail_payload(raw_next_data: str, listing_type: str) -> dict | None:
    """Wrap the detail page's payload into the search-results shape the
    existing parse_rental / parse_listing functions expect, then call
    them verbatim.

    The detail page exposes a few fields in slightly different shapes
    than the search-results page (some scalars are strings instead of
    {value: ...} dicts). Coerce the known-different fields back into
    the search-results shape so the existing parsers don't have to be
    forked.
    """
    nd = json.loads(raw_next_data)
    try:
        re_data = nd["props"]["pageProps"]["detailData"]["realEstate"]
    except Exception:
        return None

    # Detail-page coercions: convert any scalar to the dict shape the
    # search-results parsers expect.
    #   price → search page returns {"value": 2000, ...} but detail page
    #     can return the raw integer or a "€ 2.000" string.
    pv = re_data.get("price")
    if not isinstance(pv, dict):
        # Pull digits out of strings like "€ 2.000/mese" → 2000
        if isinstance(pv, str):
            import re as _re
            digits = _re.sub(r"[^\d]", "", pv)
            pv = int(digits) if digits else None
        re_data["price"] = {"value": pv}

    # properties[0] is always a dict on detail pages we've sampled, but
    # guard anyway — the AttributeError observed in the field came from
    # this chain.
    props = re_data.get("properties") or [{}]
    if not isinstance(props, list) or not props or not isinstance(props[0], dict):
        return None

    item = {"realEstate": re_data}
    if listing_type == "rental":
        return parse_rental(item)
    # parse_listing requires (item, city_key, city_label) — Milan-only
    # for now; extend when a non-Milan stale-data wave appears.
    return parse_listing(item, "milano", "Milano")


def minimal_update(parsed: dict, listing_type: str, original: dict) -> dict | None:
    """Trim parsed dict down to a PATCH body Supabase accepts.

    Two stages:
      1. Drop every parsed key that isn't in RECOVERABLE_KEYS, so we
         don't clobber enrichment/scoring data.
      2. Pass through listing_to_row(), which applies the FIELD_RENAME
         table (rent_mo → price, ask_psqm → ask_psqm_rent for rentals,
         spese_condominiali → condominium_fees) and drops anything not
         in SCHEMA_COLUMNS. Without this, PostgREST rejected the PATCH
         with HTTP 400 ("column 'rent_mo' does not exist").
      3. Diff against the original Supabase row (post-renaming applied
         the same way) so we only PATCH columns that actually changed.
    """
    trimmed = {k: v for k, v in parsed.items()
               if k in RECOVERABLE_KEYS and v is not None}
    if not trimmed:
        return None
    row = listing_to_row(trimmed, listing_type)
    # listing_to_row injects listing_type — drop it; the PATCH targets a
    # single row by id, listing_type doesn't need to change.
    row.pop("listing_type", None)
    # Filter to actual changes vs the original. Original was fetched
    # with un-aliased column names already (Supabase already speaks
    # FIELD_RENAME's target), so we compare directly.
    update: dict = {}
    for col, new_val in row.items():
        if col not in SCHEMA_COLUMNS:
            continue        # belt-and-suspenders — listing_to_row should have dropped these
        old_val = original.get(col)
        if old_val is None or old_val != new_val:
            update[col] = new_val
    return update or None


def _patch_supabase_row(listing_id: str, update: dict) -> bool:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key  = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    url = f"{base}/rest/v1/listings?id=eq.{urllib.parse.quote(str(listing_id))}"
    try:
        status, _ = _supabase_patch(url, update, headers)
        return status in (200, 204)
    except Exception as exc:
        print(f"      [supabase] PATCH failed: {exc}")
        return False


def _patch_local_file(listing_id: str, update: dict, city: str = "milano") -> bool:
    """Mirror the update into the city's local JSON file if the row is
    present there. Localhost dashboard reads this file, so updating it
    keeps the two views consistent."""
    for fname in (f"dashboard/{city}_rentals_latest.json",
                  f"dashboard/rentals_latest.json"):
        p = BASE_DIR / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        changed = False
        for row in data:
            if str(row.get("id", "")) == str(listing_id):
                for k, v in update.items():
                    if row.get(k) != v:
                        row[k] = v
                        changed = True
                break
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False))
            return True
    return False


async def backfill(listing_type: str, limit: int, dry_run: bool,
                   delay: float) -> int:
    candidates = query_candidates(listing_type, limit)
    if not candidates:
        print("  Nothing to backfill — exiting.")
        return 0

    if dry_run:
        print()
        print(f"DRY RUN — {len(candidates)} listing(s) would be processed:")
        for c in candidates[:20]:
            missing = [f for f in ("rooms", "bedrooms", "bathrooms")
                       if c.get(f) is None]
            print(f"  {c['id']:>12}  missing={missing}  "
                  f"{(c.get('title') or '')[:60]}")
        if len(candidates) > 20:
            print(f"  … and {len(candidates) - 20} more")
        return 0

    print()
    print(f"Opening browser for {len(candidates)} detail-page fetch(es) "
          f"@ {delay}s delay each…")
    # Chrome via the shared session (Edge 122 is blocked by Immobiliare);
    # one profile for the manual tools — they don't run concurrently.
    browser = BrowserSession("immobiliare-tools")
    await browser.start()

    fixed   = 0
    skipped = 0
    failed  = 0
    delisted = 0
    try:
        for i, c in enumerate(candidates, 1):
            lid = str(c["id"])
            url = f"https://www.immobiliare.it/annunci/{lid}/"
            print(f"\n  [{i}/{len(candidates)}] {lid} — {url}", flush=True)
            try:
                tab = await browser.get(url)
                await asyncio.sleep(delay)
                # Delisted-listing detection (fast path): when an ad is
                # taken down, Immobiliare redirects away from
                # /annunci/{id}/ — usually to /annunci/ or city search.
                # The URL check is far more reliable than waiting for
                # __NEXT_DATA__ to populate (which never happens on the
                # placeholder page).
                cur = await tab.evaluate("window.location.href")
                if cur and f"/annunci/{lid}" not in cur:
                    print(f"      [delisted] redirected to {cur}")
                    print(f"      → marking is_stale=true")
                    _patch_supabase_row(lid, {"is_stale": True})
                    delisted += 1
                    continue
                raw = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
                # No __NEXT_DATA__ AND URL still points at us → could be
                # a slow-render Cloudflare blip. ONE short retry, then
                # treat the page as gone (also stale-mark it — a listing
                # whose detail page can't even render is effectively
                # delisted as far as scoring is concerned).
                if not raw:
                    await asyncio.sleep(delay + 2.0)
                    raw = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
                if not raw:
                    print(f"      [delisted?] no __NEXT_DATA__ after retry")
                    print(f"      → marking is_stale=true")
                    _patch_supabase_row(lid, {"is_stale": True})
                    delisted += 1
                    continue
                try:
                    parsed = parse_detail_payload(raw, listing_type)
                except Exception as exc:
                    # Print the actual failing line — keeps the run going
                    # while we collect shape-diff samples to fix.
                    tb = traceback.format_exc().splitlines()
                    last = next((l for l in reversed(tb) if "fetch_rentals" in l or "fetch_listings" in l or "detail_backfill" in l), tb[-2] if len(tb) > 1 else tb[-1])
                    print(f"      [parse-error] {type(exc).__name__}: {exc}")
                    print(f"        at {last.strip()}")
                    failed += 1
                    continue
                if not parsed:
                    # __NEXT_DATA__ was there but the realEstate node
                    # wasn't — same "page exists but ad is gone" pattern
                    # as the no-NEXT-DATA case above. Mark stale.
                    print(f"      [delisted?] detail payload missing realEstate")
                    print(f"      → marking is_stale=true")
                    _patch_supabase_row(lid, {"is_stale": True})
                    delisted += 1
                    continue
                update = minimal_update(parsed, listing_type, c)
                if not update:
                    print("      [info] no new fields to write — already up to date")
                    skipped += 1
                    continue
                # Pretty-print what we recovered.
                print(f"      recovered: " + ", ".join(
                    f"{k}={v}" for k, v in update.items()
                    if k in ("rooms", "bedrooms", "bathrooms", "sqm"))
                )
                ok = _patch_supabase_row(lid, update)
                if ok:
                    if _patch_local_file(lid, update):
                        print("      ✓ supabase + local file updated")
                    else:
                        print("      ✓ supabase updated (not in local file)")
                    fixed += 1
                else:
                    failed += 1
            except Exception as exc:
                print(f"      [error] {type(exc).__name__}: {exc}")
                failed += 1
    finally:
        browser.stop()

    print()
    print(f"Done. fixed={fixed}  delisted={delisted}  skipped={skipped}  "
          f"failed={failed}  (of {len(candidates)} candidates)")
    return 0 if failed == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listing-type", choices=("rental", "sale"),
                   default="rental",
                   help="which Supabase rows to backfill (default: rental)")
    p.add_argument("--limit", type=int, default=300,
                   help="max listings to process (default: 300)")
    p.add_argument("--dry-run", action="store_true",
                   help="list candidates without fetching or writing")
    p.add_argument("--delay", type=float, default=3.0,
                   help="seconds between detail-page requests (default: 3)")
    args = p.parse_args()
    return asyncio.run(backfill(
        listing_type=args.listing_type,
        limit=args.limit,
        dry_run=args.dry_run,
        delay=args.delay,
    ))


if __name__ == "__main__":
    sys.exit(main())
