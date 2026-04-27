#!/usr/bin/env python3
"""
fetch_idealista.py
──────────────────
Fetches Milano rental listings from the Idealista API,
scores them using OMI benchmarks, and merges into
dashboard/rentals_latest.json alongside Immobiliare.it results.

Usage:
    python3 fetch_idealista.py
    python3 fetch_idealista.py --pages 3 --max-rent 2000
    python3 fetch_idealista.py --daemon

Credentials: set IDEALISTA_KEY and IDEALISTA_SECRET in .env
Register at https://developers.idealista.com
"""

import argparse
import base64
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR            = Path(__file__).parent
DASHBOARD_DIR       = BASE_DIR / "dashboard"
OUTPUT_PATH         = DASHBOARD_DIR / "rentals_latest.json"
SEEN_IDS_PATH       = BASE_DIR / "seen_ids_idealista.json"
STATUS_PATH         = BASE_DIR / "scanner_status_idealista.json"
NETLIFY_CONFIG_PATH = BASE_DIR / "netlify_config.json"

IDEALISTA_TOKEN_URL  = "https://api.idealista.com/oauth/token"
IDEALISTA_SEARCH_URL = "https://api.idealista.com/3.5/it/search"
MILANO_LOCATION_ID   = "0-EU-IT-MI"

DAEMON_INTERVAL_SEC = 60 * 60
SOURCE              = "idealista"


# ── Credentials ────────────────────────────────────────────────────────────────

def load_env():
    """Load .env file into os.environ (only sets vars not already present)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def get_credentials() -> tuple:
    load_env()
    return (
        os.environ.get("IDEALISTA_KEY",    "").strip(),
        os.environ.get("IDEALISTA_SECRET", "").strip(),
    )


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_token(key: str, secret: str) -> str:
    creds = base64.b64encode(f"{key}:{secret}".encode()).decode()
    data  = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req   = urllib.request.Request(
        IDEALISTA_TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


# ── Search ─────────────────────────────────────────────────────────────────────

def search_page(token: str, page: int = 1, max_price: int = 0,
                min_size: int = 0, max_size: int = 0,
                min_rooms: int = 0) -> tuple:
    params = {
        "country":      "it",
        "operation":    "rent",
        "propertyType": "homes",
        "locationId":   MILANO_LOCATION_ID,
        "numPage":      page,
        "maxItems":     50,
        "language":     "it",
    }
    if max_price: params["maxPrice"] = max_price
    if min_size:  params["minSize"]  = min_size
    if max_size:  params["maxSize"]  = max_size
    if min_rooms: params["minRooms"] = min_rooms

    data = urllib.parse.urlencode(params).encode()
    req  = urllib.request.Request(
        IDEALISTA_SEARCH_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result.get("elementList", []), result.get("totalPages", 1)


# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_idealista(item: dict):
    from fetch_rentals import match_omi

    listing_id = str(item.get("propertyCode", ""))
    rent       = item.get("price")
    sqm        = item.get("size")

    if not listing_id or not rent or not sqm:
        return None
    try:
        rent = int(rent)
        sqm  = int(sqm)
    except (TypeError, ValueError):
        return None
    if rent <= 0 or sqm <= 0:
        return None

    ask_psqm = round(rent / sqm, 2)

    # Floor
    floor_raw = str(item.get("floor", "")).strip().lower()
    floor = None
    if floor_raw:
        if floor_raw in ("bj", "pt", "rdc", "t", "ground"):
            floor = 0
        elif floor_raw in ("ss", "b", "basement"):
            floor = -1
        else:
            try:
                floor = int(floor_raw)
            except ValueError:
                floor = floor_raw

    neighbourhood = (item.get("neighborhood") or item.get("district") or "").strip()
    address       = (item.get("address") or "").strip()

    url = item.get("url", "")
    if url and not url.startswith("http"):
        url = "https://www.idealista.it" + url

    # Thumbnail
    thumbnail = item.get("thumbnail") or ""
    if not thumbnail:
        imgs = item.get("images") or []
        if imgs:
            first = imgs[0]
            thumbnail = first if isinstance(first, str) else (first.get("url") or "")

    # Condition
    condition = {
        "good":           "Good condition",
        "renew":          "Needs renovation",
        "newdevelopment": "New build",
        "vgood":          "Very good",
    }.get(item.get("status", ""), item.get("status") or "")

    omi = match_omi(neighbourhood)

    # ── Enriched fields ────────────────────────────────────────────────────────
    # floor_n: integer
    floor_n = floor  # already parsed as int/0/-1 above; use directly

    # elevator
    elev_raw = item.get("hasLift") or item.get("lift") or item.get("elevator")
    elevator_bool = None
    if elev_raw is not None:
        elevator_bool = bool(elev_raw) if not isinstance(elev_raw, str) else elev_raw.lower() not in ("false", "no", "0", "")

    # is_external (exterior field already parsed above as item.get("exterior"))
    is_external = item.get("exterior")
    if is_external is not None and isinstance(is_external, str):
        is_external = is_external.lower() in ("true", "yes", "1")

    # energy_class
    energy_class = str(item.get("energyCertification") or item.get("energy") or "").strip().upper() or None

    # bathrooms
    bathrooms = item.get("bathrooms")
    if bathrooms is not None:
        try:
            bathrooms = int(bathrooms)
        except (TypeError, ValueError):
            bathrooms = None

    # has_balcony
    has_balcony = item.get("hasTerraceOrGarden") or item.get("terrace") or item.get("balcony") or item.get("garden")
    if has_balcony is not None and not isinstance(has_balcony, bool):
        has_balcony = bool(has_balcony)

    # has_parking
    has_parking = item.get("parking")
    if has_parking is not None and not isinstance(has_parking, bool):
        has_parking = bool(has_parking)

    # heating_type
    heat_raw = str(item.get("heatingType") or item.get("heating") or "").lower()
    if "autonom" in heat_raw or "individual" in heat_raw:
        heating_type = "autonomous"
    elif "central" in heat_raw:
        heating_type = "centralised"
    elif heat_raw:
        heating_type = "unknown"
    else:
        heating_type = None

    # furnished
    furn_raw = item.get("furnished") or item.get("isFurnished")
    if furn_raw is None:
        furnished = None
    elif isinstance(furn_raw, bool):
        furnished = furn_raw
    else:
        furnished = str(furn_raw).lower() in ("true", "yes", "1", "amueblado")

    # photo_count
    photo_count = len(item.get("images") or [])
    if not photo_count and item.get("thumbnail"):
        photo_count = 1

    # days_on_market
    days_on_market = None
    for df in ("publishDate", "publicationDate", "created"):
        raw_d = item.get(df)
        if raw_d:
            try:
                from datetime import date as _date, datetime as _dt
                pub = _dt.fromisoformat(str(raw_d)[:10]).date()
                days_on_market = (_date.today() - pub).days
                break
            except Exception:
                pass

    # lat/lng for geo enrichment
    latitude  = item.get("latitude")
    longitude = item.get("longitude")

    return {
        "id":                 listing_id,
        "source":             SOURCE,
        "city":               "Milano",
        "city_key":           "milano",
        "neighbourhood":      neighbourhood,
        "address":            address,
        "rent_mo":            rent,
        "sqm":                sqm,
        "ask_psqm":           ask_psqm,
        "spese_condominiali": None,
        "rooms":              item.get("rooms"),
        "floor":              floor,
        "floor_n":            floor_n,
        "is_external":        is_external,
        "elevator":           elevator_bool,
        "condition":          condition,
        "thumbnail":          thumbnail,
        "url":                url,
        "latitude":           latitude,
        "longitude":          longitude,
        "energy_class":       energy_class,
        "year_built":         None,
        "bathrooms":          bathrooms,
        "has_balcony":        has_balcony,
        "has_parking":        has_parking,
        "heating_type":       heating_type,
        "furnished":          furnished,
        "photo_count":        photo_count,
        "days_on_market":     days_on_market,
        "omi":                omi,
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
    }


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_idealista(pages: int = 3, area_names: list = None,
                    max_rent: int = 0, min_sqm: int = 0,
                    max_sqm: int = 0, min_rooms: int = 0) -> list:
    key, secret = get_credentials()
    if not key or not secret:
        print("  [idealista] No credentials — set IDEALISTA_KEY + IDEALISTA_SECRET in .env")
        return []

    print("  [idealista] Authenticating…", end="", flush=True)
    try:
        token = get_token(key, secret)
        print(" ok", flush=True)
    except Exception as e:
        print(f" FAILED: {e}")
        return []

    areas     = [a.strip() for a in (area_names or []) if a.strip()]
    all_items: list = []
    seen_ids:  set  = set()

    for page in range(1, pages + 1):
        print(f"    [idealista] page {page}…", end="", flush=True)
        try:
            items, total_pages = search_page(
                token, page=page,
                max_price=max_rent, min_size=min_sqm,
                max_size=max_sqm,   min_rooms=min_rooms,
            )
        except urllib.error.HTTPError as e:
            print(f" HTTP {e.code}")
            break
        except Exception as e:
            print(f" error: {e}")
            break

        new_this = 0
        for item in items:
            lid = str(item.get("propertyCode", ""))
            if not lid or lid in seen_ids:
                continue
            # Area keyword filter — client-side since API only supports locationId
            if areas:
                haystack = " ".join([
                    item.get("neighborhood") or "",
                    item.get("district")     or "",
                    item.get("address")      or "",
                ]).lower()
                if not any(a.lower() in haystack for a in areas):
                    continue
            parsed = parse_idealista(item)
            if not parsed:
                continue
            seen_ids.add(lid)
            all_items.append(parsed)
            new_this += 1

        max_pg = min(pages, total_pages)
        print(f" +{new_this} ({page}/{max_pg})", flush=True)
        if page >= max_pg:
            break
        time.sleep(1.0)   # rate-limit

    n_fetched = len(all_items)
    print(f"  [fetch]  Idealista: {n_fetched} listings fetched ({pages} pages)")

    # ── Cache-aware geo enrichment ─────────────────────────────────────────
    try:
        import enrichment_cache as _ecache
        from enrich_geo import enrich_batch as _enrich_batch

        _ecache.load()

        new_items = [l for l in all_items if _ecache.get("idealista", l["id"]) is None]
        n_cached  = n_fetched - len(new_items)
        print(f"  [cache]  {n_cached} already enriched, {len(new_items)} new")

        if new_items:
            print(f"  [enrich] Enriching {len(new_items)} new listings in parallel…",
                  flush=True)
            t_enrich = time.time()
            geo_results = _enrich_batch(new_items)
            print(f"  [enrich] Done in {time.time() - t_enrich:.1f}s")
            _ecache.bulk_save(
                [("idealista", l["id"], g) for l, g in zip(new_items, geo_results)]
            )

        # Merge cached geo into every listing
        for listing in all_items:
            cached = _ecache.get("idealista", listing["id"])
            if cached:
                listing.update({k: v for k, v in cached.items() if k != "enriched_at"})

        print(f"  [merge]  {n_fetched} listings enriched and ready")

    except ImportError:
        pass

    return all_items


# ── Seen IDs ───────────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    if SEEN_IDS_PATH.exists():
        try:
            return set(json.loads(SEEN_IDS_PATH.read_text()))
        except Exception:
            pass
    return set()


def save_seen_ids(ids: set):
    SEEN_IDS_PATH.write_text(json.dumps(sorted(ids)))


# ── Status ─────────────────────────────────────────────────────────────────────

def write_status(new_count: int, total_seen: int):
    payload = json.dumps({
        "last_run":   datetime.now().isoformat(timespec="seconds"),
        "new_count":  new_count,
        "total_seen": total_seen,
        "source":     SOURCE,
    })
    STATUS_PATH.write_text(payload)
    # Also write into dashboard/ for Netlify
    (DASHBOARD_DIR / "scanner_status_idealista.json").write_text(payload)


# ── Merged output ──────────────────────────────────────────────────────────────

def write_merged_output(listings: list) -> list:
    """Score listings and merge into rentals_latest.json, preserving other sources."""
    from fetch_rentals import score_all, write_output
    scored = score_all(listings)
    write_output(scored, source=SOURCE)
    return scored


# ── Netlify deploy ─────────────────────────────────────────────────────────────

def netlify_deploy():
    try:
        from fetch_rentals import _netlify_deploy
        _netlify_deploy()
    except Exception as e:
        print(f"  [netlify] deploy error: {e}", file=sys.stderr)


# ── Run cycle ──────────────────────────────────────────────────────────────────

def run_once(args) -> list:
    area_names = [a.strip() for a in args.areas.split(",") if a.strip()] if args.areas else []
    raw = fetch_idealista(
        pages=args.pages,
        area_names=area_names,
        max_rent=getattr(args, "max_rent",  0) or 0,
        min_sqm=getattr(args,  "min_sqm",   0) or 0,
        max_sqm=getattr(args,  "max_sqm",   0) or 0,
        min_rooms=getattr(args,"min_rooms",  0) or 0,
    )
    if not raw:
        print("  [idealista] No listings fetched.")
        return []

    scored = write_merged_output(raw)

    seen         = load_seen_ids()
    new_listings = [l for l in scored if l["id"] not in seen]
    seen.update(l["id"] for l in scored)
    save_seen_ids(seen)

    write_status(len(new_listings), len(seen))
    print(f"  ✓ [idealista] {len(scored)} merged · {len(new_listings)} new")

    if NETLIFY_CONFIG_PATH.exists() and scored:
        netlify_deploy()

    return new_listings


# ── Daemon ─────────────────────────────────────────────────────────────────────

def daemon_loop(args):
    log_path = BASE_DIR / "scanner_idealista.log"
    log_fh   = open(log_path, "a", buffering=1)

    def log(msg: str):
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [idealista] {msg}"
        print(line, flush=True)
        print(line, file=log_fh, flush=True)

    log(f"Daemon started — interval {DAEMON_INTERVAL_SEC // 60} min")

    while True:
        log("Running scan…")
        try:
            new_listings = run_once(args)
            log(f"Scan done — {len(new_listings)} new")
        except KeyboardInterrupt:
            log("Interrupted — stopping.")
            log_fh.close()
            raise
        except Exception as e:
            log(f"✗ Error: {e}\n{traceback.format_exc()}")

        log(f"Sleeping {DAEMON_INTERVAL_SEC // 60} min…")
        try:
            time.sleep(DAEMON_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Interrupted during sleep — stopping.")
            log_fh.close()
            raise


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Fetch Milano rentals from Idealista API.")
    p.add_argument("--pages",     type=int,   default=3)
    p.add_argument("--areas",     type=str,   default="")
    p.add_argument("--max-rent",  type=int,   default=0)
    p.add_argument("--min-sqm",   type=int,   default=0)
    p.add_argument("--max-sqm",   type=int,   default=0)
    p.add_argument("--min-rooms", type=int,   default=0)
    p.add_argument("--daemon",    action="store_true")
    return p.parse_args()


def main():
    args       = parse_args()
    key, secret = get_credentials()
    if not key or not secret:
        print("  [idealista] IDEALISTA_KEY / IDEALISTA_SECRET not set in .env — skipping")
        sys.exit(0)

    print(f"\n{'─'*52}")
    print(f"  Idealista Scorer — rental fetch (Milano)")
    print(f"  Pages : {args.pages}")
    if args.areas:    print(f"  Areas : {args.areas}")
    print(f"  Mode  : {'daemon' if args.daemon else 'one-shot'}")
    print(f"{'─'*52}\n")

    if args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
