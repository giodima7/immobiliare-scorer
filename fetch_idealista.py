#!/usr/bin/env python3
"""
fetch_idealista.py
──────────────────
Fetches Milano rental listings from Idealista.it via nodriver (same Chrome
approach as fetch_rentals.py), scores each one against OMI rent benchmarks,
and merges the results into dashboard/rentals_latest.json alongside
Immobiliare.it data.

Usage (one-shot):
    python3 fetch_idealista.py
    python3 fetch_idealista.py --pages 5
    python3 fetch_idealista.py --areas navigli,brera --max-rent 2000

Daemon mode (loops every 60 min):
    python3 fetch_idealista.py --daemon

First run — CAPTCHA:
    Idealista may show a DataDome CAPTCHA on first visit.  The scanner will
    pause and wait up to 2 minutes for you to solve it manually in the Chrome
    window.  After solving it once the session persists and subsequent runs
    complete automatically.

    Watch the terminal: if you see
        ⚠ CAPTCHA detected — please solve it in the browser window.
    switch to the Chrome window, solve the puzzle, then return to the terminal.
    After "✓ CAPTCHA solved" the scan continues automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re as _re
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import nodriver as uc

# ── Shared infrastructure imported from fetch_rentals ────────────────────────
# All OMI data, scoring, enrichment, and persistence live in fetch_rentals so
# this file only adds Idealista-specific URL building and HTML/DOM parsing.
from fetch_rentals import (
    # Paths
    BASE_DIR, DASHBOARD_DIR, SEEN_IDS_PATH, OUTPUT_PATH,
    NETLIFY_CONFIG_PATH, STATUS_PATH, DIGEST_SENT_PATH,
    # Constants
    CITY_KEY, CITY_LABEL, CHROME_PATH, DAEMON_INTERVAL_SEC,
    # OMI + scoring
    match_omi, score_all,
    # Parsing helpers
    parse_floor, to_url_slug,
    # Persistence
    load_seen_ids, save_seen_ids, write_output,
    # Active areas
    _load_active_areas,
    # Netlify deploy + prefs
    _netlify_deploy, _load_scan_prefs,
    # Daily digest helpers
    mark_digest_sent,
)

SOURCE             = "idealista"
IDEALISTA_CITY_BASE = "https://www.idealista.it/affitto-case/milano-milano/"  # city-wide
IDEALISTA_ZONE_BASE = "https://www.idealista.it/affitto-case/milano/"          # per-zone
# Keep IDEALISTA_BASE as an alias so any external references still work
IDEALISTA_BASE = IDEALISTA_CITY_BASE

# Path to the Idealista-specific area settings file (separate from Immobiliare's)
IDEALISTA_AREAS_PATH = BASE_DIR / "idealista_area_settings.json"

# Path to the neighbourhood synonym map: Idealista sub-zone name → Immobiliare canonical name
_SYNONYMS_PATH = BASE_DIR / "neighbourhood_synonyms.json"


def _load_synonyms() -> dict:
    """
    Load Idealista → Immobiliare neighbourhood synonym map from JSON.
    Returns an empty dict if the file is missing or malformed.
    """
    if _SYNONYMS_PATH.exists():
        try:
            return json.loads(_SYNONYMS_PATH.read_text())
        except Exception:
            pass
    return {}


# Loaded once at module import time so every listing parse uses it.
_NEIGHBOURHOOD_SYNONYMS: dict = _load_synonyms()


def _load_idealista_areas() -> list:
    """
    Load active Idealista zone names from idealista_area_settings.json.
    Falls back to _load_active_areas() (Immobiliare areas) if the file is absent.
    """
    if IDEALISTA_AREAS_PATH.exists():
        try:
            data = json.loads(IDEALISTA_AREAS_PATH.read_text())
            active = [a["name"] for a in data.get("areas", []) if a.get("active")]
            if active:
                return active
        except Exception:
            pass
    # Fallback: use Immobiliare area names (likely have same neighbourhood names)
    return _load_active_areas()


# ── URL construction ──────────────────────────────────────────────────────────
#
# Idealista uses PATH-BASED filters (not query params).  The format is:
#   /affitto-case/milano-milano/{area_slug}/con-{filter1},{filter2},.../
#
# Known filter tokens:
#   prezzo_N          — max monthly rent  (e.g. prezzo_3000)
#   dimensione_N      — min surface m²    (e.g. dimensione_50)
#   monolocali-1      — 1-room apartments
#   bilocali-2        — 2-room apartments
#   trilocali-3       — 3-room apartments
#   quadrilocali-4    — 4-room apartments
#   5-locali-o-piu    — 5+ rooms
#   affitto-lungo-termine — long-term rentals only (excludes vacation lets)
#
# Pagination is still a query parameter: ?pag=N

_ROOM_PATH_TOKENS = {
    1: "monolocali-1",
    2: "bilocali-2",
    3: "trilocali-3",
    4: "quadrilocali-4",
    5: "5-locali-o-piu",
}


def build_idealista_url(page: int, area_slug: str = None,
                        max_rent: int = 0, min_sqm: int = 0,
                        min_rooms: int = 0) -> str:
    """
    Build a fully parameterised Idealista.it rental search URL using the
    correct path-segment filter syntax.

    Idealista uses TWO different URL bases:
      • City-wide (no area): /affitto-case/milano-milano/
      • Per-zone  (area):    /affitto-case/milano/{zone_slug}/

    Examples
    --------
    No filters (city-wide):
      https://www.idealista.it/affitto-case/milano-milano/con-affitto-lungo-termine/

    With zone + filters:
      https://www.idealista.it/affitto-case/milano/navigli-bocconi/
      con-prezzo_3000,bilocali-2,trilocali-3,quadrilocali-4,5-locali-o-piu,
      affitto-lungo-termine/
    """
    if area_slug:
        base = IDEALISTA_ZONE_BASE + f"{area_slug}/"
    else:
        base = IDEALISTA_CITY_BASE

    # Assemble filter tokens
    tokens: list = []
    if max_rent:
        tokens.append(f"prezzo_{max_rent}")
    if min_sqm:
        tokens.append(f"dimensione_{min_sqm}")
    if min_rooms:
        # Include every room-count bucket from min_rooms upward
        for n in range(min_rooms, max(min_rooms, 5) + 1):
            if n in _ROOM_PATH_TOKENS:
                tokens.append(_ROOM_PATH_TOKENS[n])
    # Always request long-term rentals (excludes holiday lets)
    tokens.append("affitto-lungo-termine")

    url = base + "con-" + ",".join(tokens) + "/"
    if page > 1:
        # Idealista uses path-based pagination: .../con-.../lista-N.htm
        # (NOT ?pag=N query params — those are ignored)
        url += f"lista-{page}.htm"
    return url


# ── CAPTCHA handling ──────────────────────────────────────────────────────────

async def _check_and_handle_captcha(tab, timeout: int = 120) -> bool:
    """
    Detect DataDome CAPTCHA and wait for the user to solve it manually.
    Returns True if the page is clean, False if timed out.

    NOTE: We do NOT use body-text length as a CAPTCHA signal — Idealista's
    JS-heavy page is legitimately empty for ~12 seconds after navigation, which
    would produce false positives.  We only flag when CAPTCHA-specific keywords
    are present AND article.item elements are absent after the render wait.
    """
    # First, wait for the page to render (articles OR enough text to decide)
    await _wait_for_render(tab)

    try:
        page_text = await tab.evaluate("document.body?.innerText || ''")
    except Exception:
        return True   # can't introspect page — assume OK

    lpt = (page_text or "").lower()
    captcha_signals = [
        "captcha"  in lpt,
        "datadome" in lpt,
    ]

    if not any(captcha_signals):
        return True   # page looks clean

    print("\n  [idealista] ⚠ CAPTCHA detected — please solve it in the browser window.")
    print(f"  [idealista] Waiting up to {timeout}s for you to complete it…")

    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            pt = await tab.evaluate("document.body?.innerText || ''")
            lpt2 = (pt or "").lower()
            if not any(["captcha" in lpt2, "datadome" in lpt2]):
                print("  [idealista] ✓ CAPTCHA solved — continuing scan")
                return True
        except Exception:
            pass

    print("  [idealista] ✗ CAPTCHA not solved in time — aborting this area")
    return False


# ── DOM extraction ────────────────────────────────────────────────────────────
#
# IMPORTANT: nodriver's tab.evaluate() may return complex JS objects as CDP
# descriptor structures (lists of [key, {type, value}] pairs) rather than plain
# Python dicts.  Wrapping the JS return value in JSON.stringify() and parsing it
# in Python gives clean, reliable dicts regardless of nodriver version.

_EXTRACT_JS = r"""
JSON.stringify((() => {
    const articles = document.querySelectorAll('article.item');
    if (!articles.length) return [];
    return Array.from(articles).map(a => {
        const getText = sel => {
            const el = a.querySelector(sel);
            return el ? (el.innerText || el.textContent || '').trim() : '';
        };

        // Collect all detail chips into a flat array for smarter field detection
        const details = Array.from(
            a.querySelectorAll(
                '.item-detail-char .item-detail, '
                + '[class*="detail-item"], '
                + '.item-details li, '
                + '.item-detail'
            )
        ).map(d => (d.innerText || d.textContent || '').trim()).filter(Boolean);

        // ID: prefer data-adid; fall back to extracting numeric ID from href.
        // Idealista uses /immobile/12345678/ or /annunci/12345678/ patterns.
        const idAttr = a.getAttribute('data-adid')
                    || a.getAttribute('data-element-id')
                    || a.getAttribute('data-id')
                    || '';

        // Prefer resolved href so relative paths become absolute automatically
        const linkEl = a.querySelector('a.item-link')
                    || a.querySelector('a[href*="/immobile/"]')
                    || a.querySelector('a[href*="/annunci/"]')
                    || a.querySelector('a[href]');
        const url = linkEl ? (linkEl.href || linkEl.getAttribute('href') || '') : '';

        // Extract numeric ID from URL path (8+ digit segment)
        const idFromUrl = idAttr
            || (url.match(/\/immobile\/(\d+)/)
             || url.match(/\/annunci\/(\d+)/)
             || url.match(/\/(\d{7,})(?:\/|$)/) || [])[1]
            || '';

        // Idealista encodes the full address in the listing title:
        //   "Bilocale in Via Roma, 10, Navigli, Milano"
        //   "Appartamento in Piazza Duomo, Centro, Milano"
        // We extract everything after " in " as the address string.
        const titleText = getText('a.item-link') || getText('.item-title');
        const inIdx     = titleText.search(/\s+in\s+/i);
        const addressFromTitle = inIdx >= 0
            ? titleText.slice(inIdx).replace(/^\s+in\s+/i, '').trim()
            : '';

        return {
            id:          idFromUrl,
            url,
            title:       titleText,
            price_text:  getText('.item-price') || getText('[class*="price"]'),
            // Pick the chip containing m² / mq
            size_text:   details.find(d => /m[²2]|mq/i.test(d)) || getText('[class*="size"]') || '',
            // Pick the chip describing room count
            rooms_text:  details.find(d =>
                             /local[ei]|trilocale|bilocale|monolocale|\blocali\b/i.test(d)
                         ) || '',
            // Pick the chip describing floor
            floor_text:  details.find(d => /piano|floor|\bp\.?\s*\d/i.test(d)) || '',
            // Address: prefer explicit element; fall back to title-extracted version
            address:     getText('.item-address')
                      || getText('[class*="address"]')
                      || getText('.item-location')
                      || addressFromTitle,
            description: getText('.item-description') || getText('[class*="description"]') || '',
            tags:  Array.from(a.querySelectorAll('.item-tag, [class*="tag"]'))
                       .map(t => (t.innerText || t.textContent || '').trim())
                       .filter(Boolean),
            img: (() => {
                // picture source srcset is ALWAYS present in the HTML (not
                // lazy-loaded), so check it FIRST before falling back to img.src
                // which may still point to a placeholder at extraction time.
                const srcEl = a.querySelector('picture source');
                if (srcEl) {
                    const raw = srcEl.getAttribute('srcset') || srcEl.srcset || '';
                    // srcset may be comma-separated; take the first URL token
                    const url = raw.split(',')[0].split(' ')[0].trim();
                    if (url && url.startsWith('http')) return url;
                }
                // Fallback: img element data attributes / resolved src
                const imgEl = a.querySelector('img.item-multimedia__image')
                           || a.querySelector('img[data-src]')
                           || a.querySelector('img');
                if (!imgEl) return '';
                return imgEl.getAttribute('data-src')
                    || imgEl.getAttribute('data-lazy')
                    || imgEl.getAttribute('data-original')
                    || imgEl.src
                    || '';
            })(),
            latitude:    a.getAttribute('data-latitude')  || '',
            longitude:   a.getAttribute('data-longitude') || '',
            agency:      getText('.item-agency')
                      || getText('[class*="agency"]')
                      || getText('.advertiser-name')
                      || getText('[class*="advertiser"]')
                      || '',
        };
    });
})())
"""

_JSON_CHECK_JS = r"""
JSON.stringify((() => {
    if (window.idealista && window.idealista.searchPageData)
        return window.idealista.searchPageData;
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of scripts) {
        try {
            const d = JSON.parse(s.textContent);
            if (d['@type'] === 'ItemList') return d;
        } catch (e) {}
    }
    return null;
})())
"""

# Minimum seconds to wait for Idealista's JS-rendered listings to appear.
# Debug showed the page takes ~12s to render article.item elements.
_RENDER_POLL_INTERVAL = 1.0    # seconds between polls
_RENDER_TIMEOUT       = 25.0   # give up waiting after this many seconds


async def _wait_for_render(tab) -> bool:
    """
    Poll until article.item elements appear on the page (JS render complete).
    Returns True when listings are found, False on timeout.
    """
    deadline = time.time() + _RENDER_TIMEOUT
    while time.time() < deadline:
        try:
            count = await tab.evaluate(
                "document.querySelectorAll('article.item').length"
            )
            if count and int(count) > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(_RENDER_POLL_INTERVAL)
    return False


async def _extract_listings_from_page(tab) -> list:
    """
    Extract raw listing dicts from the current Idealista page.
    First polls until the page renders (JS-heavy, ~12s), then tries:
      1. Embedded JSON (window.idealista.searchPageData or ld+json ItemList)
      2. Live DOM extraction via article.item CSS selector
    Returns JSON.stringify-parsed plain Python dicts.
    """
    rendered = await _wait_for_render(tab)
    if not rendered:
        print(" [no-render]", end="", flush=True)
        # Continue anyway — maybe the page has a different structure

    # Try embedded JSON first — more reliable when available
    try:
        json_str = await tab.evaluate(_JSON_CHECK_JS)
        if json_str and isinstance(json_str, str) and json_str.strip() not in ("null", ""):
            json_data = json.loads(json_str)
            if json_data and isinstance(json_data, dict) and json_data.get("@type") == "ItemList":
                parsed = _parse_ld_json(json_data)
                if parsed:
                    print(" [json]", end="", flush=True)
                    return parsed
    except Exception:
        pass

    # Fall back to live DOM extraction (always returns JSON string now)
    try:
        result_str = await tab.evaluate(_EXTRACT_JS)
        if result_str and isinstance(result_str, str):
            result = json.loads(result_str)
            if isinstance(result, list):
                return result
        # Older nodriver might auto-parse the JSON — handle both cases
        elif isinstance(result_str, list):
            return result_str
    except Exception as e:
        print(f" [dom-err:{type(e).__name__}]", end="", flush=True)

    return []


def _parse_ld_json(data: dict) -> list:
    """Convert a ld+json ItemList into our raw listing dict format."""
    result = []
    for item in data.get("itemListElement", []):
        thing   = item.get("item", item)
        url     = thing.get("url", "")
        lid     = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        price   = (thing.get("offers") or {}).get("price") or thing.get("price")
        addr    = thing.get("address", "")
        addr_str = (addr.get("streetAddress", "") if isinstance(addr, dict) else str(addr))
        geo     = thing.get("geo") or {}
        result.append({
            "id":          lid,
            "url":         url,
            "title":       thing.get("name", ""),
            "price_text":  str(price) if price else "",
            "size_text":   "",
            "rooms_text":  "",
            "floor_text":  "",
            "address":     addr_str,
            "description": thing.get("description", ""),
            "tags":        [],
            "img":         thing.get("image", ""),
            "latitude":    str(geo.get("latitude",  "")) if isinstance(geo, dict) else "",
            "longitude":   str(geo.get("longitude", "")) if isinstance(geo, dict) else "",
            "agency":      "",
        })
    return result


# ── Parse helpers ─────────────────────────────────────────────────────────────

def _parse_price(text: str) -> Optional[int]:
    """Extract integer monthly rent from strings like '€ 1.200 /mese' or '1200'."""
    digits = _re.sub(r"[^\d]", "", str(text))
    if not digits:
        return None
    v = int(digits)
    return v if 100 <= v <= 50_000 else None   # guard against noise


def _parse_sqm(text: str) -> Optional[int]:
    """Extract integer sqm from strings like '80 m²' or '80mq'."""
    m = _re.search(r"(\d+)", str(text))
    if not m:
        return None
    v = int(m.group(1))
    return v if 10 <= v <= 1_000 else None


_ROOM_NAMES = {
    "monolocale": 1, "bilocale": 2, "trilocale": 3,
    "quadrilocale": 4, "pentalocale": 5, "esalocale": 6,
}


def _parse_rooms(text: str) -> Optional[int]:
    """Extract room count from strings like '3 locali' or 'bilocale'."""
    tl = str(text).lower()
    for word, n in _ROOM_NAMES.items():
        if word in tl:
            return n
    m = _re.search(r"(\d+)\s*local", tl)
    return int(m.group(1)) if m else None


def _extract_neighbourhood(address: str) -> str:
    """
    Extract neighbourhood from Idealista address strings.
    Typical format: "Via X N, Neighbourhood, Milano MI"
    Returns the last meaningful segment before city suffixes.
    City segments are detected by their first token being a known city name.
    """
    _CITY_TOKENS = {"milano", "mi", "milan"}
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # Drop any part whose first word is a city name (catches "Milano", "Milano MI", "MI")
    parts = [p for p in parts
             if (p.split()[0].lower() if p.split() else "") not in _CITY_TOKENS]
    if len(parts) >= 2:
        return parts[-1]    # last meaningful segment = neighbourhood
    return parts[0] if parts else address.strip()


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


# ── Listing parser ────────────────────────────────────────────────────────────

def parse_idealista_listing(raw: dict) -> Optional[dict]:
    """
    Convert raw DOM-extracted fields into the standard listing dict.
    Field names match fetch_rentals.parse_rental() exactly so both sources
    are transparently merged and scored in rentals_latest.json.
    Returns None when price or sqm cannot be reliably parsed.
    """
    listing_id = str(raw.get("id", "")).strip()
    if not listing_id:
        return None

    # Monthly rent €
    price = _parse_price(raw.get("price_text", ""))
    if not price:
        return None

    # Surface m²
    sqm = _parse_sqm(raw.get("size_text", ""))
    if not sqm:
        return None

    ask_psqm = round(price / sqm, 2)   # €/m²/month

    # Rooms
    rooms = _parse_rooms(raw.get("rooms_text", ""))

    # Floor — Idealista floor strings look like "Piano rialzato con ascensore"
    # or "6º piano con ascensore". Strip the "con/senza ascensore" suffix before
    # calling parse_floor() so the floor token is recognised.
    floor_text_raw = (raw.get("floor_text") or "").strip()
    floor_for_parse = _re.sub(
        r'\s+(?:con|senza)\s+ascensore.*', '', floor_text_raw,
        flags=_re.IGNORECASE
    ).strip()
    floor_n, floor_label = parse_floor(floor_for_parse)
    is_below_ground = floor_n is not None and floor_n < 0
    is_ground_floor  = floor_n == 0

    # URL — ensure absolute
    url = raw.get("url", "")
    if url and not url.startswith("http"):
        url = "https://www.idealista.it" + url

    # Neighbourhood — extract from address then normalise to Immobiliare canonical names
    address       = raw.get("address", "").strip()
    neighbourhood = _extract_neighbourhood(address)

    # Normalise Idealista sub-zone names to Immobiliare canonical area names so
    # they match MILAN_AREAS and the dashboard filter works bidirectionally.
    # The synonym map is loaded once at import from neighbourhood_synonyms.json.
    neighbourhood = _NEIGHBOURHOOD_SYNONYMS.get(neighbourhood, neighbourhood)

    # Coordinates
    lat = _safe_float(raw.get("latitude"))
    lon = _safe_float(raw.get("longitude"))

    # Feature flags — inferred from free-text tags + description + floor text.
    # Include floor_text_raw so "con ascensore" phrases are detected even when
    # the description is empty (common on Idealista search-result cards).
    tags     = [t.lower() for t in (raw.get("tags") or [])]
    desc     = raw.get("description", "").lower()
    all_text = " ".join(tags) + " " + desc + " " + floor_text_raw.lower()

    # Use None (unknown) rather than False so the dashboard can distinguish
    # "definitely no balcony" from "we don't know" — use True only when confirmed.
    has_balcony = True if any(k in all_text for k in ("balcon", "terrazza", "giardino")) else None
    has_parking = True if any(k in all_text for k in ("box", "garage", "parcheggio", "posto auto")) else None
    elevator    = True if any(k in all_text for k in ("ascensor", "lift", "elevator")) else None
    furnished   = True if any(k in all_text for k in ("arredato", "arredat", "furnished")) else None

    omi = match_omi(neighbourhood)

    return {
        # ── Identity ──────────────────────────────────────────────────────────
        "id":                 f"id_{listing_id}",   # "id_" prefix prevents collisions
        "source":             SOURCE,               # "idealista"
        "city":               CITY_LABEL,
        "city_key":           CITY_KEY,
        "title":              raw.get("title", "").strip(),
        "neighbourhood":      neighbourhood,
        "address":            address,
        "latitude":           lat,
        "longitude":          lon,
        "url":                url,
        "thumbnail":          raw.get("img") or None,
        # ── Price ─────────────────────────────────────────────────────────────
        "rent_mo":            price,        # monthly rent € — same field name as fetch_rentals
        "sqm":                sqm,
        "ask_psqm":           ask_psqm,    # €/m²/month
        "spese_condominiali": None,         # not available on search results page
        # ── Physical ──────────────────────────────────────────────────────────
        "rooms":              rooms,
        "floor":              raw.get("floor_text") or None,
        "floor_n":            floor_n,
        "floor_label":        floor_label,
        "is_below_ground":    is_below_ground,
        "is_ground_floor":    is_ground_floor,
        "elevator":           elevator,
        "is_external":        None,
        "energy_class":       None,
        "year_built":         None,
        "bathrooms":          None,
        "has_balcony":        has_balcony,
        "has_parking":        has_parking,
        "heating_type":       None,
        "furnished":          furnished,
        "photo_count":        None,
        "days_on_market":     None,
        "published_date":     None,
        "condition":          None,
        # ── Agency ────────────────────────────────────────────────────────────
        "agency_id":          "",
        "agency_name":        (raw.get("agency") or "").strip(),
        "agency_type":        "",
        "agency_url":         "",
        # ── OMI ───────────────────────────────────────────────────────────────
        "omi":                omi,          # stripped before JSON export (same as fetch_rentals)
        "omi_zona":           None,         # filled by geo enrichment pass
        "omi_loc_mid":        None,
        # ── Tracking ──────────────────────────────────────────────────────────
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
        "first_seen_date":    None,         # stamped by run_once()
    }


# ── Idealista-specific status writer ─────────────────────────────────────────

def write_idealista_status(new_count: int, running: bool = False,
                           skipped_areas: list = None):
    """
    Merge Idealista scanner state into scanner_status.json under key 'idealista'.
    Reads existing file first so the Immobiliare status block is preserved.
    Writes to both DASHBOARD_DIR and BASE_DIR copies.
    """
    payload = {
        "last_run":      datetime.now().isoformat(timespec="seconds"),
        "new_count":     new_count,
        "running":       running,
        "skipped_areas": skipped_areas or [],
    }
    for path in (DASHBOARD_DIR / "scanner_status.json", STATUS_PATH):
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                pass
        existing["idealista"] = payload
        try:
            path.write_text(json.dumps(existing, ensure_ascii=False))
        except Exception:
            pass


# ── Async fetch loop ──────────────────────────────────────────────────────────

async def _fetch_async(pages: int, area_slugs: list, max_rent: int,
                       min_sqm: int, min_rooms: int,
                       delay: float, browser) -> tuple:
    """
    Navigate Idealista rental pages and return (listings, skipped_areas).
    Mirrors fetch_rentals._fetch_async() exactly, replacing __NEXT_DATA__
    extraction with live HTML DOM + optional embedded JSON parsing.
    """
    all_items: list  = []
    seen_ids:  set   = set()   # global dedup across all areas
    skipped_areas    = []

    targets = area_slugs if area_slugs else [None]

    # ── Initial load + CAPTCHA gate ───────────────────────────────────────────
    # Load the first target URL before entering the area loop so we can
    # detect and handle a CAPTCHA without wasting time on other areas.
    # _check_and_handle_captcha now calls _wait_for_render internally, so no
    # extra sleep is needed here.
    first_url = build_idealista_url(1, targets[0], max_rent, min_sqm, min_rooms)
    print(f"\n    [debug] GET {first_url} (initial + CAPTCHA check)", flush=True)
    tab = await browser.get(first_url)
    await asyncio.sleep(2.0)   # brief navigation settle — _wait_for_render polls the rest

    if not await _check_and_handle_captcha(tab):
        return [], [{"name": "all", "reason": "CAPTCHA not solved in time"}]

    # ── Per-area page loop ────────────────────────────────────────────────────
    for area_slug in targets:
        label = area_slug or "all Milano"
        print(f"\n    area: {label}", end="", flush=True)

        try:
            area_ids_seen: set = set()
            canonical_slug     = area_slug
            area_got_results   = False

            for page in range(1, pages + 1):
                url = build_idealista_url(page, canonical_slug, max_rent, min_sqm, min_rooms)
                print(f"\n    [debug] GET {url}", flush=True)
                tab = await browser.get(url)
                await asyncio.sleep(2.0)   # brief settle; _wait_for_render (inside extraction) handles the rest

                # Mid-session CAPTCHA check on every page (shorter timeout)
                # NOTE: this now only fires when CAPTCHA keywords are detected,
                # not on slow-loading pages.
                if not await _check_and_handle_captcha(tab, timeout=60):
                    skipped_areas.append({"name": label,
                                          "reason": "CAPTCHA timeout mid-scan"})
                    print(f" [captcha-timeout — skipping area]", end="", flush=True)
                    break

                # Resolve redirect on page 1 — Idealista sometimes rewrites slugs.
                # With path-based filters the area slug sits between
                # /milano-milano/ and /con- so we must capture only that segment.
                if page == 1 and area_slug:
                    try:
                        actual_href = await tab.evaluate("window.location.href")
                        if actual_href:
                            # Match slug segment that precedes /con- or end of path.
                            # Handles both zone URLs (/milano/{slug}/con-) and
                            # city-wide URLs (/milano-milano/{slug}/con-).
                            m = (_re.search(r"/milano(?:-milano)?/([^/?#]+)/con-", actual_href)
                                 or _re.search(r"/milano(?:-milano)?/([^/?#]+?)/?$", actual_href))
                            if m:
                                resolved = m.group(1)
                                if resolved != canonical_slug:
                                    print(f" [→{resolved}]", end="", flush=True)
                                    canonical_slug = resolved
                    except Exception:
                        pass

                raw_listings = await _extract_listings_from_page(tab)
                if not raw_listings:
                    print(f" (empty, done)", end="", flush=True)
                    break

                area_got_results = True

                # Detect page recycling: if every ID on this page was seen before,
                # the site is returning the same page repeatedly (pag= ignored).
                page_ids = {r.get("id", "") for r in raw_listings if r.get("id")}
                if page > 1 and page_ids and page_ids.issubset(area_ids_seen):
                    print(f" (repeat page, done)", end="", flush=True)
                    break
                area_ids_seen.update(page_ids)

                new_this_page = 0
                for raw in raw_listings:
                    parsed = parse_idealista_listing(raw)
                    if not parsed:
                        continue
                    lid = parsed["id"]   # already "id_XXXXXX"
                    if lid in seen_ids:
                        continue
                    parsed["_fetched_area"] = canonical_slug or "all"
                    seen_ids.add(lid)
                    all_items.append(parsed)
                    new_this_page += 1

                # Detect "next page" link — Idealista uses several different selectors
                try:
                    has_next = await tab.evaluate(
                        "!!(document.querySelector('a.icon-arrow-right-after')"
                        "|| document.querySelector('li.next a')"
                        "|| document.querySelector('a[rel=\"next\"]')"
                        "|| document.querySelector('.pagination-next a'))"
                    )
                except Exception:
                    has_next = False

                print(f" p{page}(+{new_this_page})", end="", flush=True)
                if not has_next:
                    break

            if not area_got_results and area_slug:
                reason = ("no results — slug may be invalid or "
                          "not found on Idealista.it")
                skipped_areas.append({"name": area_slug, "reason": reason})
                print(f"\n    [warn] No results for area '{label}'", flush=True)

        except Exception as exc:
            # One area failing must never stop the rest of the scan
            reason = f"{type(exc).__name__}: {exc}"
            skipped_areas.append({"name": label, "reason": reason})
            print(f"\n    [error] Area '{label}' failed: {reason} — skipping",
                  flush=True)
            # Recover browser session: navigate to blank page to reset CDP state
            try:
                tab = await browser.get("about:blank")
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

    return all_items, skipped_areas


def fetch_idealista(pages: int = 3, area_names: list = None, max_rent: int = 0,
                    min_sqm: int = 0, min_rooms: int = 0,
                    delay: float = 3.0) -> tuple:
    """
    One-shot fetch of Milano rental listings from Idealista.it via nodriver.
    delay defaults to 3.0s — Idealista applies stricter bot detection.
    Returns (listings, skipped_areas).
    """
    pre_skipped: list = []
    valid_slugs: list = []
    for name in (area_names or []):
        name = name.strip()
        if not name:
            continue
        slug = to_url_slug(name)
        if not slug:
            pre_skipped.append({"name": name, "reason": "converts to empty URL slug"})
            print(f"  [warn] Area '{name}' has no valid Idealista URL slug — skipped.")
        else:
            valid_slugs.append(slug)

    if area_names and valid_slugs:
        n_valid   = len(valid_slugs)
        n_skipped = len(pre_skipped)
        desc      = ", ".join(valid_slugs)
        print(f"  [scan] {len(area_names)} area(s) requested · "
              f"{n_valid} valid · {n_skipped} skipped (bad slug)")
        if pre_skipped:
            print(f"  [scan] Skipped: {', '.join(s['name'] for s in pre_skipped)}")
    else:
        desc = "all Milano"

    area_slugs = valid_slugs
    print(f"  Fetching Idealista rentals ({desc})…", end="", flush=True)

    async def _run():
        browser = await uc.start(
            browser_executable_path=CHROME_PATH,
            headless=False,
            lang="it-IT",
        )
        try:
            items, skipped = await _fetch_async(
                pages, area_slugs, max_rent, min_sqm, min_rooms, delay, browser
            )
        finally:
            browser.stop()
        return items, skipped

    t0 = time.time()
    items, fetch_skipped = asyncio.run(_run())
    skipped_areas = pre_skipped + fetch_skipped
    n_fetched = len(items)
    print(f"\n  [fetch]  Idealista: {n_fetched} listings fetched ({pages} pages)")

    if not items:
        print("  [warn]   No Idealista listings parsed — check selectors or CAPTCHA")
        return items, skipped_areas

    # ── Cache-aware geo enrichment (identical to fetch_rentals pattern) ────────
    INLINE_ENRICH_CAP = 200

    try:
        import enrichment_cache as _ecache
        from enrich_geo import enrich_batch as _enrich_batch

        _ecache.load()

        new_items = [l for l in items if _ecache.get(SOURCE, l["id"]) is None]
        n_cached  = n_fetched - len(new_items)
        print(f"  [cache]  {n_cached} already enriched, {len(new_items)} new")

        if new_items:
            if len(new_items) > INLINE_ENRICH_CAP:
                print(f"  [enrich] {len(new_items)} new listings — skipping inline enrichment "
                      f"(>{INLINE_ENRICH_CAP} cap). Use 'Enrich geo' in the dashboard.",
                      flush=True)
            else:
                print(f"  [enrich] Enriching {len(new_items)} new listings in parallel…",
                      flush=True)
                t_enrich = time.time()
                try:
                    geo_results = _enrich_batch(new_items)
                    print(f"  [enrich] Done in {time.time() - t_enrich:.1f}s")
                    _ecache.bulk_save(
                        [(SOURCE, l["id"], g) for l, g in zip(new_items, geo_results)]
                    )
                except Exception as _geo_exc:
                    print(f"  [enrich] Geo enrichment failed "
                          f"({type(_geo_exc).__name__}): {_geo_exc}"
                          f" — saving without geo data", flush=True)

        # Merge cached geo data into every listing
        for listing in items:
            cached = _ecache.get(SOURCE, listing["id"])
            if cached:
                listing.update({k: v for k, v in cached.items() if k != "enriched_at"})

        # OMI polygon fields for listings still missing them (fast, in-memory)
        try:
            import omi_lookup as _omi_lookup
            if _omi_lookup.ZONES:
                _need_omi = [
                    l for l in items
                    if l.get("omi_loc_mid") is None
                    and l.get("latitude") and l.get("longitude")
                ]
                _omi_updates = []
                for _l in _need_omi:
                    _zone, _src = _omi_lookup.lookup(float(_l["latitude"]),
                                                      float(_l["longitude"]))
                    if _zone:
                        _omi_f = {
                            "omi_zona":      _zone["zona"],
                            "omi_fascia":    _zone["fascia"],
                            "omi_descr":     _zone["descr"],
                            "omi_loc_min":   _zone["loc_min"],
                            "omi_loc_max":   _zone["loc_max"],
                            "omi_loc_mid":   _zone["loc_mid"],
                            "omi_compr_min": _zone["compr_min"],
                            "omi_compr_max": _zone["compr_max"],
                            "omi_compr_mid": _zone["compr_mid"],
                            "omi_source":    _src,
                        }
                        _l.update(_omi_f)
                        _omi_updates.append(
                            (SOURCE, _l["id"],
                             {**(_ecache.get(SOURCE, _l["id"]) or {}), **_omi_f})
                        )
                if _omi_updates:
                    _ecache.bulk_save(_omi_updates)
                    print(f"  [omi]    polygon fields applied to "
                          f"{len(_omi_updates)} listings")
        except Exception as _omi_exc:
            print(f"  [omi]    polygon step skipped: {_omi_exc}", file=sys.stderr)

        print(f"  [merge]  {n_fetched} listings enriched and ready")

    except ImportError:
        pass   # enrichment_cache / enrich_geo not installed — skip geo step

    if skipped_areas:
        print(f"  [scan]   {len(skipped_areas)} area(s) skipped: "
              + ", ".join(s["name"] for s in skipped_areas))

    print(f"  [done]   Run complete in {time.time() - t0:.1f}s total")
    return items, skipped_areas


# ── Run cycle ─────────────────────────────────────────────────────────────────

def run_once(args) -> list:
    """Execute one fetch-score-write cycle. Returns newly seen listings."""
    if args.areas:
        area_names = [a.strip() for a in args.areas.split(",") if a.strip()]
    else:
        # Default: load active zones from idealista_area_settings.json,
        # mirroring how fetch_rentals.py reads from area_settings.json.
        # This scans each active Idealista zone separately so we get paginated
        # results per zone rather than hitting the city-wide listing cap.
        area_names = _load_idealista_areas()
        if area_names:
            print(f"  [scan] Using {len(area_names)} active Idealista zone(s) "
                  f"from idealista_area_settings.json", flush=True)
        else:
            # No zones configured → fall back to city-wide scan
            print("  [scan] No active zones found — scanning all Milano", flush=True)

    raw, skipped_areas = fetch_idealista(
        pages=args.pages,
        area_names=area_names,
        max_rent=args.max_rent   or 0,
        min_sqm=args.min_sqm    or 0,
        min_rooms=args.min_rooms or 0,
        delay=args.delay,
    )

    if not raw:
        print("  ✗ No Idealista listings fetched.")
        write_idealista_status(new_count=0, skipped_areas=skipped_areas)
        return []

    # Score through the same pipeline as Immobiliare listings
    scored = score_all(raw)

    # Stamp first_seen_date — shared seen_ids.json with fetch_rentals;
    # "id_" prefix ensures no collision with Immobiliare numeric IDs.
    seen      = load_seen_ids()
    today_str = str(date.today())
    for l in scored:
        lid = l["id"]
        l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str

    # Derive canonical area slugs that were actually scanned
    fetched_slugs = {l.get("_fetched_area") for l in scored if l.get("_fetched_area")}

    # Merge into rentals_latest.json — Immobiliare entries are untouched
    write_output(scored, source=SOURCE, scanned_area_slugs=fetched_slugs or None)

    # Update shared seen_ids.json
    new_listings = [l for l in scored if l["id"] not in seen]
    for l in scored:
        seen[l["id"]] = {"first_seen_date": l["first_seen_date"]}
    save_seen_ids(seen)

    write_idealista_status(new_count=len(new_listings), skipped_areas=skipped_areas)
    print(f"  ✓ {len(scored)} Idealista listings written · {len(new_listings)} new")

    # Netlify deploy — only when config is present and we have data
    if NETLIFY_CONFIG_PATH.exists() and scored:
        _netlify_deploy()

    return new_listings


# ── Daemon ────────────────────────────────────────────────────────────────────

def daemon_loop(args):
    try:
        from email_digest import send_digest, load_config as load_email_config
        _email_available = True
    except ImportError:
        _email_available = False

    log_path = BASE_DIR / "idealista_scanner.log"
    log_fh   = open(log_path, "a", buffering=1)   # line-buffered

    def log(msg: str):
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        print(line, file=log_fh, flush=True)

    log(f"Idealista daemon started — interval {DAEMON_INTERVAL_SEC // 60} min, "
        f"output {OUTPUT_PATH}")

    while True:
        log("Running Idealista scan…")
        new_listings = []
        try:
            new_listings = run_once(args)

            if _email_available:
                email_cfg = load_email_config()

                def _should_send():
                    today = str(date.today())
                    return not (DIGEST_SENT_PATH.exists()
                                and DIGEST_SENT_PATH.read_text().strip() == today)

                want_email = email_cfg.get("enabled", False) or args.email
                if want_email and new_listings and _should_send():
                    log(f"Sending digest for {len(new_listings)} new listing(s)…")
                    send_digest(new_listings, email_cfg)
                    mark_digest_sent()

            log(f"Scan done — {len(new_listings)} new Idealista listing(s)")

        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                log("Interrupted — stopping Idealista daemon.")
                log_fh.close()
                raise
            log(f"✗ Scan error: {e}\n{traceback.format_exc()}")
            try:
                write_idealista_status(new_count=0)
            except Exception:
                pass

        log(f"Sleeping {DAEMON_INTERVAL_SEC // 60} min…")
        try:
            time.sleep(DAEMON_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Interrupted during sleep — stopping Idealista daemon.")
            log_fh.close()
            raise


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch Milano rental listings from Idealista.it and score against OMI."
    )
    p.add_argument("--pages",     type=int,   default=None,
                   help="Pages to fetch (approx 30 listings/page, default from scan_prefs.json)")
    p.add_argument("--areas",     type=str,   default="",
                   help="Comma-separated Idealista zone names (slugified automatically). "
                        "Default: scan all Milano city-level (most reliable).")
    p.add_argument("--max-rent",  type=int,   default=None,
                   help="Max monthly rent € (0 = no filter, overrides scan_prefs)")
    p.add_argument("--min-sqm",   type=int,   default=None,
                   help="Min surface m² (0 = no filter, overrides scan_prefs)")
    p.add_argument("--min-rooms", type=int,   default=None,
                   help="Min number of rooms (0 = no filter, overrides scan_prefs)")
    p.add_argument("--delay",     type=float, default=3.0,
                   help="Seconds between page loads (default 3.0 — stricter than Immobiliare)")
    p.add_argument("--daemon",    action="store_true",
                   help="Loop forever, fetching every 60 minutes")
    p.add_argument("--email",     action="store_true",
                   help="Send daily email digest (requires --daemon; see email_digest.py)")
    p.add_argument("--netlify",   action="store_true",
                   help="Deploy dashboard/ to Netlify after each scan "
                        "(requires netlify_config.json with site_id + token)")
    return p.parse_args()


def main():
    args = parse_args()

    # Apply dashboard scan preferences as defaults.
    # Explicit CLI flags (even 0) always win — prefs only fill in when arg is None.
    _prefs = _load_scan_prefs()
    if args.pages is None:
        args.pages = int(_prefs.get("pages", 3))
    if args.max_rent is None:
        args.max_rent = int(_prefs.get("max_rent", 0))
    if args.min_rooms is None:
        args.min_rooms = int(_prefs.get("min_rooms", 0))
    if args.min_sqm is None:
        args.min_sqm = int(_prefs.get("min_sqm", 0))

    print(f"\n{'─'*52}")
    print(f"  Idealista Scorer — rental fetch (Milano)")
    print(f"  Pages    : {args.pages} (~{args.pages * 30} listings max)")
    if args.areas:
        print(f"  Areas    : {args.areas}")
    else:
        print(f"  Areas    : all Milano (city-level — default)")
    if args.max_rent:
        print(f"  Max rent : €{args.max_rent}/mo")
    else:
        print(f"  Max rent : (no filter)")
    if args.min_rooms:
        print(f"  Min rooms: {args.min_rooms}+")
    else:
        print(f"  Min rooms: (no filter)")
    print(f"  Mode     : {'daemon (every %d min)' % (DAEMON_INTERVAL_SEC // 60) if args.daemon else 'one-shot'}")
    if args.netlify:
        print(f"  Netlify  : enabled (→ direct deploy after each scan)")
    print(f"{'─'*52}")
    print(f"  NOTE: Idealista may show a CAPTCHA on first visit.")
    print(f"        Solve it in the Chrome window — the scan resumes automatically.")
    print(f"{'─'*52}\n")

    if args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
