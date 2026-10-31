"""Subito.it — Milan flats for rent from private landlords.

Measured 2026-10-18: 863 private listings in Milan (~540 within ≤ €3,000 and
2+ rooms); of the in-scope ones with coordinates 53 % were on neither
Immobiliare nor Idealista — ~270 flats we didn't see, and no agency fee.
Agency listings on Subito are mostly ours already (78 %, largely Spacest /
Roomless bulk uploads), so only private advertisers are read (`advt=0`).

The search pages carry everything — full description, features, photos —
in __NEXT_DATA__, so no listing page is opened. One pass reads every page
(~29) once a day (SUBITO_EVERY_SEC), run from either scanner daemon's pause between
slots (fetch_rentals._between_slots), in its own minimized Chrome profile.

Location: ~40 % of private listings carry the map address and coordinates.
The rest say it in words only ("Senza agenzia MM verde Romolo", "in via
Ettore Ponti"): place_from_text reads a street (the Comune di Milano house
numbers, geo_index) or a metro station named next to "MM / metro / fermata"
(milan_pois.json). geo_precision says which: map / civico / near / street /
metro. A listing placed nowhere gets no comparison with nearby flats.

Rows go into milano_rentals_latest.json as source "subito" (ids "sb_<n>")
through the same write_output (hidden after 2 complete passes without
them), enrichment, scoring and digest as the other two sites.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import random
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Optional

BASE_DIR   = Path(__file__).resolve().parent
SOURCE     = "subito"
SEARCH_URL = "https://www.subito.it/annunci-lombardia/affitto/appartamenti/milano/milano/?advt=0"
SCAN_AREA  = "subito-milano"          # one "area": the whole city, read in full

SUBITO_EVERY_SEC = 20 * 3600         # one pass a night (scan_window); 20 h so it doesn't drift past the window
BLOCK_PAUSE_SEC  = 6 * 3600
MAX_PAGES        = 60                 # 29 on 2026-10-18
PAGE_DELAY_S     = (3.0, 6.0)

STATE_PATH = BASE_DIR / ".subito_state.json"
LOCK_PATH  = BASE_DIR / ".subito.lock"
LOG_PATH   = BASE_DIR / "subito_scans_milano.jsonl"
POIS_PATH  = BASE_DIR / "milan_pois.json"

IMG_FULL  = "?rule=fullscreen-1x-auto"      # always JPEG — what photo_eval reads
IMG_THUMB = "?rule=gallery-desktop-1x-auto"  # small AVIF / WebP for the cards

_BLOCK_RX = re.compile(r"captcha|robot|verifica|access denied|accesso negato|datadome|"
                       r"too many requests|troppe richieste", re.I)


# ── State ────────────────────────────────────────────────────────────────────

def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def subito_due() -> bool:
    s = _state()
    return time.time() >= max((s.get("at") or 0) + SUBITO_EVERY_SEC, s.get("pause_until") or 0)


# ── Search pages ─────────────────────────────────────────────────────────────

_EXTRACT_JS = r"""JSON.stringify((() => {
  const nd = window.__NEXT_DATA__;
  const st = nd && nd.props && nd.props.pageProps && nd.props.pageProps.initialState
             && nd.props.pageProps.initialState.items;
  if (!st || !st.originalList) return {missing: true, title: document.title || '',
      text: ((document.body && document.body.innerText) || '').slice(0, 400)};
  return {total: st.total, pages: st.totalPages, items: st.originalList.map(x => x.item || x)};
})())"""


async def _read(tab, timed) -> dict:
    """The page's search state; waits up to 15 s for the app to render."""
    deadline = time.time() + 15
    while True:
        raw = await timed(tab.evaluate(_EXTRACT_JS), what="read subito")
        if not isinstance(raw, str):              # a JS exception comes back as an object
            raise RuntimeError(f"evaluate failed: {str(raw)[:120]}")
        d = json.loads(raw)
        if not d.get("missing") or time.time() > deadline:
            return d
        await asyncio.sleep(1)


async def _sweep(max_pages: int = MAX_PAGES) -> tuple[list, dict]:
    """Every search page → (raw items, stats). complete: all pages read."""
    from browser_session import BrowserSession, timed
    stats = {"pages": 0, "total": None, "total_pages": None, "failed": 0, "blocked": False}
    items: list = []
    browser = BrowserSession(SOURCE, background=True)
    await browser.start()
    try:
        page = 1
        while page <= max_pages:
            url = SEARCH_URL + (f"&o={page}" if page > 1 else "")
            d: dict = {}
            for attempt in (1, 2):
                try:
                    d = await _read(await browser.get(url), timed)
                except Exception as exc:
                    d = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                if d.get("items") is not None:
                    break
                if d.get("missing") and _BLOCK_RX.search(f"{d.get('title')} {d.get('text')}"):
                    stats["blocked"] = True
                    break
                await asyncio.sleep(random.uniform(6, 10))
            if d.get("items") is None:
                stats["failed"] += 1
                stats["last_error"] = d.get("error") or (d.get("title") or "")[:120]
                break
            stats["pages"] += 1
            stats["total"], stats["total_pages"] = d.get("total"), d.get("pages")
            items += d["items"]
            if not d["items"] or page >= (d.get("pages") or 0):
                break
            page += 1
            await asyncio.sleep(random.uniform(*PAGE_DELAY_S))
    finally:
        browser.stop()
    stats["complete"] = bool(not stats["failed"] and stats["total_pages"]
                             and stats["pages"] >= stats["total_pages"])
    return items, stats


# ── Parsing ──────────────────────────────────────────────────────────────────

def _feat(it: dict, key: str, field: str = "key"):
    vals = ((it.get("features") or {}).get(key) or {}).get("values") or []
    return vals[0].get(field) if vals else None


def _yes(it: dict, key: str) -> Optional[bool]:
    v = _feat(it, key, "value")
    return None if v is None else str(v).strip().lower() in ("sì", "si", "yes")


def _int(v) -> Optional[int]:
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else None


_CONDITIONS = (("ristruttur", "Ottimo / Ristrutturato"), ("ottim", "Ottimo / Ristrutturato"),
               ("nuov", "Nuovo / In costruzione"), ("costruz", "Nuovo / In costruzione"),
               ("da ristruttur", "Da ristrutturare"), ("buon", "Buono / Abitabile"),
               ("abitabil", "Buono / Abitabile"))


def _condition(v) -> Optional[str]:
    t = str(v or "").lower()
    if not t:
        return None
    if "da ristruttur" in t:
        return "Da ristrutturare"
    for key, label in _CONDITIONS:
        if key in t:
            return label
    return None


def _floor(it: dict, text: str) -> tuple:
    """(floor, floor_n, floor_label) in Immobiliare's shapes ("3", 3,
    "3° piano"; "T" / "R" for ground / raised)."""
    from fetch_idealista import _parse_floor_from_text
    v = str(_feat(it, "/floor", "value") or "").strip()
    n, label = _parse_floor_from_text(f"{v}° piano" if v.isdigit() else v) if v else (None, None)
    if n is None:
        n, label = _parse_floor_from_text(text)
    if n is None:
        return None, None, None
    floor = {"Piano terra": "T", "Piano rialzato": "R"}.get(label, str(n))
    return floor, n, label


_CONTRACT_RX = (("4+4", re.compile(r"\b4\s*\+\s*4\b")), ("3+2", re.compile(r"\b3\s*\+\s*2\b")),
                ("transitorio", re.compile(r"\btransitori[oa]\b", re.I)),
                ("studenti", re.compile(r"\b(?:per|a)\s+studenti\b|contratto\s+(?:per\s+)?studenti", re.I)))
_DEPOSIT_RX = re.compile(
    r"(?:deposito|cauzione)[^.\d€]{0,40}?(\d{1,2}|una|due|tre|quattro|sei)\s*(?:mensilit|mesi|mensili)", re.I)
_NUM_WORDS = {"una": 1, "due": 2, "tre": 3, "quattro": 4, "sei": 6}
_ROOM_WORDS = (("monolocale", 1), ("bilocale", 2), ("trilocale", 3), ("quadrilocale", 4), ("pentalocale", 5))
# The text over the form when they disagree (2026-10-24: "Monolocale 28 mq"
# filed as 2 rooms, scored 78; 22 such rows): a "monolocale" with no other
# room word is a studio.
_STUDIO_RX = re.compile(r"\bmonolocal[ei]\b", re.I)
_MULTI_RX = re.compile(r"\b(?:bi|tri|quadri|penta)local[ei]\b", re.I)
# An all-in price in the text above the form's rent ("si affitta a 1100 €
# tutto compreso (spese condominiali, bollette…)" with €900 filed): the
# difference is the monthly extras.
_ALL_IN_RX = re.compile(r"(\d[\d.]{2,5})\s*(?:€|euro|eur)\s*(?:al\s+mese\s*|mensili\s*|/\s*mese\s*)?"
                        r"(?:tutto\s+compreso|tutto\s+incluso|comprensiv\w+|spese\s+incluse)", re.I)


# "Con un affitto di €1100 + € 200" (sb_640856746): the rent, then the
# monthly extras after a "+". Only when the first number is the form's rent.
_PLUS_RX = re.compile(r"(?:€\s*)?(\d[\d.]{2,5})\s*(?:€|euro|eur)?\s*\+\s*(?:€\s*)?(\d{2,4})(?!\d|[.,]\d)", re.I)


def _plus_extra(text: str, rent: int) -> Optional[int]:
    for m in _PLUS_RX.finditer(text or ""):
        first, extra = _int(m.group(1).replace(".", "")), _int(m.group(2))
        if first == rent and extra and 20 <= extra <= rent * 0.6:
            return extra
    return None


def _all_in_extra(text: str, rent: int) -> Optional[int]:
    for m in _ALL_IN_RX.finditer(text or ""):
        v = _int(m.group(1).replace(".", ""))
        if v and rent < v <= rent * 1.6:
            return v - rent
    return None
_MONTHS = r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre"
# Holiday and event lets the "Affitto breve" flag misses ("per 11 giorni",
# "periodo Olimpiadi", "solo giugno, luglio e agosto"), and people looking
# for a flat who post in the wrong section.
_SHORT_LET_RX = re.compile(
    rf"\b(?:affitto|locazione|uso)\s+(?:breve|turistic\w*|temporaneo)\b|\bper\s+(?:\d{{1,2}}|pochi)\s+"
    rf"(?:giorni|notti|settimane)\b|\b(?:a|per)\s+notte\b|\bolimpiadi\b|\bsettimanal\w*\b"
    rf"|\bsolo\s+(?:per\s+)?(?:il\s+mese\s+di\s+|i\s+mesi\s+di\s+)?(?:{_MONTHS})\b", re.I)
_WANTED_RX = re.compile(r"\bcerc(?:o|hiamo|asi)\s+(?:casa|appartament\w+|bilocale|monolocale|"
                        r"trilocale|stanza|camera|alloggio|un\s+appartamento)\b", re.I)


# Another town named in the title ("Bilocale in centro Vimercate"): Subito's
# "Milano" search lets them in. fetch_listings' list of the comuni around
# Milan without the ones that are also words or metro stops (Opera, Pero,
# Rho), plus a few further out seen in the listings.
def _other_town_rx():
    from fetch_listings import MILAN_ADJACENT_COMUNI
    towns = (set(MILAN_ADJACENT_COMUNI) - {"opera", "pero", "rho"}) | {
        "vimercate", "monza", "bollate", "garbagnate", "legnano", "brugherio", "lainate",
        "arese", "senago", "melzo", "gorgonzola", "carugate", "agrate", "peschiera borromeo"}
    return re.compile(r"\b(" + "|".join(sorted(map(re.escape, towns), key=len, reverse=True)) + r")\b", re.I)


_OTHER_TOWN = None


def other_town(title: str) -> Optional[str]:
    global _OTHER_TOWN
    _OTHER_TOWN = _OTHER_TOWN or _other_town_rx()
    m = _OTHER_TOWN.search(_fold(title))
    return m.group(1).title() if m else None


def _is_fake(title: str, body: str) -> bool:
    """fetch_listings' foreign-property / sea-view bait check, on whole
    words: its substring test reads "trasporto" as Porto (34 of 59 private
    Subito listings flagged on 2026-10-18). Its all-caps-title rule is left
    out — private landlords shout in titles without being scammers."""
    from fetch_listings import _FAKE_FOREIGN_KEYWORDS, _SEA_VIEW_KEYWORDS
    text = f"{title} {body[:1000]}".lower()
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in (*_FAKE_FOREIGN_KEYWORDS, *_SEA_VIEW_KEYWORDS))


def _room_let(text: str) -> bool:
    from listing_details import is_room_let
    return is_room_let(text)


def _bedrooms(text: str, rooms) -> Optional[int]:
    from listing_details import bedrooms_from_text
    return bedrooms_from_text(text, rooms)


def parse_item(it: dict, max_rent: int = 0, min_rooms: int = 0) -> tuple[Optional[dict], str]:
    """A Subito search item → (rentals row, "") or (None, why skipped)."""
    from fetch_rentals import _parse_condo_from_description, condo_included
    if ((it.get("type") or {}).get("value") or "").lower() != "in affitto":
        return None, "not_for_rent"
    adv = it.get("advertiser") or {}
    if adv.get("type") not in (0, None) or adv.get("company"):
        return None, "business"
    title, body = (it.get("subject") or "").strip(), (it.get("body") or "").strip()
    text = f"{title}\n{body}"
    town = other_town(title)
    if _yes(it, "/is_rental") or _SHORT_LET_RX.search(text):    # "Affitto breve"
        return None, "short_let"
    if _WANTED_RX.search(text):
        return None, "wanted"
    m = re.search(r":list:(\d+)", it.get("urn") or "") or re.search(r"(\d+)\.htm", (it.get("urls") or {}).get("default") or "")
    rent, sqm, rooms = _int(_feat(it, "/price")), _int(_feat(it, "/size")), _int(_feat(it, "/room"))
    if rooms is None:
        rooms = next((n for w, n in _ROOM_WORDS if w in text.lower()), None)
    elif rooms > 1 and _STUDIO_RX.search(text) and not _MULTI_RX.search(text):
        rooms = 1
    if not m or not rent or rent < 200:
        return None, "no_price"
    if not sqm or sqm < 10:
        return None, "no_sqm"
    if max_rent and rent > max_rent:
        return None, "out_of_scope"
    if min_rooms and rooms and rooms < min_rooms:
        return None, "out_of_scope"

    lat = lon = None
    address, precision = None, None
    g = ((it.get("geo") or {}).get("map")) or {}
    if town:
        pass                                # not in Milan: not placed (scored out below)
    elif g.get("latitude") and g.get("longitude"):
        lat, lon = float(g["latitude"]), float(g["longitude"])
        address = re.sub(r",\s*\d{5}\s+Milano\s+MI,\s*Italia$|,\s*Italia$", ", Milano", g.get("address") or "") or None
        precision = "map"
    else:
        hit = place_from_text(text)
        if hit:
            lat, lon, precision, address = hit
    floor, floor_n, floor_label = _floor(it, text)
    heating = str(_feat(it, "/heating", "value") or "").lower()
    contract = next((name for name, rx in _CONTRACT_RX if rx.search(text)), None)
    dep = _DEPOSIT_RX.search(text)
    deposit = (_NUM_WORDS.get(dep.group(1).lower()) or _int(dep.group(1))) if dep else None
    images = [i.get("cdnBaseUrl") for i in it.get("images") or [] if i.get("cdnBaseUrl")]
    spese = _parse_condo_from_description(body) or _all_in_extra(text, rent) or _plus_extra(text, rent)
    return {
        "id":                 f"sb_{m.group(1)}",
        "source":             SOURCE,
        "city":               "milano",
        "city_label":         "Milano",
        "city_key":           "milano",
        "title":              title,
        "description":        body or None,
        "neighbourhood":      None,          # from the nearby listings (settle_places)
        "address":            address,
        "latitude":           lat,
        "longitude":          lon,
        "geo_precision":      precision,
        "url":                (it.get("urls") or {}).get("default"),
        "thumbnail":          images[0] + IMG_THUMB if images else None,
        "photos":             [u + IMG_FULL for u in images],
        "photo_count":        len(images),
        "rent_mo":            rent,
        "sqm":                sqm,
        "ask_psqm":           round(rent / sqm, 2),
        "spese_condominiali": spese,
        "condo_included":     (spese is None and condo_included(body)) or None,
        "deposit_months":     deposit if deposit and deposit <= 12 else None,
        # The search item is the whole listing: no listing page to wait for.
        "costs_read":         True,
        "rooms":              rooms,
        "bedrooms":           _bedrooms(text, rooms),
        "bathrooms":          _int(_feat(it, "/bathrooms")),
        "floor":              floor,
        "floor_n":            floor_n,
        "floor_label":        floor_label,
        "is_ground_floor":    floor_n == 0 if floor_n is not None else None,
        "is_below_ground":    floor_n < 0 if floor_n is not None else None,
        "elevator":           _yes(it, "/elevator"),
        "has_balcony":        _yes(it, "/balcony"),
        "has_parking":        None if _feat(it, "/parking") is None else str(_feat(it, "/parking", "value")).lower() not in ("no", "nessuno"),
        "furnished":          _yes(it, "/furnished"),
        "air_conditioning":   _yes(it, "/air_conditioning"),
        "heating_type":       "autonomous" if "autonom" in heating else "centralised" if "centralizz" in heating else ("unknown" if heating else None),
        "energy_class":       _feat(it, "/energy_class", "value"),
        "condition":          _condition(_feat(it, "/building_condition", "value")),
        "availability":       "libero" if _yes(it, "/available_now") else None,
        "contract_type":      contract,
        "published_date":     (it.get("date") or "")[:10] or None,
        "agency_id":          "",
        "agency_name":        "",
        "agency_type":        "user",        # Immobiliare's word for a private advertiser
        "agency_url":         "",
        "is_fake":            bool(town) or _is_fake(title, body),
        "is_room_let":        _room_let(text),       # a room in a shared flat
        "is_outside_city":    bool(town) or None,
        "misrep_reason":      f"Title names {town}, not Milan" if town else None,
        "scan_area":          SCAN_AREA,
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
    }, ""


# ── Location from the text ───────────────────────────────────────────────────

_STREET_TYPES = (r"via|viale|v\.le|corso|c\.so|piazza|p\.za|p\.zza|piazzale|p\.le|largo|l\.go|alzaia|"
                 r"ripa|vicolo|bastioni|piazzetta|passaggio|strada")
_STREET_RX = re.compile(rf"\b({_STREET_TYPES})\s+((?:[^\W\d_][\w'’.]*[ \t]*){{1,5}})"
                        r"(?:,?[ \t]*(?:n\.?|nr\.?|n°|civico)?[ \t]*(\d{1,4}(?:[ /]?[a-zA-Z]\b)?)\b)?", re.I)
_STOP_WORDS = {"a", "e", "in", "con", "zona", "vicino", "angolo", "ang", "milano", "mm", "metro",
               "fermata", "piano", "affitto", "appartamento", "bilocale", "trilocale", "quadrilocale",
               "monolocale", "libero", "subito", "senza", "agenzia", "per", "tra", "nei", "pressi",
               "adiacente", "adiacenze", "accanto", "dietro", "fronte", "linea", "dal", "dalla", "da"}
_METRO_CUE = (r"(?:\bmm\s*\d?|\bm[1-5]\b|\bmetro\w*|\bfermat[ae]|\bstazione|\blinea\s+\w+|"
              r"\b(?:rossa|verde|gialla|lilla|blu)\b)")
# A station named as a destination, not as where the flat is: "10 minuti di
# metro dal Duomo", "raggiungere ... la stazione Centrale", "Duomo 7 min".
_TRAVEL_BEFORE_RX = re.compile(r"minut\w*\s+(?:di|in|con)\s+(?:metro|mm|tram|bus|treno|mezzi|auto|macchina)|minut\w*\s+(?:da|dal|dalla|dallo)\b|raggiung|colleg|\bfino\s+a\b|\bverso\b|"
                               r"\bin\s+metro\b|\bper\s+(?:arrivare|andare)|\bfermate\s+d|\bbus\b|\btram\b", re.I)
_TRAVEL_AFTER_RX = re.compile(r"\s*(?:in|a)?\s*\d{1,2}\s*min|\s*(?:si\s+trova\s+)?a\s+\d+\s+fermat|\s*raggiungibil", re.I)
# Hubs people name as destinations: used only when no other station is named.
_HUBS = {"Duomo", "Centrale FS", "Garibaldi FS", "Cadorna FN", "San Babila", "Cordusio", "Cairoli",
         "Lambrate FS", "Rogoredo FS", "Repubblica"}
_stations: list | None = None


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _station_patterns() -> list:
    """[(compiled pattern, lat, lon, name)] — each station by its name and,
    when unambiguous, its parts ("Sforza-Policlinico", "Centrale FS")."""
    global _stations
    if _stations is not None:
        return _stations
    try:
        metro = json.loads(POIS_PATH.read_text()).get("metro") or []
    except (OSError, ValueError):
        metro = []
    names: dict = {}
    for el in metro:
        if el.get("is_station") and el.get("name"):
            names.setdefault(el["name"], (el["lat"], el["lon"]))
    aliases: dict = {}
    for name, ll in names.items():
        base = re.sub(r"^stazione\s+|\s+(?:fs|fn|t\.i\.b\.b\.)$", "", _fold(name)).strip()
        for a in {base, *[p.strip() for p in re.split(r"\s*[-/]\s*", base)]}:
            if len(a) >= 4:
                aliases.setdefault(a, set()).add(name)
    out = []
    for a, owners in aliases.items():
        if len(owners) != 1:
            continue                       # "san siro": two stations
        name = next(iter(owners))
        word = re.escape(a).replace(r"\ ", r"\s+").replace("'", "['’ ]?")
        rx = re.compile(rf"{_METRO_CUE}[^.\n;]{{0,25}}?\b(?P<a>{word})\b"
                        rf"|\b(?P<b>{word})\b\s*\(?\s*(?:mm|m[1-5])\b", re.I)
        out.append((rx, *names[name], name))
    _stations = out
    return out


def _station_hit(text: str) -> Optional[tuple]:
    """(lat, lon, name) of the first station the text places the flat by;
    a hub only when no other station is named."""
    folded = _fold(text)
    found = []
    for rx, lat, lon, name in _station_patterns():
        for m in rx.finditer(folded):
            g = "a" if m.group("a") else "b"
            s, e = m.start(g), m.end(g)
            if _TRAVEL_BEFORE_RX.search(folded[max(0, s - 40):s]) or _TRAVEL_AFTER_RX.match(folded[e:e + 12]):
                continue
            found.append((name in _HUBS, s, lat, lon, name))
            break
    if not found:
        return None
    _, _, lat, lon, name = min(found)
    return lat, lon, name


def _street_hit(text: str) -> Optional[tuple]:
    """The first street of the text the house-number index knows →
    (lat, lon, precision, address); an exact number wins over a bare street."""
    try:
        import geo_index
        if not geo_index.available():
            return None
    except Exception:
        return None
    best = None
    for m in _STREET_RX.finditer(text):
        words = [w.strip(".,;:'’") for w in m.group(2).split()]
        # The name ends at the first ordinary word ("via Verro zona Bocconi").
        cut = next((i for i, w in enumerate(words) if w.lower() in _STOP_WORDS), len(words))
        words, num = words[:cut], (m.group(3) or "").replace(" ", "") if cut == len(words) else ""
        if not words or _TRAVEL_BEFORE_RX.search(text[max(0, m.start() - 40):m.start()]):
            continue
        # Never a shorter name: "via Ettore" isn't via Ettore Ponti. The
        # index's own tolerant match takes extra words ("piazza Duomo
        # tramite" → piazza Duomo).
        street = f"{m.group(1)} {' '.join(words)}"
        query = f"{street}{', ' + num if num else ''}, Milano"
        hit = geo_index.lookup(query)
        if not hit:
            continue
        lat, lon, prec = hit
        name = (geo_index.street_key(query) or street).title()
        addr = f"{name}{' ' + num.upper() if prec in ('civico', 'near') else ''}, Milano"
        if prec in ("civico", "near"):
            return lat, lon, prec, addr
        best = best or (lat, lon, prec, addr)
    return best


def place_from_text(text: str) -> Optional[tuple]:
    """(lat, lon, precision, address) from the words of a listing: a street
    (its house number, else the centre of a short street), else a metro
    station named as such. None when the text names neither. Checked on the
    253 listings with map coordinates (2026-10-18): house number median 12 m
    off, street 44 m, metro station ~370 m (p80 ~750 m)."""
    street = _street_hit(text)
    if street:
        return street
    station = _station_hit(text)
    if station:
        name = re.sub(r"\s+(?:FS|FN|T\.I\.B\.B\.)$", "", station[2])
        return station[0], station[1], "metro", f"Metro {name}, Milano"
    return street


def _dist_m(a: tuple, b: tuple) -> float:
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    return 6371000 * math.hypot(x, (b[0] - a[0]) * r)


# Zone from the text, for a listing that names neither a street nor a stop
# ("Bilocale zona Bocconi"): the centre of the listings the other sites put
# in that neighbourhood. Checked on the 214 listings with Subito's pin
# (2026-10-18): 101 name a zone; the zone centre is 727 m from the real pin
# (median), p80 1.2 km, 11 beyond 1.5 km — good for the area filter, not for
# a deal label (scoring: rent_label_exact_place).
_ZONE_CUE = (r"(?:zona|quartiere|nel cuore d\w*|in pieno|adiacenz\w*|nei pressi d\w*|a due passi da\w*|"
             r"a pochi passi da\w*|vicin\w* a\w*|ad?\s+angolo|in)\s+(?:di\s+|del\s+|della\s+|dei\s+|dell'|alla\s+|al\s+)?")
_ZONE_GENERIC = {"centro", "centrale", "milano", "zona", "citta"}


def _zone_index(pool: list) -> dict:
    """{alias: (lat, lon, neighbourhood)} from the other sites' listings:
    every neighbourhood name and its parts ("porta romana - medaglie d'oro"
    → also "porta romana"), with at least 8 listings."""
    import statistics
    pts: dict = {}
    for l in pool:
        if l.get("source") == SOURCE or not l.get("neighbourhood") or l.get("latitude") is None or l.get("is_stale"):
            continue
        name = l["neighbourhood"]
        n = _fold(name)
        for a in {n, *[p.strip() for p in re.split(r"\s+-\s+|,|/", n)]}:
            if len(a) >= 4 and a not in _ZONE_GENERIC:
                pts.setdefault(a, []).append((l["latitude"], l["longitude"], name))
    out = {}
    for a, p in pts.items():
        if len(p) >= 8:
            names = [x[2] for x in p]
            out[a] = (statistics.median(x[0] for x in p), statistics.median(x[1] for x in p),
                      max(set(names), key=names.count))
    return out


def zone_from_text(title: str, body: str, index: dict) -> Optional[str]:
    """The neighbourhood a listing names: anywhere in the title, else after
    a cue ("zona", "nei pressi di"…) in the text; destinations ("10 minuti
    dal …") don't count."""
    aliases = sorted(index, key=len, reverse=True)
    t = _fold(title)
    for a in aliases:
        m = re.search(r"\b" + re.escape(a) + r"\b", t)
        if m and not _TRAVEL_BEFORE_RX.search(t[max(0, m.start() - 40):m.start()]):
            return a
    b, best = _fold(body), None
    for a in aliases:
        for m in re.finditer(_ZONE_CUE + r"\b" + re.escape(a) + r"\b", b):
            if _TRAVEL_BEFORE_RX.search(b[max(0, m.start() - 40):m.start()]):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), a)
            break
    return best[1] if best else None


def place_by_zone(rows: list, pool: list) -> int:
    """Rows still without a place: the zone their text names (geo_precision
    "zone", neighbourhood from the other sites). Before enrichment."""
    todo = [r for r in rows if r.get("latitude") is None and not r.get("is_outside_city")]
    if not todo:
        return 0
    index, n = _zone_index(pool), 0
    for r in todo:
        a = zone_from_text(r.get("title") or "", r.get("description") or "", index)
        if a:
            lat, lon, name = index[a]
            r.update(latitude=lat, longitude=lon, geo_precision="zone", neighbourhood=name,
                     address=r.get("address") or f"Zona {name}, Milano")
            n += 1
    return n


def settle_places(rows: list, pool: list, source: str = SOURCE) -> dict:
    """After enrichment: a pin outside the Comune di Milano marks the
    listing outside the city (scored out, like Idealista's); a placed row
    gets the neighbourhood the other sites' listings around it use (most
    common within 300 m, else the nearest within 800 m), else its OMI
    zone's first name — the card title, the area filter, focus areas and
    stats read it. `source`: the caller's rows (fetch_casait), left out of
    the grid like Subito's — their names come from the pin too."""
    import omi_lookup
    grid: dict = {}
    for l in pool:
        if (l.get("source") not in (SOURCE, source) and l.get("neighbourhood")
                and l.get("latitude") is not None and l.get("longitude") is not None):
            key = (math.floor(l["latitude"] * 100), math.floor(l["longitude"] * 100))   # ~1 km cells
            grid.setdefault(key, []).append((l["latitude"], l["longitude"], l["neighbourhood"]))
    out = {"outside": 0, "named": 0, "named_omi": 0}
    for r in rows:
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is None or lon is None:
            continue
        if omi_lookup.lookup_for_city(float(lat), float(lon), city="milano")[1] != "polygon":
            r.update(is_outside_city=True, is_fake=True, misrep_reason="Pin outside the Comune di Milano")
            out["outside"] += 1
            continue
        if r.get("neighbourhood"):
            continue
        here = (lat, lon)
        near = sorted((_dist_m(here, (la, lo)), name)
                      for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                      for la, lo, name in grid.get((math.floor(lat * 100) + dy, math.floor(lon * 100) + dx), ())
                      if _dist_m(here, (la, lo)) <= 800)
        close = [name for d, name in near if d <= 300]
        if close:
            r["neighbourhood"] = max(set(close), key=close.count)
        elif near:
            r["neighbourhood"] = near[0][1]
        elif r.get("omi_descr"):
            r["neighbourhood"] = str(r["omi_descr"]).split(",")[0].strip().title()
            out["named_omi"] += 1
            continue
        else:
            continue
        out["named"] += 1
    return out


# A listing whose address is only a neighbourhood ("Navigli - Porta Genova,
# Milano", "Isola, Milano" — the site's zone, picked by the agency) sits
# where the geocoder put that name: the zone's centre, or somewhere else
# entirely ("Isola" landed near Corsico, 12 km off). When the opening of its
# description names a street or a metro stop more than RELOCATE_M away, the
# pin moves there (2026-10-31: 19 sales and 4 rentals; id_36728487 says
# "VIALE FAMAGOSTA", was pinned in the Navigli and scored 95 as 34 % below
# neighbours it doesn't have).
RELOCATE_M = 1000
_STREETISH_RX = re.compile(r"^(?:via|viale|v\.le|piazza|p\.za|piazzale|p\.le|corso|c\.so|largo|vicolo|alzaia|"
                           r"ripa|strada|galleria|bastioni|foro|passaggio|metro)\b|\d", re.I)
_relocate_seen: dict = {}


def _zone_only(l: dict) -> bool:
    head = (l.get("address") or "").split(",")[0].strip()
    return bool(head) and not _STREETISH_RX.search(head)


def relocate_zone_only(rows: list, pool: list) -> int:
    """Move zone-only listings to the street or metro stop their description
    opens with (see above): new pin, neighbourhood from the listings around
    it (settle_places), OMI zone and POIs re-read and written to the
    enrichment cache — which every scan re-applies, so the old pin doesn't
    come back. Subito's own rows are placed from their text already."""
    moved = []
    for l in rows:
        if l.get("is_stale") or l.get("source") == SOURCE or not l.get("description") or not _zone_only(l):
            continue
        text = l["description"][:400]
        key = (str(l.get("id")), text)
        if key not in _relocate_seen:
            _relocate_seen[key] = place_from_text(text)
        hit = _relocate_seen[key]
        if not hit:
            continue
        lat, lon, prec, addr = hit
        if l.get("latitude") is not None and l.get("longitude") is not None \
                and _dist_m((float(l["latitude"]), float(l["longitude"])), (lat, lon)) <= RELOCATE_M:
            continue
        l.update(latitude=lat, longitude=lon, geo_precision=prec, place_from_text=addr,
                 neighbourhood=None, omi_source=None, omi_compr_mid=None)
        if prec in ("civico", "near", "street"):
            l["address"] = addr
        moved.append(l)
    if not moved:
        return 0
    settle_places(moved, pool, source=moved[0].get("source") or "")
    import enrichment_cache as ec
    from enrich_geo import enrich_batch
    ec.load()
    entries = []
    for l, g in zip(moved, enrich_batch(moved)):
        # The scans merge every cached field back over what they parse, so the
        # place and its name stay when the site's zone label comes back.
        g = {k: v for k, v in g.items() if v is not None}
        g.update({k: l[k] for k in ("latitude", "longitude", "geo_precision", "place_from_text", "neighbourhood", "address")
                  if l.get(k) is not None})
        l.update(g)
        # The scans' cache keys: an Immobiliare rental row may carry no source.
        entries.append((l.get("source") or "immobiliare", l["id"], g))
    ec.bulk_save(entries)
    for l in moved:
        print(f"  [place] {l['id']} moved to {l['place_from_text']} → {l.get('neighbourhood')}", flush=True)
    return len(moved)


# ── Run ──────────────────────────────────────────────────────────────────────

def run_subito(args=None, log=print) -> dict:
    """One pass: every search page, parse, place, enrich, score, merge into
    the rentals file. Returns the numbers (also appended to LOG_PATH)."""
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "another pass is running"}
        return _run(args, log)


def _run(args, log) -> dict:
    import fetch_rentals as fr
    from scoring import score_all
    t0 = time.time()
    max_rent = int(getattr(args, "max_rent", 0) or 3000)
    min_rooms = int(getattr(args, "min_rooms", 0) or 2)
    rec: dict = {"at": datetime.now().isoformat(timespec="seconds"), "source": SOURCE}
    state = _state()
    state["at"] = time.time()
    STATE_PATH.write_text(json.dumps(state))

    items, stats = asyncio.run(_sweep())
    rec.update({k: stats[k] for k in ("pages", "total", "total_pages", "failed", "blocked", "complete")})
    if stats.get("last_error"):
        rec["error"] = stats["last_error"]
    if stats["blocked"] or (stats["failed"] and not stats["pages"]):
        state["pause_until"] = time.time() + BLOCK_PAUSE_SEC
        STATE_PATH.write_text(json.dumps(state))

    rows, skipped = {}, {}
    for it in items:
        row, why = parse_item(it, max_rent, min_rooms)
        if row:
            rows[row["id"]] = row
        else:
            skipped[why] = skipped.get(why, 0) + 1
    rows = list(rows.values())
    rec.update(found=len(items), kept=len(rows), skipped=skipped,
               placed={p: sum(1 for r in rows if r.get("geo_precision") == p)
                       for p in ("map", "civico", "near", "street", "metro")},
               unplaced=sum(1 for r in rows if r.get("latitude") is None))

    if rows:
        try:
            pool = json.loads(fr.OUTPUT_PATH.read_text())
        except (OSError, ValueError):
            pool = []
        from sales_scan import enrich_rows
        with contextlib.redirect_stderr(open("/dev/null", "w")):
            rec["zoned"] = place_by_zone(rows, pool)
            rec["unplaced"] -= rec["zoned"]
            rec["geocoded"] = enrich_rows(rows)
            rec.update(settle_places(rows, pool))
            scored = score_all(rows, comps_pool=rows + [l for l in pool if l.get("source") != SOURCE])
        seen = fr.load_seen_ids()
        today = str(date.today())
        now_iso = datetime.now().isoformat(timespec="minutes")
        for l in scored:
            l["first_seen_date"] = (seen.get(l["id"]) or {}).get("first_seen_date") or today
            l["first_seen_at"] = (seen.get(l["id"]) or {}).get("first_seen_at") or (None if l["id"] in seen else now_iso)
            l["last_seen_date"] = today
            l["is_stale"] = False
        # A pass that stopped early must not hide what it didn't reach.
        rec.update(fr.write_output(scored, source=SOURCE,
                                   swept_areas={SCAN_AREA} if stats["complete"] else None))
        new = [l["id"] for l in scored if l["id"] not in seen]
        for l in scored:
            seen[l["id"]] = {"first_seen_date": l["first_seen_date"], "first_seen_at": l.get("first_seen_at"),
                             "last_seen_date": today}
        fr.save_seen_ids(seen)
        fr._queue_digest(new)
        rec["new"] = len(new)
        fr.rescore_output()
    fr.record_sweeps(SOURCE, [SCAN_AREA], [SCAN_AREA] if stats["complete"] else [],
                     tried_slugs=[SCAN_AREA])
    rec["seconds"] = round(time.time() - t0)
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"Subito — {rec['kept']} private listings in scope of {rec['found']} "
        f"({rec['pages']}/{rec.get('total_pages') or '?'} pages{'' if stats['complete'] else ', NOT complete'}), "
        f"{rec.get('new', 0)} new, {rec.get('hidden', 0)} hidden, {rec['unplaced']} without a place"
        + (" · blocked — paused 6 h" if stats["blocked"] else ""))
    return rec


def dump(out: str, pages: int = MAX_PAGES) -> None:
    """Read the pages and save the parsed rows only (no write): for testing."""
    items, stats = asyncio.run(_sweep(pages))
    rows = [r for r in (parse_item(it, 3000, 2)[0] for it in items) if r]
    Path(out).write_text(json.dumps({"stats": stats, "items": items, "rows": rows}, ensure_ascii=False))
    print(stats, len(items), "items", len(rows), "rows")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dump", help="save parsed rows to this file instead of writing")
    p.add_argument("--pages", type=int, default=MAX_PAGES)
    a = p.parse_args()
    if a.dump:
        dump(a.dump, a.pages)
    else:
        print(run_subito(None))
