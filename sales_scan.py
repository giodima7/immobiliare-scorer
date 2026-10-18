"""Sales (compravendita) in the focus areas.

Both scanner daemons run run_sales(<their site>) between slots every
SALES_EVERY_SEC: one browser session sweeps the site's sale areas from
focus_areas.json ("sales": Immobiliare macro-areas, Idealista (sub)zones,
max price, min rooms) — ~580 listings / 24 pages on Immobiliare on
2026-10-18 — and the result is merged into dashboard/milano_sales_latest.json:

- first / last seen (seen_ids_sales_milano.json), price drops;
- de-listing like the rentals: a listing missing from DELIST_AFTER_MISSES
  complete sweeps of its area in a row is hidden; unseen for STALE_DAYS →
  hidden, for REMOVE_AFTER_DAYS → dropped (the May 2026 rows go this way);
- location and OMI zone for listings without them (enrich_geo, omi_lookup);
- scoring.score_all_sales, then the rent the flat would fetch, from the
  rentals we hold: median €/m² of rentals within RENT_RADIUS_M of similar
  size and rooms → estimated_rent_mo and gross estimated_yield_pct.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import random
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

BASE_DIR        = Path(__file__).resolve().parent
SALES_PATH      = BASE_DIR / "dashboard" / "milano_sales_latest.json"
RENTALS_PATH    = BASE_DIR / "dashboard" / "milano_rentals_latest.json"
SEEN_PATH       = BASE_DIR / "seen_ids_sales_milano.json"
LOG_PATH        = BASE_DIR / "sales_scans_milano.jsonl"
CONFIG_PATH     = BASE_DIR / "focus_areas.json"

SALES_EVERY_SEC     = 4 * 3600
DELIST_AFTER_MISSES = 2
STALE_DAYS          = 7
REMOVE_AFTER_DAYS   = 30
ROW_SOURCE          = {"immobiliare": "sale", "idealista": "idealista_sale"}
RENT_RADIUS_M       = (700, 1200)     # second radius when the first has too few
RENT_MIN_COMPS      = 5


# The 17 Immobiliare macro-areas of Milan, readable — the areas the user can
# pick for sales (Tools-free: the "For sale" tab, api.py /api/sale-areas).
MACRO_NAMES = {
    "affori-bovisa": "Affori, Bovisa", "arco-della-pace-arena-pagano": "Arco della Pace, Arena, Pagano",
    "cenisio-sarpi-isola": "Cenisio, Sarpi, Isola", "centrale-repubblica": "Centrale, Repubblica",
    "centro": "Centro", "citta-studi-susa": "Città Studi, Susa",
    "fiera-sempione-city-life-portello": "Fiera, Sempione, City Life, Portello",
    "garibaldi-moscova-porta-nuova": "Garibaldi, Moscova, Porta Nuova", "genova-ticinese": "Genova, Ticinese",
    "navigli": "Navigli", "ponte-lambro-santa-giulia": "Ponte Lambro, Santa Giulia",
    "porta-romana-cadore-montenero": "Porta Romana, Cadore, Montenero",
    "porta-venezia-indipendenza": "Porta Venezia, Indipendenza", "porta-vittoria-lodi": "Porta Vittoria, Lodi",
    "quadronno-palestro-guastalla": "Quadronno, Palestro, Guastalla", "ripamonti-vigentino": "Ripamonti, Vigentino",
    "solari-washington": "Solari, Washington",
}
# Idealista's zones are wider than Immobiliare's macro-areas; for the focus
# areas a subzone fits better than the whole zone.
IDEALISTA_SUBZONES = {
    "porta-romana-cadore-montenero": ["porta-vittoria/porta-romana"],
    "genova-ticinese": ["navigli-bocconi/corso-genova"],
    "quadronno-palestro-guastalla": ["navigli-bocconi/mercalli-quadronno"],
}


def available_areas() -> list:
    return [{"slug": s, "name": n} for s, n in MACRO_NAMES.items()]


def idealista_zones_for(macros: list) -> list:
    """Idealista zones covering these macro-areas (fetch_idealista's own
    matching); a subzone is dropped when its whole zone is listed anyway."""
    from fetch_idealista import _match_zones_for_immo_areas, to_url_slug
    out: list = []
    for m in macros:
        for z in IDEALISTA_SUBZONES.get(m) or [to_url_slug(n) for n in _match_zones_for_immo_areas([m])]:
            if z not in out:
                out.append(z)
    return [z for z in out if "/" not in z or z.split("/")[0] not in out]


def set_sale_areas(macros: list, max_price=None, min_rooms=None) -> dict:
    """Save the chosen sale areas (and filters) to focus_areas.json and make
    both daemons sweep at their next pause (≤ ~20 min)."""
    d = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    s = d.setdefault("sales", {})
    s["immobiliare"] = [m for m in macros if m in MACRO_NAMES]
    s["idealista"] = idealista_zones_for(s["immobiliare"])
    if max_price is not None:
        s["max_price"] = int(max_price)
    if min_rooms is not None:
        s["min_rooms"] = int(min_rooms)
    CONFIG_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    for source in ROW_SOURCE:
        _state_path(source).unlink(missing_ok=True)
    return s


def config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text()).get("sales") or {}
    except (OSError, ValueError):
        return {}


def _state_path(source: str) -> Path:
    return BASE_DIR / f".sales_last_{source}"


def sales_due(source: str) -> bool:
    if not config().get(source):
        return False
    try:
        last = float(_state_path(source).read_text().strip())
    except (OSError, ValueError):
        last = 0.0
    return time.time() - last >= SALES_EVERY_SEC


# ── Sweeps ───────────────────────────────────────────────────────────────────

async def _sweep_immobiliare(browser, areas: list, max_price: int, min_rooms: int,
                             delay: float = 2.5) -> tuple:
    """Every page of each sale macro-area. → (rows, {area: area} read to the
    last page, page loads)."""
    from browser_session import PageTimeout, timed
    from fetch_listings import parse_listing
    rows, complete, loads = [], {}, 0
    for i, area in enumerate(areas):
        seen_here: set = set()
        finished = False
        print(f"\n    sale area: {area}", end="", flush=True)
        for page in range(1, 61):
            params = {"prezzoMassimo": max_price, "localiMinimo": min_rooms}
            if page > 1:
                params["pag"] = page
            url = f"https://www.immobiliare.it/vendita-case/milano/{area}/?{urlencode(params)}"
            data = None
            for attempt in (1, 2):
                try:
                    tab = await browser.get(url)
                    loads += 1
                    await asyncio.sleep(delay if attempt == 1 else delay + 4)
                    raw = await timed(tab.evaluate("JSON.stringify(window.__NEXT_DATA__)"),
                                      what="read __NEXT_DATA__")
                    data = json.loads(raw)["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
                    break
                except PageTimeout:
                    await browser.restart()
                    break
                except Exception:
                    continue
            if data is None:
                print(f" p{page}: unreadable — area not read in full", end="", flush=True)
                break
            results = data.get("results") or []
            if not results:
                finished = True
                break
            ids = {str((r.get("realEstate") or {}).get("id")) for r in results}
            if page > 1 and ids <= seen_here:
                finished = True
                break
            seen_here |= ids
            for item in results:
                l = parse_listing(item, "milano", "Milano")
                if l:
                    l["source"], l["scan_area"] = "sale", area
                    rows.append(l)
            print(f" p{page}", end="", flush=True)
            if page >= (data.get("maxPages") or page):
                finished = True
                break
        if finished:
            complete[area] = area
        if i < len(areas) - 1:
            await asyncio.sleep(random.uniform(4, 8))
    return rows, complete, loads


async def _sweep_idealista(browser, zones: list, max_price: int, min_rooms: int) -> tuple:
    from fetch_idealista import _fetch_sale_async
    complete: dict = {}
    rows, _skipped = await _fetch_sale_async(60, zones, max_price, 0, min_rooms, 3.0, browser,
                                             complete=complete)
    for l in rows:
        l["source"] = "idealista_sale"
    return rows, complete, None


# ── Merge ────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked():
    with open(SALES_PATH.with_suffix(SALES_PATH.suffix + ".lock"), "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _merge(existing: list, fresh: list, row_source: str, swept: set, seen: dict) -> tuple:
    """→ (rows, counts). Only rows of `row_source` are touched."""
    today = date.today().isoformat()
    by_id = {str(l.get("id")): l for l in existing}
    fresh_ids = {str(l["id"]) for l in fresh}
    out, counts = [], {"new": 0, "missed": 0, "hidden": 0, "dropped": 0, "price_drops": 0}
    for f in fresh:
        lid = str(f["id"])
        old = by_id.get(lid) or {}
        row = {**old, **{k: v for k, v in f.items() if v is not None}}
        for k in ("missed_sweeps", "delisted_date"):
            row.pop(k, None)
        row["is_stale"] = False
        if lid not in seen:
            counts["new"] += 1
        row["first_seen_date"] = (seen.get(lid) or {}).get("first_seen_date") or old.get("first_seen_date") or today
        row["last_seen_date"] = today
        was, now = old.get("price"), f.get("price")
        if was and now and now < was:
            row["previous_price"], row["price_changed_date"] = was, today
            counts["price_drops"] += 1
        seen[lid] = {"first_seen_date": row["first_seen_date"], "last_seen_date": today}
        out.append(row)
    cut_stale = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    cut_drop = (date.today() - timedelta(days=REMOVE_AFTER_DAYS)).isoformat()
    for l in existing:
        lid = str(l.get("id"))
        if lid in fresh_ids:
            continue
        if (l.get("source") or "sale") != row_source:
            out.append(l)
            continue
        last = str(l.get("last_seen_date") or l.get("first_seen_date") or "")[:10]
        if last and last < cut_drop:
            counts["dropped"] += 1
            seen.pop(lid, None)
            continue
        if l.get("scan_area") in swept and not l.get("is_stale"):
            l["missed_sweeps"] = (l.get("missed_sweeps") or 0) + 1
            if l["missed_sweeps"] >= DELIST_AFTER_MISSES:
                l["is_stale"], l["delisted_date"] = True, today
                counts["hidden"] += 1
            else:
                counts["missed"] += 1
        if last and last < cut_stale:
            l["is_stale"] = True
        out.append(l)
    return list({str(l.get("id")): l for l in out}.values()), counts


# ── Enrichment and estimates ─────────────────────────────────────────────────

def enrich_rows(rows: list) -> int:
    """Location + POIs for rows without coordinates or metro data (cached by
    source:id like the rentals), then OMI polygon fields where missing."""
    import enrichment_cache as ec
    from enrich_geo import enrich_batch
    ec.load()
    todo = [l for l in rows if not l.get("is_stale") and (l.get("metro_nearest_dist_m") is None)
            and ec.get(l["source"], l["id"]) is None]
    if todo:
        results = enrich_batch(todo)
        ec.bulk_save([(l["source"], l["id"], g) for l, g in zip(todo, results)])
    for l in rows:
        cached = ec.get(l.get("source") or "sale", l["id"])
        if cached:
            l.update({k: v for k, v in cached.items() if k not in ec.META_KEYS and v is not None})
    import omi_lookup
    for l in rows:
        if l.get("omi_compr_mid") is None and l.get("latitude") and l.get("longitude"):
            zone, src = omi_lookup.lookup_for_city(float(l["latitude"]), float(l["longitude"]), city="milano")
            if zone:
                l.update({"omi_zona": zone["zona"], "omi_fascia": zone["fascia"], "omi_descr": zone["descr"],
                          "omi_loc_min": zone["loc_min"], "omi_loc_max": zone["loc_max"], "omi_loc_mid": zone["loc_mid"],
                          "omi_compr_min": zone["compr_min"], "omi_compr_max": zone["compr_max"],
                          "omi_compr_mid": zone["compr_mid"], "omi_source": src})
    return len(todo)


def _dist_m(a: tuple, b: tuple) -> float:
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    return 6371000 * math.hypot(x, (b[0] - a[0]) * r)


def estimate_rents(sales: list) -> int:
    """estimated_rent_mo / _psqm / _n_comps / _method and gross
    estimated_yield_pct from the visible rentals near each sale: within
    RENT_RADIUS_M, size within ±30 %, rooms within ±1; median rent €/m² × m²."""
    try:
        rentals = json.loads(RENTALS_PATH.read_text())
    except (OSError, ValueError):
        return 0
    pool = [(float(r["latitude"]), float(r["longitude"]), float(r["sqm"]), r.get("rooms") or 0,
             float(r["rent_mo"]) / float(r["sqm"]))
            for r in rentals
            if not r.get("is_stale") and not r.get("excluded") and r.get("latitude") and r.get("longitude")
            and r.get("sqm") and r.get("rent_mo") and 15 <= float(r["sqm"]) <= 400]
    n_done = 0
    for s in sales:
        if s.get("is_stale") or not (s.get("latitude") and s.get("longitude") and s.get("sqm") and s.get("price")):
            continue
        here, sqm, rooms = (float(s["latitude"]), float(s["longitude"])), float(s["sqm"]), s.get("rooms") or 0
        for radius in RENT_RADIUS_M:
            dlat = radius / 111_000
            near = [p for p in pool
                    if abs(p[0] - here[0]) <= dlat and abs(p[1] - here[1]) <= dlat * 1.5
                    and 0.7 * sqm <= p[2] <= 1.3 * sqm and (not rooms or not p[3] or abs(p[3] - rooms) <= 1)
                    and _dist_m(here, (p[0], p[1])) <= radius]
            if len(near) >= RENT_MIN_COMPS:
                psqm = statistics.median(p[4] for p in near)
                rent = round(psqm * sqm / 10) * 10
                s.update({"estimated_rent_mo": rent, "estimated_rent_psqm": round(psqm, 2),
                          "estimated_rent_n_comps": len(near), "estimated_rent_method": f"rentals_{radius}m",
                          # 0–100 (an INTEGER column online): 15+ comparable rentals → 80, 5–14 → 50
                          "estimated_rent_confidence": 80 if len(near) >= 15 else 50,
                          "estimated_yield_pct": round(rent * 12 / float(s["price"]) * 100, 2)})
                n_done += 1
                break
    return n_done


# ── Run ──────────────────────────────────────────────────────────────────────

def run_sales(source: str, log=print) -> dict:
    """Sweep `source`'s sale areas, merge, enrich, score, estimate rents,
    write. Returns the numbers (also appended to LOG_PATH)."""
    cfg = config()
    areas = cfg.get(source) or []
    max_price, min_rooms = int(cfg.get("max_price") or 0), int(cfg.get("min_rooms") or 0)
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "source": source, "areas": areas}
    _state_path(source).write_text(str(time.time()))
    if not areas:
        return rec
    t0 = time.time()
    from browser_session import BrowserSession

    async def _run():
        browser = BrowserSession(source, background=True)
        await browser.start()
        try:
            if source == "immobiliare":
                return await _sweep_immobiliare(browser, areas, max_price, min_rooms)
            return await _sweep_idealista(browser, areas, max_price, min_rooms)
        finally:
            browser.stop()

    fresh, complete, loads = asyncio.run(_run())
    # The sites' own filters, again: a redirected page can drop them. One row
    # per id: a listing can slide onto the next page mid-sweep and come back
    # twice (Supabase rejects a batch holding the same id twice).
    fresh = list({str(l["id"]): l for l in fresh
                  if (not max_price or (l.get("price") or 0) <= max_price)
                  and (not min_rooms or not l.get("rooms") or l["rooms"] >= min_rooms)}.values())
    rec.update(found=len(fresh), complete=sorted(complete), pages=loads)

    import scoring
    from dashboard_io import write_snapshot
    with _locked():
        try:
            existing = json.loads(SALES_PATH.read_text()) if SALES_PATH.exists() else []
        except ValueError:
            existing = []
        try:
            seen = json.loads(SEEN_PATH.read_text())
        except (OSError, ValueError):
            seen = {str(l["id"]): {"first_seen_date": l.get("first_seen_date"),
                                   "last_seen_date": l.get("last_seen_date")} for l in existing if l.get("id")}
        rows, counts = _merge(existing, fresh, ROW_SOURCE[source], set(complete.values()), seen)
        rec.update(counts)
        rec["geocoded"] = enrich_rows(rows)
        # Listing pages read by the drip (condo fees, heating, gallery…).
        from listing_details import apply_details
        rec["pages_applied"] = apply_details(rows)
        with contextlib.redirect_stderr(open("/dev/null", "w")):
            scored = scoring.score_all_sales(rows)
        # What the photos showed (photo_eval): same stamp and cap as rentals.
        evals = scoring.load_photo_evals()
        cap = scoring._load_settings().get("photo_contradicts_cap", 69)
        for l in scored:
            scoring.apply_photo_verdict(l, evals, cap)
        rec["photo_verdicts"] = sum(1 for l in scored if l.get("photo_deal"))
        rec["rent_estimates"] = estimate_rents(scored)
        clean = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
        write_snapshot(SALES_PATH, clean)
        SEEN_PATH.write_text(json.dumps(seen))
    rec["visible"] = sum(1 for l in clean if not l.get("is_stale"))
    rec["seconds"] = round(time.time() - t0)
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    log(f"Sales — {source}: {rec['found']} found ({len(complete)}/{len(areas)} areas in full), "
        f"{counts['new']} new, {counts['hidden']} hidden, {counts['dropped']} dropped, "
        f"{rec['rent_estimates']} rent estimates · {rec['visible']} visible")
    return rec
