"""Casa.it — the Milan rentals that none of our other sites carry.

The user, 2026-10-27: "I don't want only private listings, I want all those
that aren't on Idealista, Immobiliare or Subito", with every field the other
sites fill. Measured that day: 12,634 Milan rentals up to €3,000 on Casa.it
(508 from private landlords); of 83 private ones in scope, 28 % were on none
of our sites. Only those are kept — plus a private landlord's copy of a flat
an agency lists elsewhere (the way round the fee: one card on the dashboard,
"private on Casa.it", widgets.js _dedupListings). A copy of a listing we
already have is dropped (same_as_known: comps_engine's duplicate tests).

Casa.it sits behind DataDome: plain HTTP gets a 403, a real Chrome passes.
A search shows at most 80 pages of 20, so the city is read zone by zone
(ZONES, the site's 37; ~630 pages in all). One pass every CASAIT_EVERY_SEC
inside the scan window, from either daemon's pause between slots
(fetch_rentals._between_slots), in its own minimized Chrome profile, at
most state["budget"] page loads:
  1. new listings: newest first, until the pages are ones we know;
  2. the zone swept longest ago, from where the last pass stopped — every
     zone every few days; a listing nobody saw for STALE_DAYS is hidden;
  3. listing pages (DETAILS_PER_PASS) of kept listings not read yet: full
     description, condition, furnishing, heating, condo fees, deposit,
     contract, year, extras, the whole gallery and the floor plan —
     stored in the detail cache, applied like the other sites' pages
     (listing_details.apply_details).
DataDome challenged the profile after ~100 pages in 40 min on 2026-10-27
(first tests, 4–8 s apart): pages are 8–15 s apart, a block pauses the site
6 h and halves the budget, a clean pass raises it again.

Rows go into milano_rentals_latest.json as source "casait" (ids "ca_<n>")
through the same write_output, enrichment, scoring and digest as the other
sites. Location and text checks reuse fetch_subito.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import random
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import fetch_subito as fs

BASE_DIR    = Path(__file__).resolve().parent
SOURCE      = "casait"
SEARCH_BASE = "https://www.casa.it/affitto/residenziale/milano/"
SCOPE_QUERY = "priceMax=3000"         # 12,634 of 14,195 on 2026-10-27; rooms are filtered on parse
# The site's own zones (its search page, 2026-10-27).
ZONES = (
    "abbiategrasso-chiesa-rossa-gratosoglio-ticinello", "adriano-crescenzago-parco-lambro-cimiano",
    "affori-bovisa", "arco-della-pace-sempione", "baggio-bisceglie-olmi",
    "bande-nere-inganni-lorenteggio", "barona-famagosta-san-cristoforo",
    "bicocca-ca-granda-parco-nord", "buenos-aires-porta-venezia",
    "cascina-merlata-cimitero-maggiore-viale-certosa", "casoretto-nolo", "cenisio-sarpi",
    "centrale-repubblica", "centro-duomo-brera", "citta-studi-corsica-susa",
    "citylife-fiera-lotto-portello", "comasina-quarto-oggiaro", "corso-lodi-corvetto-rogoredo",
    "corso-magenta", "dergano-maciachini-farini-maggiolina-zara", "figino-quinto-romano",
    "forlanini-mecenate-ponte-lambro", "gallaratese-qt8-trenno", "garibaldi-isola",
    "gorla-greco-precotto", "guastalla-palestro", "lambrate", "macconago-quintosole",
    "navigli-porta-genova", "porta-romana-risorgimento", "porta-vittoria-molise",
    "ripamonti-vigentino", "san-siro-san-carlo", "solari-tortona-washington", "ticinese",
)

CASAIT_EVERY_SEC   = 60 * 60          # a pass an hour inside the window: ~10 a day
BLOCK_PAUSE_SEC    = 6 * 3600
ZONE_MAX_PAGES     = 80               # the site's cap (1,600 listings)
DISCOVER_MAX_PAGES = 4
DISCOVER_STOP_KNOWN = 15              # of a page's 20: the rest we have already
DETAILS_PER_PASS   = 10
BUDGET_START, BUDGET_MIN, BUDGET_MAX = 30, 10, 45   # page loads a pass
PAGE_DELAY_S       = (8.0, 15.0)
STALE_DAYS         = 8                # unseen this long (two rotations): hidden

STATE_PATH = BASE_DIR / ".casait_state.json"
LOCK_PATH  = BASE_DIR / ".casait.lock"
LOG_PATH   = BASE_DIR / "casait_scans_milano.jsonl"

IMG_BASE  = "https://images-1.casa.it"
IMG_FULL  = "/1024x768"               # JPEG at every size; what photo_eval reads
IMG_THUMB = "/360x265"

# DataDome's interstitial says "Please enable JS and disable any ad blocker".
_BLOCK_RX = re.compile(r"captcha|enable js|ad blocker|robot|access denied|accesso negato|"
                       r"too many requests|troppe richieste", re.I)


# ── State ────────────────────────────────────────────────────────────────────

def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def casait_due() -> bool:
    """A pass an hour — every half hour inside a one-off catch-up window."""
    import scan_window
    s = _state()
    every = CASAIT_EVERY_SEC // 2 if scan_window.in_extra() else CASAIT_EVERY_SEC
    return time.time() >= max((s.get("at") or 0) + every, s.get("pause_until") or 0)


# Casa.it's zones holding the user's focus areas (2026-10-30: Corso Genova,
# Porta Romana, Crocetta, Guastalla, Missori, Solari): swept first each
# time the window opens, with any the kept listings point to.
FOCUS_ZONES = ("porta-romana-risorgimento", "ticinese", "navigli-porta-genova", "guastalla-palestro",
               "centro-duomo-brera", "solari-tortona-washington")


def next_zone(state: dict) -> tuple[str, int]:
    """(zone, first page): a focus zone not swept since the window opened
    (resumed where a pass left it), else the zone a pass left half-read,
    else the zone swept longest ago (never swept first, in ZONES order)."""
    cur = state.get("cursor") or {}
    swept = state.get("swept") or {}
    try:
        import scan_window
        from focus_areas import priority_slugs
        opened = scan_window.opened_at()
        focus = set(FOCUS_ZONES) | {s.removeprefix("casait-") for s in priority_slugs(SOURCE, min_n=3)}
        due = [z for z in ZONES if z in focus and (opened is None or swept.get(z, 0) < opened.timestamp())]
        if due:
            z = cur["zone"] if cur.get("zone") in due else min(due, key=lambda z: swept.get(z, 0))
            return z, int(cur.get("page") or 1) if z == cur.get("zone") else 1
    except Exception:
        pass
    if cur.get("zone") in ZONES:
        return cur["zone"], int(cur.get("page") or 1)
    return min(ZONES, key=lambda z: swept.get(z, 0)), 1


def zone_slug(name: str) -> Optional[str]:
    """"Città Studi, Corsica, Susa" → "citta-studi-corsica-susa" (ZONES)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s if s in ZONES else None


# ── Pages ────────────────────────────────────────────────────────────────────

_SEARCH_JS = r"""JSON.stringify((() => {
  const st = window.__INITIAL_STATE__;
  const s = st && st.agencySrp;
  if (!s || !Array.isArray(s.list)) return {missing: true, title: document.title || '',
      text: ((document.body && document.body.innerText) || '').slice(0, 400)};
  return {total: s.total, pages: (s.paginator || {}).totalPages, items: s.list};
})())"""

_DETAIL_JS = r"""JSON.stringify((() => {
  const nd = window.__NEXT_DATA__;
  const p = nd && nd.props && nd.props.pageProps && nd.props.pageProps.pdp;
  if (!p) return {missing: true, title: document.title || '',
      text: ((document.body && document.body.innerText) || '').slice(0, 400)};
  return {pdp: p};
})())"""

_GONE_RX = re.compile(r"pagina non trovata|non è più disponibile|annuncio non (?:è )?più", re.I)


def _is_block(d: dict) -> bool:
    """DataDome's challenge: no page state, titled just "casa.it"."""
    return bool(d.get("missing") and ((d.get("title") or "").strip().lower() == "casa.it"
                                      or _BLOCK_RX.search(f"{d.get('title')} {d.get('text')}")))


async def _read(tab, timed, js: str, what: str) -> dict:
    """The page's state; waits up to 25 s (DataDome's check reloads the page
    once it has passed)."""
    deadline = time.time() + 25
    while True:
        raw = await timed(tab.evaluate(js), what=what)
        if not isinstance(raw, str):              # a JS exception comes back as an object
            raise RuntimeError(f"evaluate failed: {str(raw)[:120]}")
        d = json.loads(raw)
        if not d.get("missing") or time.time() > deadline:
            return d
        if _GONE_RX.search(d.get("title") or ""):
            return d
        await asyncio.sleep(1.5)


async def _search(browser, timed, url: str) -> dict:
    """One search page → {"items": [...], "total", "pages"} or {"blocked"} / {"error"}."""
    d: dict = {}
    for attempt in (1, 2):
        try:
            d = await _read(await browser.get(url), timed, _SEARCH_JS, "read casait search")
        except Exception as exc:
            d = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        if d.get("items") is not None:
            return d
        if _is_block(d):
            return {"blocked": True}
        await asyncio.sleep(random.uniform(10, 20))
    return {"error": d.get("error") or (d.get("title") or "no search state")[:120]}


async def _sweep(known: set, zone: Optional[str], detail_items: list,
                 discover_pages: int = DISCOVER_MAX_PAGES, first_page: int = 1,
                 budget: int = BUDGET_MAX) -> tuple[list, dict, dict]:
    """New listings, the zone from `first_page`, then listing pages, in one
    browser session, at most `budget` page loads (the zone gets what the
    listing pages leave) → (search items, stats, {listing id: page fields
    or "gone"}). stats["next_page"]: where the zone stopped, None when read
    to its end."""
    from browser_session import BrowserSession, timed
    stats = {"pages": 0, "discover_pages": 0, "zone": zone, "zone_pages": 0, "zone_total_pages": None,
             "zone_complete": False, "next_page": first_page, "failed": 0, "blocked": False, "details": 0}
    zone_budget = max(0, budget - len(detail_items))
    items: list = []
    pages: dict = {}
    browser = BrowserSession(SOURCE, background=True)
    await browser.start()
    try:
        # 1. Newest first, until a page is mostly listings we know.
        for page in range(1, discover_pages + 1):
            d = await _search(browser, timed, f"{SEARCH_BASE}?{SCOPE_QUERY}&sortType=date_desc"
                              + (f"&page={page}" if page > 1 else ""))
            if d.get("blocked"):
                stats["blocked"] = True
                return items, stats, pages
            if d.get("error"):
                stats["failed"] += 1
                stats["last_error"] = d["error"]
                break
            stats["pages"] += 1
            stats["discover_pages"] += 1
            items += d["items"]
            if (not d["items"] or page >= (d.get("pages") or 0)
                    or sum(1 for it in d["items"] if f"ca_{it.get('id')}" in known) >= DISCOVER_STOP_KNOWN):
                break
            await asyncio.sleep(random.uniform(*PAGE_DELAY_S))
        # 2. The zone, from where the last pass stopped.
        if zone:
            page = first_page
            while page <= ZONE_MAX_PAGES and stats["pages"] < zone_budget:
                await asyncio.sleep(random.uniform(*PAGE_DELAY_S))
                d = await _search(browser, timed, f"{SEARCH_BASE}{zone}/?{SCOPE_QUERY}"
                                  + (f"&page={page}" if page > 1 else ""))
                if d.get("blocked"):
                    stats["blocked"] = True
                    return items, stats, pages
                if d.get("error"):
                    stats["failed"] += 1
                    stats["last_error"] = d["error"]
                    break
                stats["pages"] += 1
                stats["zone_pages"] += 1
                stats["zone_total_pages"] = d.get("pages")
                items += d["items"]
                if not d["items"] or page >= (d.get("pages") or 0):
                    stats["zone_complete"] = True
                    stats["next_page"] = None
                    break
                page += 1
                stats["next_page"] = page
        # 3. Listing pages.
        for lid, url in detail_items:
            await asyncio.sleep(random.uniform(*PAGE_DELAY_S))
            try:
                d = await _read(await browser.get(url), timed, _DETAIL_JS, "read casait listing")
            except Exception:
                continue
            if d.get("pdp"):
                pages[lid] = parse_detail(d["pdp"])
                stats["details"] += 1
            elif _GONE_RX.search(d.get("title") or ""):
                pages[lid] = "gone"
            elif _is_block(d):
                stats["blocked"] = True
                break
    finally:
        browser.stop()
    return items, stats, pages


# ── Parsing: search items ────────────────────────────────────────────────────

def _int(v) -> Optional[int]:
    """"1.240" / "€ 1.240" / 55 → 1240 / 55 (Casa.it writes thousands with a dot)."""
    m = re.search(r"\d[\d.]*", str(v if v is not None else ""))
    return int(m.group(0).replace(".", "")) if m else None


def _floor(level: str, text: str) -> tuple:
    """(floor, floor_n, floor_label) in Immobiliare's shapes, from "1° piano",
    "Piano terra", "Piano rialzato"… or else the description."""
    from fetch_idealista import _parse_floor_from_text
    n, label = _parse_floor_from_text(level) if level else (None, None)
    if n is None:
        n, label = _parse_floor_from_text(text)
    if n is None:
        return None, None, None
    floor = {"Piano terra": "T", "Piano rialzato": "R"}.get(label, str(n))
    return floor, n, label


def _media(it: dict) -> tuple[list, list]:
    """(photo urls, floor-plan urls) at IMG_FULL. media.floorplan is the
    index of the gallery picture that is the plan (16 → the 17th)."""
    m = it.get("media") or {}
    urls = [IMG_BASE + IMG_FULL + x["uri"] for x in m.get("items") or [] if x.get("uri")]
    fp = m.get("floorplan")
    if isinstance(fp, int) and not isinstance(fp, bool) and 0 <= fp < len(urls):
        return urls[:fp] + urls[fp + 1:], [urls[fp]]
    return urls, []


def parse_item(it: dict, max_rent: int = 0, min_rooms: int = 0) -> tuple[Optional[dict], str]:
    """A Casa.it search item → (rentals row, "") or (None, why skipped)."""
    from fetch_rentals import _parse_condo_from_description, condo_included
    if it.get("channel") != "affitto":
        return None, "not_for_rent"
    pub = it.get("publisher") or {}
    private = pub.get("publisherType") == "Private"
    feats = it.get("features") or {}
    title = ((it.get("title") or {}).get("main") or "").strip()
    body = re.sub(r"\s+", " ", it.get("description") or "").strip()
    text = f"{title}\n{body}"
    if re.search(r"\bstanz|posto letto", str(it.get("propertyType") or ""), re.I):
        return None, "room_let"
    if fs._SHORT_LET_RX.search(text):
        return None, "short_let"
    if fs._WANTED_RX.search(text):
        return None, "wanted"
    rent = _int((feats.get("price") or {}).get("value"))
    sqm, rooms = _int(feats.get("mq")), _int(feats.get("rooms"))
    if rooms is None:
        rooms = next((n for w, n in fs._ROOM_WORDS if w in text.lower()), None)
    elif rooms > 1 and fs._STUDIO_RX.search(text) and not fs._MULTI_RX.search(text):
        rooms = 1
    if not it.get("id") or not rent or rent < 200:
        return None, "no_price"
    if not sqm or sqm < 10:
        return None, "no_sqm"
    if max_rent and rent > max_rent:
        return None, "out_of_scope"
    if min_rooms and rooms and rooms < min_rooms:
        return None, "out_of_scope"

    town = fs.other_town(title)
    g = it.get("geoInfos") or {}
    lat = lon = None
    address, precision = None, None
    if town:
        pass                                # not in Milan: not placed (scored out below)
    elif g.get("lat") and g.get("lon"):
        lat, lon = float(g["lat"]), float(g["lon"])
        street = (g.get("street") or "").strip() if g.get("show_address") and g.get("has_street") else ""
        address = f"{street}, Milano" if street else None
        # geo_visibility_level 1: the advertiser's own address; higher: a
        # pin the site placed in the zone.
        precision = "map" if g.get("geo_visibility_level") == 1 else "near"
    else:
        hit = fs.place_from_text(text)
        if hit:
            lat, lon, precision, address = hit
    floor, floor_n, floor_label = _floor(str(feats.get("level") or ""), text)
    lift = ((it.get("leadsData") or {}).get("paEvtDatalayer") or {}).get("cust_ad_has_lift")
    contract = next((name for name, rx in fs._CONTRACT_RX if rx.search(text)), None)
    dep = fs._DEPOSIT_RX.search(text)
    deposit = (fs._NUM_WORDS.get(dep.group(1).lower()) or _int(dep.group(1))) if dep else None
    photos, plans = _media(it)
    spese = _parse_condo_from_description(body) or fs._all_in_extra(text, rent) or fs._plus_extra(text, rent)
    zone = zone_slug(g.get("district_name") or "")
    return {
        "id":                 f"ca_{it['id']}",
        "source":             SOURCE,
        "city":               "milano",
        "city_label":         "Milano",
        "city_key":           "milano",
        "title":              title,
        "description":        body or None,  # cut at ~400 characters; the listing page has it all
        "neighbourhood":      None,          # from the nearby listings (fs.settle_places)
        "address":            address,
        "latitude":           lat,
        "longitude":          lon,
        "geo_precision":      precision,
        "url":                "https://www.casa.it" + (it.get("uri") or f"/immobili/{it['id']}/"),
        "thumbnail":          photos[0].replace(IMG_FULL, IMG_THUMB, 1) if photos else None,
        "photos":             photos,
        "floorplans":         plans or None,
        "photo_count":        len(photos),
        "rent_mo":            rent,
        "sqm":                sqm,
        "ask_psqm":           round(rent / sqm, 2),
        "spese_condominiali": spese,
        "condo_included":     (spese is None and condo_included(body)) or None,
        "deposit_months":     deposit if deposit and deposit <= 12 else None,
        "rooms":              rooms,
        "bedrooms":           fs._bedrooms(text, rooms),
        "bathrooms":          _int(feats.get("bathrooms")),
        "floor":              floor,
        "floor_n":            floor_n,
        "floor_label":        floor_label,
        "is_ground_floor":    floor_n == 0 if floor_n is not None else None,
        "is_below_ground":    floor_n < 0 if floor_n is not None else None,
        "elevator":           lift if isinstance(lift, bool) else None,
        "energy_class":       feats.get("energyClass") or None,
        "contract_type":      contract,
        "agency_id":          "" if private else str(pub.get("publisherId") or ""),
        "agency_name":        "" if private else (pub.get("publisherName") or ""),
        # Immobiliare's words: "user" for a private advertiser.
        "agency_type":        "user" if private else "agency",
        "agency_url":         "" if private or not pub.get("publisherUrl") else "https://www.casa.it" + pub["publisherUrl"],
        "is_fake":            bool(town) or fs._is_fake(title, body),
        "is_room_let":        fs._room_let(text),    # a room in a shared flat
        "is_outside_city":    bool(town) or None,
        "misrep_reason":      f"Title names {town}, not Milan" if town else None,
        "scan_area":          f"casait-{zone}" if zone else "casait-milano",
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
    }, ""


# ── Parsing: listing pages ───────────────────────────────────────────────────

_CONDITIONS = (("da ristruttur", "Da ristrutturare"), ("ristruttur", "Ottimo / Ristrutturato"),
               ("ottim", "Ottimo / Ristrutturato"), ("nuov", "Nuovo / In costruzione"),
               ("costruz", "Nuovo / In costruzione"), ("buon", "Buono / Abitabile"),
               ("abitabil", "Buono / Abitabile"))


def _feature_map(pdp: dict) -> dict:
    """{label (lower case): [values as text]} over the page's feature lists."""
    out: dict = {}
    for grp in ((pdp.get("features") or {}).get("featureLists") or []):
        for item in grp.get("items") or []:
            vals = []
            for v in item.get("values") or []:
                if isinstance(v, dict):
                    main = v.get("main")
                    main = main.get("value") if isinstance(main, dict) else main
                    sec = v.get("secondary")
                    vals.append(" ".join(str(x) for x in [main, *(sec if isinstance(sec, list) else [])] if x))
                else:
                    vals.append(str(v))
            out[str(item.get("label") or "").strip().lower()] = vals
    return out


def parse_detail(pdp: dict) -> dict:
    """Fields from a Casa.it listing page (__NEXT_DATA__ … pdp), in the
    other sites' shapes (listing_details.DETAIL_KEYS and friends)."""
    from listing_details import _contract_type, _not_a_flat_to_buy, _room_check, bedrooms_from_text
    f = _feature_map(pdp)
    first = lambda k: (f.get(k) or [""])[0].lower()
    out: dict = {}
    c = first("condizioni immobile")
    out["condition"] = next((name for key, name in _CONDITIONS if key in c), None) if c else None
    h = first("riscaldamento")
    out["heating_type"] = ("autonomous" if "autonom" in h else "centralised" if "centralizz" in h
                           else "none" if "assente" in h or "nessun" in h else None)
    yes = lambda k: None if k not in f else first(k).startswith("s")
    out["air_conditioning"] = yes("aria condizionata")
    out["elevator"] = yes("ascensore")
    a = first("arredamento")
    out["furnished"] = None if not a else not re.search(r"\bnon\b", a)
    out["year_built"] = _int(first("anno di costruzione"))
    out["bathrooms"] = _int(first("bagni"))
    # "Altre caratteristiche" lists the extras; without the list, unknown.
    if "altre caratteristiche" in f:
        extras = " | ".join(f["altre caratteristiche"]).lower()
        out["has_balcony"] = bool(re.search(r"balcon|terrazz", extras))
        out["has_parking"] = bool(re.search(r"\bbox\b|garage|posto auto", extras))
    ec = ((pdp.get("energyClass") or {}).get("class") or "").strip()
    out["energy_class"] = ec.upper() or None
    out["contract_type"] = _contract_type(first("durata contratto"))
    rent_line = " ".join(f.get("canone affitto") or [])
    m = re.search(r"cauzione\s+(\d{1,2})\s+mes", rent_line, re.I)
    if m:
        out["deposit_months"] = int(m.group(1))
    spese = " ".join(f.get("spese condominiali") or [])
    fee = _int(spese)
    if fee and fee < 2000:
        out["spese_condominiali"] = fee
    elif re.search(r"inclus", spese, re.I):
        out["condo_included"] = True
    host = (pdp.get("media") or {}).get("hostImages") or (IMG_BASE + "/")
    gallery = (pdp.get("media") or {}).get("gallery") or []
    photos = [f"{host.rstrip('/')}{IMG_FULL}/listing/{g['mediaid']}" for g in gallery
              if g.get("mediaid") and g.get("type", "photo") == "photo"]
    plans = [f"{host.rstrip('/')}{IMG_FULL}/fp/{g['mediaid']}" for g in gallery
             if g.get("mediaid") and "plan" in str(g.get("type") or "")]
    if photos:
        out["photos"] = photos[:50]
        out["photo_count"] = len(photos)
    if plans:
        out["floorplans"] = plans[:4]
    desc = (((pdp.get("description") or {}).get("it") or {}).get("text") or "").strip()
    if desc:
        out["description"] = desc[:3000]
    out["costs_read"] = 1
    title = str(((pdp.get("helmetData") or {}).get("seoData") or {}).get("h1") or "")
    out.update(_not_a_flat_to_buy(f"{title} | {desc}"))
    out.update(_room_check(f"{title} | {desc}", desc))
    beds = bedrooms_from_text(desc, _int((pdp.get("rooms") or {}).get("value")))
    if beds:
        out["bedrooms"] = beds
    return {k: v for k, v in out.items() if v is not None}


# ── Already on another site? ─────────────────────────────────────────────────

def known_index(pool: list) -> dict:
    """The live listings of the other sites, bucketed for same_as_known."""
    by_price: dict = {}
    by_shape: dict = {}
    for l in pool:
        if l.get("source") == SOURCE or l.get("is_stale") or not l.get("sqm") or l.get("rooms") is None:
            continue
        by_price.setdefault((l.get("rent_mo"), l["rooms"]), []).append(l)
        by_shape.setdefault((l["rooms"], l["sqm"]), []).append(l)
    return {"price": by_price, "shape": by_shape}


def same_as_known(row: dict, index: dict) -> Optional[dict]:
    """The other site's copy of this flat, if any: comps_engine's tests —
    same rent, m², rooms and floor nearby; or the looser cross-site match
    (m² within 7 %, same street); or the same flat at another rent (same
    building, rooms, m² ±2, floor)."""
    from comps_engine import _dup_key, _dup_near, _loose_dup, _same_flat
    if not row.get("sqm") or row.get("rooms") is None:
        return None
    key = _dup_key(row)
    for l in index["price"].get((row.get("rent_mo"), row["rooms"]), ()):
        if (key is not None and _dup_key(l) == key and _dup_near(row, l)) or _loose_dup(row, l):
            return l
    for sqm in range(row["sqm"] - 2, row["sqm"] + 3):
        for l in index["shape"].get((row["rooms"], sqm), ()):
            if _same_flat(row, l):
                return l
    return None


# ── Run ──────────────────────────────────────────────────────────────────────

def run_casait(args=None, log=print) -> dict:
    """One pass (see the module doc). Returns the numbers (also appended to
    LOG_PATH)."""
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "another pass is running"}
        return _run(args, log)


def _detail_queue(pool: list, cache: dict) -> list:
    """[(id, url)] of kept Casa.it listings whose page wasn't read: the
    user's zones first, then best score."""
    try:
        from focus_areas import in_focus
    except Exception:
        in_focus = lambda l: False
    todo = [l for l in pool if l.get("source") == SOURCE and not l.get("is_stale")
            and not l.get("excluded") and l.get("url") and str(l["id"]) not in cache]
    todo.sort(key=lambda l: (not in_focus(l), -(l.get("score_total") or 0)))
    return [(str(l["id"]), l["url"]) for l in todo[:DETAILS_PER_PASS]]


def _hide_unseen(log) -> int:
    """Casa.it rows nobody saw for STALE_DAYS (their zone swept twice
    without them, or a row from before zones): hidden, like write_output's
    misses. Under its write lock."""
    import fetch_rentals as fr
    cut = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    with open(fr.OUTPUT_PATH.with_suffix(fr.OUTPUT_PATH.suffix + ".lock"), "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        rows = json.loads(fr.OUTPUT_PATH.read_text())
        n = 0
        for l in rows:
            if l.get("source") == SOURCE and not l.get("is_stale") and (l.get("last_seen_date") or "") < cut:
                l["is_stale"] = True
                n += 1
        if n:
            from dashboard_io import write_snapshot
            write_snapshot(fr.OUTPUT_PATH, rows)
    return n


def _run(args, log) -> dict:
    import fetch_rentals as fr
    import listing_details as ld
    from scoring import score_all
    t0 = time.time()
    max_rent = int(getattr(args, "max_rent", 0) or 3000)
    min_rooms = int(getattr(args, "min_rooms", 0) or 2)
    rec: dict = {"at": datetime.now().isoformat(timespec="seconds"), "source": SOURCE}
    state = _state()
    state["at"] = time.time()
    _save_state(state)

    try:
        pool = json.loads(fr.OUTPUT_PATH.read_text())
    except (OSError, ValueError):
        pool = []
    seen = fr.load_seen_ids()
    known = {k for k in seen if str(k).startswith("ca_")}
    zone, first_page = next_zone(state)
    budget = int(state.get("budget") or BUDGET_START)
    cache = ld.load_cache()
    items, stats, pages = asyncio.run(_sweep(known, zone, _detail_queue(pool, cache),
                                             first_page=first_page, budget=budget))
    rec.update({k: stats.get(k) for k in ("pages", "discover_pages", "zone", "zone_pages", "zone_total_pages",
                                          "zone_complete", "failed", "blocked", "details")})
    if stats.get("last_error"):
        rec["error"] = stats["last_error"]
    if stats["blocked"] or (stats["failed"] and not stats["pages"]):
        state["pause_until"] = time.time() + BLOCK_PAUSE_SEC
        state["budget"] = max(BUDGET_MIN, budget // 2)
    elif not stats["failed"]:
        state["budget"] = min(BUDGET_MAX, budget + 2)
    rec["budget"] = budget
    if stats["zone_complete"]:
        state.setdefault("swept", {})[zone] = time.time()
        state["cursor"] = None
    elif stats.get("next_page"):
        state["cursor"] = {"zone": zone, "page": stats["next_page"]}
    _save_state(state)

    today = str(date.today())
    if pages:                                 # into the detail cache: apply_details fills the rows
        ld.save_entries({lid: ({"gone": True, "fetched_at": today, "source": SOURCE} if v == "gone"
                               else {"fields": v, "fetched_at": today, "source": SOURCE})
                         for lid, v in pages.items()})

    rows, skipped = {}, {}
    for it in items:
        row, why = parse_item(it, max_rent, min_rooms)
        if row:
            rows[row["id"]] = row
        else:
            skipped[why] = skipped.get(why, 0) + 1
    index = known_index(pool)
    kept, dups = [], 0
    for r in rows.values():
        other = same_as_known(r, index)
        if other is None or r["agency_type"] == "user":   # missing elsewhere, or a private copy
            kept.append(r)
        else:
            dups += 1
    rec.update(found=len(items), in_scope=len(rows), kept=len(kept), on_other_sites=dups, skipped=skipped,
               unplaced=sum(1 for r in kept if r.get("latitude") is None))

    if kept:
        from sales_scan import enrich_rows
        with contextlib.redirect_stderr(open("/dev/null", "w")):
            rec["zoned"] = fs.place_by_zone(kept, pool)
            rec["unplaced"] -= rec["zoned"]
            rec["geocoded"] = enrich_rows(kept)
            rec.update(fs.settle_places(kept, pool, source=SOURCE))
            scored = score_all(kept, comps_pool=kept + [l for l in pool if l.get("source") != SOURCE])
        now_iso = datetime.now().isoformat(timespec="minutes")
        # The first passes bring listings up for weeks: not "new since the
        # last scan" and no digest until the city has been read once.
        backfill = len(state.get("swept") or {}) < len(ZONES)
        for l in scored:
            l["first_seen_date"] = (seen.get(l["id"]) or {}).get("first_seen_date") or today
            l["first_seen_at"] = (seen.get(l["id"]) or {}).get("first_seen_at") or (
                None if l["id"] in seen or backfill else now_iso)
            l["last_seen_date"] = today
            l["is_stale"] = False
        # A zone is read over several passes: nothing is hidden here, only by
        # STALE_DAYS unseen (_hide_unseen).
        rec.update(fr.write_output(scored, source=SOURCE, swept_areas=None))
        new = [l["id"] for l in scored if l["id"] not in seen]
        for l in scored:
            seen[l["id"]] = {"first_seen_date": l["first_seen_date"], "first_seen_at": l.get("first_seen_at"),
                             "last_seen_date": today}
        fr.save_seen_ids(seen)
        if not backfill:
            fr._queue_digest(new)
        rec["new"] = len(new)
    rec["unseen_hidden"] = _hide_unseen(log)
    if kept or pages or rec["unseen_hidden"]:
        fr.rescore_output()
    fr.record_sweeps(SOURCE, [f"casait-{z}" for z in ZONES],
                     [f"casait-{zone}"] if stats["zone_complete"] else [], tried_slugs=[f"casait-{zone}"])
    rec["seconds"] = round(time.time() - t0)
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"Casa.it — {zone}{'' if stats['zone_complete'] else ' (NOT complete)'} + newest: "
        f"{rec['in_scope']} in scope, {rec['kept']} kept ({dups} already on our sites), "
        f"{rec.get('new', 0)} new, {stats['details']} listing pages, {rec.get('hidden', 0)} hidden"
        + (" · blocked — paused 6 h" if stats["blocked"] else ""))
    return rec


def dump(out: str, zone: Optional[str] = None, discover_pages: int = 2) -> None:
    """Read the pages and save the parsed rows only (no write): for testing."""
    import fetch_rentals as fr
    items, stats, pages = asyncio.run(_sweep(set(), zone, [], discover_pages, budget=ZONE_MAX_PAGES + discover_pages))
    pool = json.loads(fr.OUTPUT_PATH.read_text())
    index = known_index(pool)
    rows = [r for r in (parse_item(it, 3000, 2)[0] for it in items) if r]
    for r in rows:
        o = same_as_known(r, index)
        r["_same_as"] = o and o.get("id")
    Path(out).write_text(json.dumps({"stats": stats, "items": items, "rows": rows}, ensure_ascii=False))
    print(stats, len(items), "items", len(rows), "rows", sum(1 for r in rows if not r["_same_as"]), "not on our sites")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dump", help="save parsed rows to this file instead of writing")
    p.add_argument("--zone", help="with --dump: also sweep this zone")
    p.add_argument("--pages", type=int, default=2, help="with --dump: newest-first pages")
    a = p.parse_args()
    if a.dump:
        dump(a.dump, a.zone, a.pages)
    else:
        print(run_casait(None))
