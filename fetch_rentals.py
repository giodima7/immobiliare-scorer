#!/usr/bin/env python3
"""
fetch_rentals.py
────────────────
Fetches Milano rental listings from Immobiliare.it via nodriver,
scores each one against OMI rent benchmarks, and writes to
dashboard/rentals_latest.json.

Usage (one-shot):
    python3 fetch_rentals.py
    python3 fetch_rentals.py --pages 5
    python3 fetch_rentals.py --areas navigli,brera --max-rent 2000

Daemon mode (loops every 60 min, tracks new listings only):
    python3 fetch_rentals.py --daemon
    python3 fetch_rentals.py --daemon --email
"""

import argparse
import asyncio
import json
import re as _re
import sys
import time
import unicodedata
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import nodriver as uc

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
DASHBOARD_DIR    = BASE_DIR / "dashboard"
SEEN_IDS_PATH    = BASE_DIR / "seen_ids.json"
STATUS_PATH      = BASE_DIR / "scanner_status.json"
DIGEST_SENT_PATH = BASE_DIR / ".digest_sent_date"
OUTPUT_PATH      = DASHBOARD_DIR / "rentals_latest.json"

EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
CITY_KEY   = "milano"
CITY_LABEL = "Milano"
CITY_SLUG  = "milano"

DAEMON_INTERVAL_SEC = 60 * 60   # 60 minutes
DIGEST_HOUR         = 8          # send daily digest at 08:xx local time

# ── OMI rent data (Milano, 2° sem 2025) ────────────────────────────────────────
# rmin/rmax: €/m²/MONTH
OMI_RENT = {
    "brera":            dict(fascia="A", rmin=28.0, rmax=42.0),
    "duomo":            dict(fascia="A", rmin=26.0, rmax=40.0),
    "centro":           dict(fascia="A", rmin=26.0, rmax=40.0),
    "porta venezia":    dict(fascia="A", rmin=18.0, rmax=28.0),
    "buenos aires":     dict(fascia="A", rmin=18.0, rmax=28.0),
    "isola":            dict(fascia="A", rmin=18.0, rmax=28.0),
    "navigli":          dict(fascia="A", rmin=17.0, rmax=26.0),
    "porta ticinese":   dict(fascia="A", rmin=17.0, rmax=26.0),
    "tortona":          dict(fascia="A", rmin=17.0, rmax=26.0),
    "porta romana":     dict(fascia="A", rmin=16.0, rmax=24.0),
    "moscova":          dict(fascia="A", rmin=20.0, rmax=30.0),
    "garibaldi":        dict(fascia="A", rmin=20.0, rmax=32.0),
    "corso como":       dict(fascia="A", rmin=20.0, rmax=32.0),
    "arco della pace":  dict(fascia="A", rmin=18.0, rmax=28.0),
    "guastalla":        dict(fascia="A", rmin=20.0, rmax=30.0),
    "ticinese":         dict(fascia="A", rmin=17.0, rmax=26.0),
    "corso genova":     dict(fascia="A", rmin=17.0, rmax=26.0),
    "indipendenza":     dict(fascia="A", rmin=18.0, rmax=28.0),
    "piave":            dict(fascia="A", rmin=18.0, rmax=28.0),
    "tricolore":        dict(fascia="A", rmin=18.0, rmax=28.0),
    "vincenzo monti":   dict(fascia="A", rmin=18.0, rmax=28.0),
    "washington":       dict(fascia="A", rmin=18.0, rmax=28.0),
    "solari":           dict(fascia="A", rmin=17.0, rmax=26.0),
    "paolo sarpi":      dict(fascia="A", rmin=16.0, rmax=25.0),
    "centrale":         dict(fascia="A", rmin=17.0, rmax=25.0),
    "amendola":         dict(fascia="A", rmin=16.0, rmax=25.0),
    "buonarroti":       dict(fascia="A", rmin=16.0, rmax=25.0),
    "melchiorre":       dict(fascia="A", rmin=15.0, rmax=23.0),
    "città studi":      dict(fascia="B", rmin=12.0, rmax=18.0),
    "citta studi":      dict(fascia="B", rmin=12.0, rmax=18.0),
    "lambrate":         dict(fascia="B", rmin=12.0, rmax=18.0),
    "loreto":           dict(fascia="B", rmin=12.0, rmax=18.0),
    "medaglie d'oro":   dict(fascia="B", rmin=13.0, rmax=20.0),
    "bovisa":           dict(fascia="B", rmin=10.0, rmax=15.0),
    "dergano":          dict(fascia="B", rmin=10.0, rmax=15.0),
    "affori":           dict(fascia="B", rmin=9.0,  rmax=14.0),
    "niguarda":         dict(fascia="B", rmin=9.0,  rmax=14.0),
    "bicocca":          dict(fascia="B", rmin=11.0, rmax=17.0),
    "via padova":       dict(fascia="B", rmin=10.0, rmax=15.0),
    "feltre":           dict(fascia="B", rmin=12.0, rmax=18.0),
    "cimiano":          dict(fascia="B", rmin=12.0, rmax=18.0),
    "san siro":         dict(fascia="B", rmin=11.0, rmax=17.0),
    "montenero":        dict(fascia="B", rmin=13.0, rmax=20.0),
    "argonne":          dict(fascia="B", rmin=12.0, rmax=18.0),
    "corsica":          dict(fascia="B", rmin=12.0, rmax=18.0),
    "maggiolina":       dict(fascia="B", rmin=12.0, rmax=18.0),
    "monte rosa":       dict(fascia="B", rmin=12.0, rmax=18.0),
    "lotto":            dict(fascia="B", rmin=12.0, rmax=18.0),
    "casoretto":        dict(fascia="B", rmin=11.0, rmax=17.0),
    "precotto":         dict(fascia="B", rmin=11.0, rmax=17.0),
    "rovereto":         dict(fascia="B", rmin=11.0, rmax=17.0),
    "turro":            dict(fascia="B", rmin=11.0, rmax=17.0),
    "parco trotter":    dict(fascia="B", rmin=11.0, rmax=17.0),
    "pasteur":          dict(fascia="B", rmin=11.0, rmax=17.0),
    "udine":            dict(fascia="B", rmin=11.0, rmax=17.0),
    "ghisolfa":         dict(fascia="B", rmin=10.0, rmax=15.0),
    "cenisio":          dict(fascia="B", rmin=11.0, rmax=17.0),
    "plebisciti":       dict(fascia="B", rmin=11.0, rmax=17.0),
    "pezzotti":         dict(fascia="B", rmin=10.0, rmax=15.0),
    "ca granda":        dict(fascia="B", rmin=10.0, rmax=15.0),
    "ca' granda":       dict(fascia="B", rmin=10.0, rmax=15.0),
    "tre castelli":     dict(fascia="B", rmin=10.0, rmax=15.0),
    "villa san giovanni": dict(fascia="B", rmin=10.0, rmax=15.0),
    "siena":            dict(fascia="B", rmin=10.0, rmax=15.0),
    "famagosta":        dict(fascia="C", rmin=9.0,  rmax=14.0),
    "lorenteggio":      dict(fascia="C", rmin=9.0,  rmax=14.0),
    "giambellino":      dict(fascia="C", rmin=9.0,  rmax=14.0),
    "rogoredo":         dict(fascia="C", rmin=8.0,  rmax=13.0),
    "mecenate":         dict(fascia="C", rmin=8.0,  rmax=13.0),
    "forlanini":        dict(fascia="C", rmin=8.0,  rmax=13.0),
    "quarto oggiaro":   dict(fascia="C", rmin=7.0,  rmax=11.0),
    "comasina":         dict(fascia="C", rmin=7.0,  rmax=11.0),
    "bruzzano":         dict(fascia="C", rmin=7.0,  rmax=11.0),
    "romolo":           dict(fascia="C", rmin=9.0,  rmax=14.0),
    "bonola":           dict(fascia="C", rmin=7.5,  rmax=12.0),
    "trenno":           dict(fascia="C", rmin=7.5,  rmax=12.0),
    "vigentino":        dict(fascia="C", rmin=8.0,  rmax=13.0),
    "corvetto":         dict(fascia="C", rmin=8.0,  rmax=13.0),
    "ripamonti":        dict(fascia="C", rmin=9.0,  rmax=14.0),
    "gratosoglio":      dict(fascia="C", rmin=7.0,  rmax=11.0),
    "bisceglie":        dict(fascia="C", rmin=8.0,  rmax=13.0),
    "certosa":          dict(fascia="C", rmin=7.5,  rmax=12.0),
    "uptown":           dict(fascia="C", rmin=8.0,  rmax=13.0),
    "cascina merlata":  dict(fascia="C", rmin=8.0,  rmax=13.0),
    "quartiere adriano": dict(fascia="C", rmin=7.5, rmax=12.0),
    "adriano":          dict(fascia="C", rmin=7.5,  rmax=12.0),
    "musocco":          dict(fascia="C", rmin=7.5,  rmax=12.0),
    "cermenate":        dict(fascia="C", rmin=8.0,  rmax=13.0),
    "abbiategrasso":    dict(fascia="C", rmin=8.0,  rmax=13.0),
}

FALLBACK = dict(fascia="B", rmin=12.0, rmax=18.0)


# ── URL helpers ────────────────────────────────────────────────────────────────

def to_url_slug(name: str) -> str:
    """
    Convert a neighbourhood name to an Immobiliare.it URL path segment.
    e.g. "Porta Venezia" → "porta-venezia", "Città Studi" → "citta-studi"
    """
    s = unicodedata.normalize("NFKD", name.strip().lower())
    s = s.encode("ascii", "ignore").decode("ascii")
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def build_rental_url(page: int, area_slug: str = None,
                     max_rent: int = 0, min_sqm: int = 0, max_sqm: int = 0,
                     min_rooms: int = 0) -> str:
    """Build a fully parameterised Immobiliare.it rental search URL."""
    base = f"https://www.immobiliare.it/affitto-case/{CITY_SLUG}/"
    if area_slug:
        base += f"{area_slug}/"
    params = {}
    if max_rent:  params["prezzoMassimo"]    = max_rent
    if min_sqm:   params["superficieMinima"] = min_sqm
    if max_sqm:   params["superficieMassima"]= max_sqm
    if min_rooms: params["localiMinimo"]     = min_rooms
    if page > 1:  params["pag"]              = page
    return base + ("?" + urlencode(params) if params else "")


# ── OMI matching ───────────────────────────────────────────────────────────────

def match_omi(neighbourhood: str) -> dict:
    """Match neighbourhood to the most specific OMI zone (longest keyword wins)."""
    nb = neighbourhood.lower().strip()
    best_key, best_len = None, 0
    for keyword, data in OMI_RENT.items():
        if nb == keyword or keyword in nb:
            if len(keyword) > best_len:
                best_key, best_len = keyword, len(keyword)
    if best_key:
        return {"zone": best_key, **OMI_RENT[best_key]}
    return {"zone": "city average", **FALLBACK}


# ── Scoring ────────────────────────────────────────────────────────────────────

def score_rental(listing: dict, all_listings: list) -> dict:
    """Score a rental listing vs OMI rent mid-range."""
    omi      = listing["omi"]
    ask_psqm = listing.get("ask_psqm", 0)   # €/m²/month
    if not ask_psqm or ask_psqm <= 0:
        return {}

    omi_rent_mid = (omi["rmin"] + omi["rmax"]) / 2
    vs_omi = (ask_psqm - omi_rent_mid) / omi_rent_mid   # negative = cheaper

    # Rent score: −20% below OMI → 100, at mid → 50, +20% above → 0
    rent_score = max(0.0, min(100.0, 50 - vs_omi * 250))

    # Within-fascia position by asking €/m²/mo
    fascia = omi["fascia"]
    peers  = sorted(
        l["ask_psqm"] for l in all_listings
        if l.get("omi", {}).get("fascia") == fascia and (l.get("ask_psqm") or 0) > 0
    )
    if peers:
        rank       = sum(1 for v in peers if v <= ask_psqm)
        fascia_pct = round(rank / len(peers) * 100)
    else:
        fascia_pct = 50
    fascia_score = 100 - fascia_pct   # cheaper = higher score

    total = round(rent_score * 0.60 + fascia_score * 0.40)

    vs_omi_label = (
        f"{abs(vs_omi*100):.0f}% below OMI" if vs_omi < -0.10
        else "at OMI benchmark"              if abs(vs_omi) < 0.10
        else f"{vs_omi*100:.0f}% above OMI"
    )
    fascia_label = (
        f"cheap in fascia {fascia}" if fascia_pct <= 33
        else f"mid in fascia {fascia}" if fascia_pct <= 66
        else f"expensive in fascia {fascia}"
    )

    return {
        "omi_zone":        omi["zone"],
        "omi_fascia":      fascia,
        "omi_rmin":        omi["rmin"],
        "omi_rmax":        omi["rmax"],
        "omi_rent_mid":    round(omi_rent_mid, 1),
        "vs_omi_rent_pct": round(vs_omi * 100, 1),
        "vs_omi_label":    vs_omi_label,
        "fascia_pct":      fascia_pct,
        "fascia_label":    fascia_label,
        "score_rent":      round(rent_score),
        "score_fascia":    round(fascia_score),
        "score_total":     total,
    }


# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_rental(item: dict) -> Optional[dict]:
    """Extract and normalise a rental listing from __NEXT_DATA__ result."""
    re_data    = item.get("realEstate", {})
    props      = re_data.get("properties", [{}])
    prop       = props[0] if props else {}
    location   = prop.get("location", {})
    price_data = re_data.get("price", {})

    # Monthly rent €
    rent = price_data.get("value")
    if not rent:
        return None
    try:
        rent = int(rent)
    except (TypeError, ValueError):
        return None
    if rent <= 0:
        return None

    # Surface m²
    sqm_raw = prop.get("surface") or prop.get("surfaceValue")
    sqm = None
    if sqm_raw is not None:
        try:
            sqm = int(_re.sub(r"[^\d]", "", str(sqm_raw)))
        except (ValueError, AttributeError):
            pass
    if not sqm or sqm <= 0:
        return None

    ask_psqm = round(rent / sqm, 2)   # €/m²/month

    # Condominium fees (spese condominiali) — try several possible field names
    spese = None
    for field in ("expenses", "condominiumFees", "monthlyCharges"):
        raw = prop.get(field) or price_data.get(field)
        if raw is not None:
            try:
                spese = int(_re.sub(r"[^\d]", "", str(raw)))
                break
            except (ValueError, AttributeError):
                pass

    # Rooms
    rooms_raw = prop.get("rooms")
    try:
        rooms = int(rooms_raw) if rooms_raw is not None else None
    except (TypeError, ValueError):
        rooms = None

    # Neighbourhood
    def _str_or_name(val):
        if isinstance(val, dict):
            return val.get("name", "")
        return str(val).strip() if val else ""

    microzone     = _str_or_name(location.get("microzone"))
    macrozone     = _str_or_name(location.get("macrozone"))
    neighbourhood = microzone or macrozone or location.get("city", "")

    address   = location.get("address", "")
    latitude  = location.get("latitude")
    longitude = location.get("longitude")

    # Floor
    floor_raw = prop.get("floor")
    if isinstance(floor_raw, dict):
        floor = floor_raw.get("abbreviation")
    else:
        floor = str(floor_raw).strip() if floor_raw is not None else None

    elevator      = prop.get("elevator") or prop.get("hasElevator")
    condition_raw = (
        prop.get("ga4Condition")
        or prop.get("condition")
        or re_data.get("typology", {}).get("name", "")
    )

    listing_id = str(re_data.get("id", ""))
    url = f"https://www.immobiliare.it/annunci/{listing_id}/" if listing_id else ""

    # Thumbnail — first photo is inside properties[0].multimedia.photos
    # (realEstate.multimedia is empty on listing-search pages)
    photos    = prop.get("multimedia", {}).get("photos", [])
    thumbnail = None
    if photos:
        first   = photos[0]
        urls    = first.get("urls", {})
        thumbnail = (
            urls.get("medium") or urls.get("large") or
            urls.get("small") or first.get("url") or first.get("src")
        )

    omi = match_omi(neighbourhood)

    return {
        "id":                 listing_id,
        "city":               CITY_LABEL,
        "city_key":           CITY_KEY,
        "title":              re_data.get("title", ""),
        "neighbourhood":      neighbourhood,
        "address":            address,
        "latitude":           latitude,
        "longitude":          longitude,
        "rent_mo":            rent,         # monthly rent €
        "sqm":                sqm,
        "ask_psqm":           ask_psqm,     # €/m²/month
        "spese_condominiali": spese,        # condominium fees €/mo (or None)
        "rooms":              rooms,
        "floor":              floor,
        "elevator":           elevator,
        "condition":          condition_raw,
        "thumbnail":          thumbnail,    # first listing photo URL (or None)
        "url":                url,
        "omi":                omi,           # dropped before JSON export
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
    }


# ── Fetch (nodriver) ───────────────────────────────────────────────────────────

async def _fetch_async(pages: int, area_slugs: list, max_rent: int,
                       min_sqm: int, max_sqm: int, min_rooms: int,
                       delay: float, browser) -> list:
    """
    Navigate Immobiliare.it rental pages and return parsed listing dicts.

    Filters (max_rent, min_sqm, max_sqm) are embedded directly in the URL so
    the site pre-filters results — fewer pages needed.

    If area_slugs is non-empty, one URL series is fetched per area slug
    (e.g. /affitto-case/milano/navigli/?prezzoMassimo=2000).
    Results are deduplicated by listing ID.
    """
    all_items = []
    seen_ids  = set()   # global dedup across all areas

    # None sentinel = no area path → fetch whole city
    targets = area_slugs if area_slugs else [None]

    for area_slug in targets:
        label = area_slug or "all Milano"
        print(f"\n    area: {label}", end="", flush=True)

        area_ids_seen = set()   # IDs seen so far within THIS area's pages
        # canonical_slug: resolved by the site on the first request (may differ
        # from area_slug when the site redirects e.g. porta-romana →
        # porta-romana-medaglie-d-oro, which drops pag= from the URL).
        # We use this resolved slug for pages 2, 3, … so pagination works.
        canonical_slug = area_slug

        for page in range(1, pages + 1):
            url = build_rental_url(page, canonical_slug, max_rent, min_sqm, max_sqm, min_rooms)
            tab = await browser.get(url)
            await asyncio.sleep(delay)

            # ── Detect redirect on page 1 ──────────────────────────────────────
            # Immobiliare.it sometimes rewrites a neighbourhood slug to a more
            # specific sub-area URL, stripping the pag= param in the process.
            # After the first page load we read the actual href and update
            # canonical_slug so that subsequent page requests go to the right URL.
            if page == 1 and area_slug:
                try:
                    actual_href = await tab.evaluate("window.location.href")
                    if actual_href:
                        m = _re.search(r'/milano/([^/?#]+)', actual_href)
                        if m:
                            resolved = m.group(1)
                            if resolved != canonical_slug:
                                print(f" [→{resolved}]", end="", flush=True)
                                canonical_slug = resolved
                except Exception:
                    pass

            try:
                raw  = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
                nd   = json.loads(raw)
                data = nd["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
            except Exception as e:
                print(f"\n  ⚠  Could not parse __NEXT_DATA__ (area={label}, page={page}): {e}")
                break

            results = data.get("results", [])
            if not results:
                print(f" (empty, done)", end="", flush=True)
                break

            # Extract raw IDs from this page before full parsing
            page_ids = {
                str(item.get("realEstate", {}).get("id", ""))
                for item in results
                if item.get("realEstate", {}).get("id")
            }

            # If every ID on this page was already seen in a previous page of this
            # area, the site is recycling page 1 content (pag= ignored for this slug).
            if page > 1 and page_ids and page_ids.issubset(area_ids_seen):
                print(f" (repeat page, done)", end="", flush=True)
                break

            area_ids_seen.update(page_ids)

            new_this_page = 0
            for item in results:
                parsed = parse_rental(item)
                if not parsed or parsed["id"] in seen_ids:
                    continue
                seen_ids.add(parsed["id"])
                all_items.append(parsed)
                new_this_page += 1

            # maxPages: guard against None or 0 coming back from the site
            site_max = data.get("maxPages") or pages
            max_pg   = min(pages, site_max)
            print(f" p{page}/{max_pg}(+{new_this_page})", end="", flush=True)
            if page >= max_pg:
                break

    return all_items


def fetch_rentals(pages: int = 3, area_names: list = None, max_rent: int = 0,
                  min_sqm: int = 0, max_sqm: int = 0, min_rooms: int = 0,
                  delay: float = 2.5) -> list:
    """
    One-shot fetch of Milano rental listings using Edge via nodriver.
    area_names: list of display names, e.g. ["Navigli", "Brera"].
                Each is converted to a URL slug and fetched as a separate URL series.
    """
    area_slugs = [to_url_slug(a) for a in (area_names or []) if a.strip()]
    desc = ", ".join(area_names) if area_names else "all Milano"
    print(f"  Fetching rentals ({desc})...", end="", flush=True)

    async def _run():
        browser = await uc.start(
            browser_executable_path=EDGE_PATH,
            headless=False,
            lang="it-IT",
        )
        try:
            items = await _fetch_async(
                pages, area_slugs, max_rent, min_sqm, max_sqm, min_rooms, delay, browser
            )
        finally:
            browser.stop()
        return items

    items = asyncio.run(_run())
    print(f"\n  → {len(items)} rentals total")
    return items


# ── Scoring pass ───────────────────────────────────────────────────────────────

def score_all(raw: list) -> list:
    scored = []
    for l in raw:
        s = score_rental(l, raw)
        scored.append({**l, **s})
    scored.sort(key=lambda x: x.get("score_total", 0), reverse=True)
    return scored


# ── Persistence helpers ────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    if SEEN_IDS_PATH.exists():
        try:
            return set(json.loads(SEEN_IDS_PATH.read_text()))
        except Exception:
            pass
    return set()


def save_seen_ids(ids: set):
    SEEN_IDS_PATH.write_text(json.dumps(sorted(ids)))


def write_output(listings: list):
    """Write scored listings to dashboard/rentals_latest.json (drop internal omi dict)."""
    DASHBOARD_DIR.mkdir(exist_ok=True)
    clean = [{k: v for k, v in l.items() if k != "omi"} for l in listings]
    OUTPUT_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2))


def write_status(new_count: int, total_seen: int):
    payload = json.dumps({
        "last_run":   datetime.now().isoformat(timespec="seconds"),
        "new_count":  new_count,
        "total_seen": total_seen,
    })
    STATUS_PATH.write_text(payload)
    # Also write into dashboard/ so Netlify picks it up
    (DASHBOARD_DIR / "scanner_status.json").write_text(payload)


# ── Run cycle ──────────────────────────────────────────────────────────────────

def run_once(args) -> list:
    """Execute one fetch-score-write cycle. Returns newly seen listings."""
    area_names = [a.strip() for a in args.areas.split(",") if a.strip()] if args.areas else []
    raw = fetch_rentals(
        pages=args.pages,
        area_names=area_names,
        max_rent=args.max_rent   or 0,
        min_sqm=args.min_sqm     or 0,
        max_sqm=args.max_sqm     or 0,
        min_rooms=args.min_rooms or 0,
        delay=args.delay,
    )
    if not raw:
        print("  ✗ No rentals fetched.")
        write_status(new_count=0, total_seen=len(load_seen_ids()))
        return []

    scored = score_all(raw)
    write_output(scored)

    seen         = load_seen_ids()
    new_listings = [l for l in scored if l["id"] not in seen]
    seen.update(l["id"] for l in scored)
    save_seen_ids(seen)

    write_status(len(new_listings), len(seen))
    print(f"  ✓ {len(scored)} rentals written · {len(new_listings)} new")
    return new_listings


# ── Daily digest helpers ───────────────────────────────────────────────────────

def should_send_digest() -> bool:
    if datetime.now().hour != DIGEST_HOUR:
        return False
    today = str(date.today())
    if DIGEST_SENT_PATH.exists() and DIGEST_SENT_PATH.read_text().strip() == today:
        return False
    return True


def mark_digest_sent():
    DIGEST_SENT_PATH.write_text(str(date.today()))


# ── Git push helper ────────────────────────────────────────────────────────────

def _git_push():
    """Commit updated dashboard JSON files and push so Netlify redeploys."""
    import subprocess
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    files = [
        str(DASHBOARD_DIR / "rentals_latest.json"),
        str(DASHBOARD_DIR / "scanner_status.json"),
    ]
    # Only add files that actually exist
    existing = [f for f in files if Path(f).exists()]
    if not existing:
        return
    try:
        subprocess.run(["git", "add"] + existing, cwd=str(BASE_DIR), check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(BASE_DIR)
        )
        if result.returncode == 0:
            print("  [git] nothing to commit — data unchanged")
            return
        subprocess.run(
            ["git", "commit", "-m", f"scanner: update rentals {ts}"],
            cwd=str(BASE_DIR), check=True
        )
        subprocess.run(["git", "push"], cwd=str(BASE_DIR), check=True)
        print(f"  [git] pushed dashboard data → Netlify redeploy triggered")
    except subprocess.CalledProcessError as e:
        print(f"  [git] push failed: {e}", file=sys.stderr)


# ── Daemon ─────────────────────────────────────────────────────────────────────

def daemon_loop(args):
    from email_digest import send_digest, load_config as load_email_config
    print(f"\n  Rental scanner daemon started")
    print(f"  Interval : every {DAEMON_INTERVAL_SEC // 60} min")
    print(f"  Output   : {OUTPUT_PATH}")
    print()

    while True:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running scan...")
        try:
            new_listings = run_once(args)

            # Re-read email config on every iteration so dashboard changes take effect
            email_cfg = load_email_config()
            digest_hour = int(email_cfg.get("digest_hour") or DIGEST_HOUR)

            def _should_send():
                if datetime.now().hour != digest_hour:
                    return False
                today = str(date.today())
                return not (DIGEST_SENT_PATH.exists()
                            and DIGEST_SENT_PATH.read_text().strip() == today)

            # Send if: email enabled in config OR --email flag passed on CLI
            want_email = email_cfg.get("enabled", False) or args.email
            if want_email and new_listings and _should_send():
                send_digest(new_listings, email_cfg)
                mark_digest_sent()

            # Push updated JSON to git so Netlify redeploys with fresh data
            if args.git_push:
                _git_push()

        except Exception as e:
            print(f"  ✗ Scan error: {e}", file=sys.stderr)
            write_status(new_count=0, total_seen=len(load_seen_ids()))

        print(f"  Sleeping {DAEMON_INTERVAL_SEC // 60} min…", flush=True)
        time.sleep(DAEMON_INTERVAL_SEC)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch Milano rental listings and score against OMI rent benchmarks."
    )
    p.add_argument("--pages",    type=int,   default=3,
                   help="Pages to fetch (25 listings/page, default 3)")
    p.add_argument("--areas",    type=str,   default="",
                   help="Comma-separated area keywords, e.g. navigli,brera,isola")
    p.add_argument("--max-rent", type=int,   default=0,    help="Max monthly rent €")
    p.add_argument("--min-sqm",  type=int,   default=0,    help="Min surface m²")
    p.add_argument("--max-sqm",  type=int,   default=0,    help="Max surface m²")
    p.add_argument("--min-rooms",type=int,   default=0,    help="Min number of rooms")
    p.add_argument("--delay",    type=float, default=2.5,  help="Seconds between page loads (default 2.5)")
    p.add_argument("--daemon",   action="store_true",
                   help="Loop forever, fetching every 60 minutes")
    p.add_argument("--email",    action="store_true",
                   help="Send daily email digest (requires --daemon; see email_digest.py)")
    p.add_argument("--git-push", action="store_true",
                   help="After each scan, git-commit dashboard/rentals_latest.json and push "
                        "(use with --daemon so Netlify auto-redeploys with fresh data)")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"\n{'─'*52}")
    print(f"  Immobiliare Scorer — rental fetch (Milano)")
    print(f"  Pages   : {args.pages} (~{args.pages * 25} listings max)")
    if args.areas:
        print(f"  Areas   : {args.areas}")
    if args.max_rent:
        print(f"  Max rent : €{args.max_rent}/mo")
    if args.min_rooms:
        print(f"  Min rooms: {args.min_rooms}+")
    print(f"  Mode    : {'daemon (every %d min)' % (DAEMON_INTERVAL_SEC // 60) if args.daemon else 'one-shot'}")
    if args.git_push:
        print(f"  Git push: enabled (→ Netlify redeploy after each scan)")
    print(f"{'─'*52}\n")

    if args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
