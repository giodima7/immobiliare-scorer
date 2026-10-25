#!/usr/bin/env python3
"""
fetch_idealista.py
──────────────────
Fetches Milano rental listings from Idealista.it via nodriver (same Edge
approach as fetch_rentals.py), scores each one against OMI rent benchmarks,
and merges the results into dashboard/rentals_latest.json alongside
Immobiliare.it data.

Usage (one-shot):
    python3 fetch_idealista.py
    python3 fetch_idealista.py --pages 5
    python3 fetch_idealista.py --areas navigli-bocconi,porto-vittoria --max-rent 2000

By default the scanner derives which Idealista zones to scan from the active
Immobiliare areas in area_settings.json (single source of truth).  Each active
Immobiliare neighbourhood is matched to the Idealista macro-zone that contains
it via idealista_neighbourhood_map.json + neighbourhood_synonyms.json.

Daemon mode (loops every 60 min):
    python3 fetch_idealista.py --daemon

First run — CAPTCHA:
    Idealista may show a DataDome CAPTCHA on first visit.  The scanner will
    pause and wait up to 2 minutes for you to solve it manually in the Edge
    window.  After solving it once the session persists and subsequent runs
    complete automatically.

    Watch the terminal: if you see
        ⚠ CAPTCHA detected — please solve it in the browser window.
    switch to the Edge window, solve the puzzle, then return to the terminal.
    After "✓ CAPTCHA solved" the scan continues automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re as _re
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import nodriver as uc

from browser_session import BrowserSession, PageTimeout, timed

# ── Shared infrastructure imported from fetch_rentals ────────────────────────
# All OMI data, scoring, enrichment, and persistence live in fetch_rentals so
# this file only adds Idealista-specific URL building and HTML/DOM parsing.
from fetch_rentals import (
    # Paths
    BASE_DIR, DASHBOARD_DIR, SEEN_IDS_PATH, OUTPUT_PATH,
    NETLIFY_CONFIG_PATH, STATUS_PATH, DIGEST_SENT_PATH,
    # Constants
    CITY_KEY, CITY_LABEL, EDGE_PATH, DAEMON_INTERVAL_SEC, DAEMON_RETRY_SEC,
    last_scan_at, mark_scan_done, sleep_until, wait_and_drip, run_marked, rescore_output, sweep_slugs_for_today, record_sweeps,
    _parse_condo_from_description,
    # OMI + scoring
    match_omi, score_all,
    # Parsing helpers
    parse_floor, to_url_slug,
    # Persistence
    load_seen_ids, save_seen_ids, write_output,
    # Active areas
    _load_active_areas,
    # Netlify deploy + prefs
    _load_scan_prefs,
    # Daily digest helpers
    mark_digest_sent,
    # Spread scan helpers
    next_slot_area, DISCOVER_EVERY_SEC, DISCOVER_OVERLAP_STOP, SLOT_GAP_SEC,
    _queue_digest, send_pending_digest, _slot_log_path, read_photos_after_slot,
    _between_slots,
)

try:
    from scoring import score_all_sales as _score_all_sales
    from explain import explain_all_sales as _explain_all_sales
except ImportError:
    _score_all_sales = None
    _explain_all_sales = None

SOURCE      = "idealista"
SOURCE_SALE = "idealista_sale"

# ── City registry ───────────────────────────────────────────────────────────
# Idealista city slugs ARE NOT always the same as Immobiliare's. La Maddalena
# uses "la-maddalena" on Idealista vs "la_maddalena" as our internal code.
CITY_REGISTRY: dict[str, dict] = {
    "milano":       {"label": "Milano",       "slug": "milano",       "tokens": {"milano", "mi", "milan"}},
    "roma":         {"label": "Roma",         "slug": "roma",         "tokens": {"roma", "rm", "rome"}},
    "napoli":       {"label": "Napoli",       "slug": "napoli",       "tokens": {"napoli", "na", "naples"}},
    "la_maddalena": {"label": "La Maddalena", "slug": "la-maddalena", "tokens": {"la maddalena", "ss", "maddalena"}},
}

# Defaults — overwritten by _apply_city() once --city parses.
CITY_KEY    = "milano"
CITY_LABEL  = "Milano"
CITY_SLUG   = "milano"
CITY_TOKENS = {"milano", "mi", "milan"}

# URL templates. The Idealista URL structure is /<intent>/<slug>-<province-slug>/
# for city-wide pages and /<intent>/<slug>/ for per-zone pages. We mirror the
# same convention by reusing the same slug for both halves of the city-wide
# URL — works for every comune we currently support.
IDEALISTA_CITY_BASE      = f"https://www.idealista.it/affitto-case/{CITY_SLUG}-{CITY_SLUG}/"
IDEALISTA_ZONE_BASE      = f"https://www.idealista.it/affitto-case/{CITY_SLUG}/"
IDEALISTA_BASE           = IDEALISTA_CITY_BASE   # external alias
IDEALISTA_SALE_CITY_BASE = f"https://www.idealista.it/vendita-case/{CITY_SLUG}-{CITY_SLUG}/"
IDEALISTA_SALE_ZONE_BASE = f"https://www.idealista.it/vendita-case/{CITY_SLUG}/"
# Output paths derived per city (matches fetch_rentals / fetch_listings).
OUTPUT_PATH      = DASHBOARD_DIR / f"{CITY_KEY}_rentals_latest.json"
SALE_OUTPUT_PATH = DASHBOARD_DIR / f"{CITY_KEY}_sales_latest.json"


def _apply_city(city: str) -> None:
    """Update every per-city module global once --city is known."""
    global CITY_KEY, CITY_LABEL, CITY_SLUG, CITY_TOKENS
    global IDEALISTA_CITY_BASE, IDEALISTA_ZONE_BASE, IDEALISTA_BASE
    global IDEALISTA_SALE_CITY_BASE, IDEALISTA_SALE_ZONE_BASE
    global OUTPUT_PATH, SALE_OUTPUT_PATH
    if city not in CITY_REGISTRY:
        raise ValueError(f"unknown city '{city}'; known: {list(CITY_REGISTRY)}")
    cfg = CITY_REGISTRY[city]
    CITY_KEY    = city
    CITY_LABEL  = cfg["label"]
    CITY_SLUG   = cfg["slug"]
    CITY_TOKENS = cfg["tokens"]
    IDEALISTA_CITY_BASE      = f"https://www.idealista.it/affitto-case/{CITY_SLUG}-{CITY_SLUG}/"
    IDEALISTA_ZONE_BASE      = f"https://www.idealista.it/affitto-case/{CITY_SLUG}/"
    IDEALISTA_BASE           = IDEALISTA_CITY_BASE
    IDEALISTA_SALE_CITY_BASE = f"https://www.idealista.it/vendita-case/{CITY_SLUG}-{CITY_SLUG}/"
    IDEALISTA_SALE_ZONE_BASE = f"https://www.idealista.it/vendita-case/{CITY_SLUG}/"
    OUTPUT_PATH              = DASHBOARD_DIR / f"{CITY_KEY}_rentals_latest.json"
    SALE_OUTPUT_PATH         = DASHBOARD_DIR / f"{CITY_KEY}_sales_latest.json"
    # fetch_rentals reads OUTPUT_PATH from its own module — set there too
    # so legacy paths from imports stay in sync.
    try:
        from fetch_rentals import _apply_city as _apply_fr
        _apply_fr(city)
    except Exception:
        pass

# Path to the neighbourhood synonym map: Idealista sub-zone name → Immobiliare canonical name
_SYNONYMS_PATH          = BASE_DIR / "neighbourhood_synonyms.json"
# Path to the full Idealista zone→subzone map (used for reverse-matching)
_ZONE_MAP_PATH          = BASE_DIR / "idealista_neighbourhood_map.json"


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


# Immobiliare macro areas whose names share no word with any Idealista
# zone/subzone. Arco della Pace / Arena / Pagano sit between Sempione and
# Ariosto - Magenta, both in Idealista's "Fiera - De Angeli".
_AREA_ZONE_OVERRIDES: dict = {
    "arco-della-pace-arena-pagano": ["Fiera - De Angeli"],
}


def _match_zones_for_immo_areas(immo_areas: list) -> list:
    """
    Given a list of active Immobiliare area names, return the Idealista zone
    names whose subzones overlap with those areas.

    Active areas are Immobiliare macro slugs ("cenisio-sarpi-isola"), while
    zones carry display names ("Isola", "Rogoredo - Santa Giulia"). Both
    sides are slugified; each name is split on " - " into phrases, and a
    zone matches when one of its phrases is a whole run of words inside an
    area slug (or the area slug a run inside a phrase: "centro" ⊂
    "centro-storico"). Word-run matching, not prefix matching: the old
    raw-string startswith() compared slugs to display names, matched only
    6 of 18 zones for 17 areas, and let "naviglio grande" hit "navigli".

    Phrases come from the zone name, its subzone names and their
    synonym-mapped (Immobiliare-facing) names. A leading "corso"/"via"/
    "viale"/"piazza"/"zona" is dropped ("Corso Genova" → "genova").
    """
    if not _ZONE_MAP_PATH.exists():
        return []
    try:
        nmap = json.loads(_ZONE_MAP_PATH.read_text())
    except Exception:
        return []

    synonyms = _load_synonyms()
    targets  = [f"-{to_url_slug(a)}-" for a in immo_areas if a]
    _street_words = ("corso-", "via-", "viale-", "piazza-", "zona-")

    def _phrases(name: str) -> set:
        out = set()
        for part in name.split(" - "):
            ph = to_url_slug(part)
            for w in _street_words:
                if ph.startswith(w) and len(ph) > len(w):
                    ph = ph[len(w):]
            if ph:
                out.add(f"-{ph}-")
        return out

    matched = []
    for zone in nmap.get("zones", []):
        zone_name = zone.get("name", "")
        phrases = _phrases(zone_name)
        for sub in zone.get("subzones", []):
            raw = sub.get("name", "")
            phrases |= _phrases(raw) | _phrases(synonyms.get(raw, raw))
        if any(ph in t or t in ph for ph in phrases for t in targets):
            matched.append(zone_name)

    for a in immo_areas:
        for zone_name in _AREA_ZONE_OVERRIDES.get(to_url_slug(a or ""), []):
            if zone_name not in matched:
                matched.append(zone_name)
    return matched


def _load_idealista_areas() -> list:
    """
    Derive the Idealista zones to scan from the active Immobiliare areas.
    Single source of truth: area_settings.json — no separate Idealista picker needed.

    Returns the Idealista zone names that cover at least one active Immobiliare
    neighbourhood, using _match_zones_for_immo_areas().  Falls back to the
    city-wide scan (empty list) when no match is found.
    """
    immo_areas = _load_active_areas()
    if not immo_areas:
        return []
    zones = _match_zones_for_immo_areas(immo_areas)
    if zones:
        print(f"  [zones] Matched {len(immo_areas)} Immobiliare area(s) "
              f"→ {len(zones)} Idealista zone(s): "
              + ", ".join(zones), flush=True)
    else:
        print("  [zones] No Idealista zones matched active Immobiliare areas "
              "— falling back to city-wide scan", flush=True)
    return zones


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
                        min_rooms: int = 0, mode: str = "rental") -> str:
    """
    Build a fully parameterised Idealista.it search URL using the
    correct path-segment filter syntax.

    Idealista uses TWO different URL bases depending on mode:
      Rental city-wide: /affitto-case/milano-milano/
      Rental per-zone:  /affitto-case/milano/{zone_slug}/
      Sale city-wide:   /vendita-case/milano-milano/
      Sale per-zone:    /vendita-case/milano/{zone_slug}/

    Examples
    --------
    No filters (city-wide rental):
      https://www.idealista.it/affitto-case/milano-milano/con-affitto-lungo-termine/

    With zone + filters (rental):
      https://www.idealista.it/affitto-case/milano/navigli-bocconi/
      con-prezzo_3000,bilocali-2,trilocali-3,quadrilocali-4,5-locali-o-piu,
      affitto-lungo-termine/

    Sale (city-wide):
      https://www.idealista.it/vendita-case/milano-milano/con-prezzo_500000/
    """
    if mode == "sale":
        if area_slug:
            base = IDEALISTA_SALE_ZONE_BASE + f"{area_slug}/"
        else:
            base = IDEALISTA_SALE_CITY_BASE
    else:
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
    # For rentals only: always request long-term (excludes holiday lets)
    if mode != "sale":
        tokens.append("affitto-lungo-termine")

    if tokens:
        url = base + "con-" + ",".join(tokens) + "/"
    else:
        url = base
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
        page_text = await timed(tab.evaluate("document.body?.innerText || ''"),
                                what="read page text")
    except PageTimeout:
        raise
    except Exception:
        return True   # can't introspect page — assume OK

    lpt = (page_text or "").lower()
    captcha_signals = [
        "captcha"  in lpt,
        "datadome" in lpt,
        "verifica del dispositivo" in lpt,   # Idealista's bot check (2026-10-18)
        "verifying your device" in lpt,
    ]

    if not any(captcha_signals):
        return True   # page looks clean

    print("\n  [idealista] ⚠ CAPTCHA detected — please solve it in the browser window.")
    print(f"  [idealista] Waiting up to {timeout}s for you to complete it…")

    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            pt = await timed(tab.evaluate("document.body?.innerText || ''"),
                             what="read page text")
            lpt2 = (pt or "").lower()
            if not any(s in lpt2 for s in ("captcha", "datadome", "verifica del dispositivo", "verifying your device")):
                print("  [idealista] ✓ CAPTCHA solved — continuing scan")
                return True
        except PageTimeout:
            raise
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

        // Idealista encodes the full address in the listing title in two forms:
        //   "Bilocale in Via Roma, 10, Navigli, Milano"   ← preposition "in"
        //   "Trilocale a Lodi - Brenta, Milano"           ← preposition "a" (at)
        //
        // We extract everything after the preposition as the address string.
        // "a" is only treated as a location marker when followed by an uppercase
        // letter (proper noun), so "appartamento a due piani" does NOT match.
        const titleText = getText('a.item-link') || getText('.item-title');

        let _addrStart  = titleText.search(/\s+in\s+/i);
        let _addrPrefix = /^\s+in\s+/i;
        if (_addrStart < 0) {
            // Try Italian "a [Location]" — uppercase lookahead prevents false matches
            const _mA = titleText.match(/\s+a\s+(?=[A-ZÀ-ɏ])/);
            if (_mA) {
                _addrStart  = _mA.index;
                _addrPrefix = /^\s+a\s+/;
            }
        }
        const addressFromTitle = _addrStart >= 0
            ? titleText.slice(_addrStart).replace(_addrPrefix, '').trim()
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
            // Address: prefer explicit DOM element; but treat Idealista's
            // "Posizione approssimativa." placeholder as equivalent to no address
            // and fall back to the title-extracted address instead (the title
            // always reveals the real street even when the card hides it).
            address:     (() => {
                const domAddr = getText('.item-address')
                             || getText('[class*="address"]')
                             || getText('.item-location')
                             || '';
                if (!domAddr || /posizione approssimativa/i.test(domAddr)) {
                    return addressFromTitle;
                }
                return domAddr;
            })(),
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
            // Multi-photo gallery: pull every <picture>/<img> in the card
            // multimedia container. Idealista's search-result cards usually
            // expose 1–5 photos via the carousel; older mid-page items may
            // expose only one. Cap at 8 to mirror the Immobiliare fetcher.
            photos: (() => {
                const out = [];
                const seen = new Set();
                const push = (u) => {
                    if (!u || typeof u !== 'string') return;
                    u = u.split(',')[0].split(' ')[0].trim();
                    if (!u.startsWith('http') || seen.has(u)) return;
                    seen.add(u);
                    out.push(u);
                };
                a.querySelectorAll('picture source').forEach(s =>
                    push(s.getAttribute('srcset') || s.srcset));
                a.querySelectorAll('.item-multimedia img, picture img, img[data-src], img[data-lazy], img[data-original]').forEach(i => {
                    push(i.getAttribute('data-src')
                      || i.getAttribute('data-lazy')
                      || i.getAttribute('data-original')
                      || i.src);
                });
                return out.slice(0, 8);
            })(),
            // Gallery size from the card's "1/5" counter (last span).
            photo_count: (() => {
                const spans = a.querySelectorAll('.item-multimedia-pictures__counter span');
                const n = spans.length ? parseInt((spans[spans.length - 1].textContent || '').replace(/\D/g, ''), 10) : NaN;
                return Number.isFinite(n) ? n : null;
            })(),
            latitude:    a.getAttribute('data-latitude')  || '',
            longitude:   a.getAttribute('data-longitude') || '',
            agency:      (() => {
                // Try a broad spread of selectors — Idealista's class names
                // shift over time and they vary between branded
                // (corporate) listings and individual ads.
                const SELECTORS = [
                    '.item-agency',
                    '[class*="item-agency"]',
                    '.advertiser-name',
                    '[class*="advertiser-name"]',
                    '.about-advertiser-name',
                    '.user-logged-data .name',
                    '[data-test-id="advertiser-name"]',
                    'picture[class*="logo"] img',
                    'img[class*="advertiser-logo"]',
                    'img[class*="logo-advertiser"]',
                    '[class*="logo"][alt]',
                ];
                for (const sel of SELECTORS) {
                    const el = a.querySelector(sel);
                    if (!el) continue;
                    // Prefer alt text on logo images (e.g. "Spacest.com")
                    const alt = (el.getAttribute && el.getAttribute('alt')) || '';
                    if (alt.trim()) return alt.trim();
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt) return txt;
                }
                // Last-ditch: scan all <img> alt attributes for likely
                // agency/branding strings.
                const imgs = a.querySelectorAll('img[alt]');
                for (const img of imgs) {
                    const alt = (img.getAttribute('alt') || '').trim();
                    // Heuristic: alt text that looks like an agency/brand name
                    // (not the listing title, not a generic photo description).
                    if (alt && alt.length < 80
                        && !/foto|image|imagen|trilocale|bilocale|monolocale|m²|mq/i.test(alt)
                        && /[A-Z]/.test(alt)) {
                        return alt;
                    }
                }
                return '';
            })(),
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
            count = await timed(tab.evaluate(
                "document.querySelectorAll('article.item').length"
            ), what="count listing cards")
            if count and int(count) > 0:
                return True
        except PageTimeout:
            raise
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
        json_str = await timed(tab.evaluate(_JSON_CHECK_JS), what="read embedded JSON")
        if json_str and isinstance(json_str, str) and json_str.strip() not in ("null", ""):
            json_data = json.loads(json_str)
            if json_data and isinstance(json_data, dict) and json_data.get("@type") == "ItemList":
                parsed = _parse_ld_json(json_data)
                if parsed:
                    print(" [json]", end="", flush=True)
                    return parsed
    except PageTimeout:
        raise
    except Exception:
        pass

    # Fall back to live DOM extraction (always returns JSON string now)
    try:
        result_str = await timed(tab.evaluate(_EXTRACT_JS), what="extract listing cards")
        if result_str and isinstance(result_str, str):
            result = json.loads(result_str)
            if isinstance(result, list):
                return result
        # Older nodriver might auto-parse the JSON — handle both cases
        elif isinstance(result_str, list):
            return result_str
    except PageTimeout:
        raise
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


def _parse_sale_price(text: str) -> Optional[int]:
    """Extract integer purchase price from strings like '€ 350.000' or '350000'."""
    digits = _re.sub(r"[^\d]", "", str(text))
    if not digits:
        return None
    v = int(digits)
    return v if 10_000 <= v <= 15_000_000 else None


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


# ── Auction / nuda proprietà detection ─────────────────────────────────────
#
# Idealista mixes judicial-auction sales and "nuda proprietà" (bare ownership)
# into the regular search results. They appear cheap-per-m² but are not
# comparable to a standard purchase. We flag them at parse time so the
# scorer + the dashboard can hide them by default.
#
# Detection works against title + description + tags. Word boundaries are
# important — naïve `'asta' in text` matches "Guastalla" and "ascensore".

_AUCTION_RX = _re.compile(
    r"\basta\b|\baste\b|"                        # standalone "asta" / "aste"
    r"all['’]?\s*asta|in\s+asta|"                # "all'asta" / "in asta"
    r"asta\s+giudiziar|vendita\s+giudiziar|"     # "asta giudiziaria" / "vendita giudiziaria"
    r"procedura\s+esecutiv|fallimentar|"
    r"concordato\s+preventiv|pignoramento",
    _re.IGNORECASE,
)
_NUDA_RX = _re.compile(
    r"nuda\s*propriet|usufrutto",
    _re.IGNORECASE,
)


def detect_auction_or_nuda(raw: dict) -> tuple[bool, bool]:
    """
    (is_auction, is_nuda_proprieta) from the raw scraped fields.
    `raw` is the dict produced by the JS extractor — has `title`, `description`,
    `tags`, etc.
    """
    text = " ".join(filter(None, [
        raw.get("title", ""),
        raw.get("description", ""),
        " ".join(raw.get("tags") or []),
    ]))
    return (
        bool(text and _AUCTION_RX.search(text)),
        bool(text and _NUDA_RX.search(text)),
    )


def parse_idealista_rooms(raw: dict) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Returns (total_rooms, bedrooms, bathrooms) from Idealista detail chips.
    Italian: locali (total habitable rooms incl. living), camere (bedrooms),
    bagni (bathrooms). Searches across rooms_text, floor_text, size_text,
    description preview, and tag list — anywhere Idealista exposes the data.
    """
    parts = [
        raw.get("rooms_text", ""),
        raw.get("floor_text", ""),
        raw.get("size_text",  ""),
        (raw.get("description", "") or "")[:300],
        " ".join(raw.get("tags") or []),
    ]
    text = " ".join(p for p in parts if p).lower()

    # Total rooms (locali) — try named monolocale/bilocale first, then "N locali"
    total_rooms = None
    for word, n in _ROOM_NAMES.items():
        if word in text:
            total_rooms = n
            break
    if total_rooms is None:
        m = _re.search(r"(\d+)\s*local", text)
        if m:
            total_rooms = int(m.group(1))

    # Bedrooms (camere) — "1 camera" / "2 camere"
    bedrooms = None
    m = _re.search(r"(\d+)\s*camer[ae]", text)
    if m:
        bedrooms = int(m.group(1))

    # Bathrooms (bagni) — "1 bagno" / "2 bagni"
    bathrooms = None
    m = _re.search(r"(\d+)\s*bagn[oi]", text)
    if m:
        bathrooms = int(m.group(1))

    # Sanity: bedrooms cannot exceed total rooms
    if bedrooms is not None and total_rooms is not None and bedrooms > total_rooms:
        bedrooms = None

    return total_rooms, bedrooms, bathrooms


# Italian ordinal words for floor levels — both `primo`/`prima` forms and
# the abbreviated `1°` / `1º` glyphs.
_FLOOR_WORDS = {
    "primo": 1, "prima": 1,
    "secondo": 2, "seconda": 2,
    "terzo": 3, "terza": 3,
    "quarto": 4, "quarta": 4,
    "quinto": 5, "quinta": 5,
    "sesto": 6, "sesta": 6,
    "settimo": 7, "settima": 7,
    "ottavo": 8, "ottava": 8,
    "nono": 9, "nona": 9,
    "decimo": 10, "decima": 10,
}


def _parse_floor_from_text(text: str) -> tuple[Optional[int], Optional[str]]:
    """
    Parse an Italian listing title/description for a floor reference.
    Returns (floor_n, floor_label) — both None when nothing recognised.
    Recognises:
      "piano terra" / "rialzato" / "interrato" / "seminterrato"
      "primo piano" / "secondo piano" ... up to "decimo piano"
      "al 3 piano" / "al 4° piano" / "3° piano" / "5º piano"
      "P.T." / "P.1" abbreviations
    """
    if not text or not isinstance(text, str):
        return None, None
    t = text.lower()

    # Below-ground / ground variants. NB Italian: "seminterrato" is one
    # word with a single 'i' between 'semi' and 'nterrato'.
    if _re.search(r"\bsemi[-\s]?nterrato\b", t):
        return -1, "Seminterrato"
    if _re.search(r"\binterrato\b", t):
        return -2, "Interrato"
    if _re.search(r"\bpiano\s+terra\b|\bp\.?\s*t\.?\b", t):
        return 0, "Piano terra"
    if _re.search(r"\b(?:piano\s+)?rialzato\b", t):
        return 0, "Piano rialzato"

    # Numeric floor: "3° piano", "al 4 piano", "5º piano", "P.7"
    m = _re.search(r"(?:al\s+)?(\d{1,2})\s*[°ºo]?\s*piano\b", t)
    if not m:
        m = _re.search(r"\bp\.\s*(\d{1,2})\b", t)
    if m:
        n = int(m.group(1))
        if 0 < n < 25:
            return n, f"{n}° piano"

    # Italian ordinal-word forms: "primo piano", "secondo piano"…
    for word, n in _FLOOR_WORDS.items():
        if _re.search(rf"\b{word}\s+piano\b", t):
            return n, f"{n}° piano"

    return None, None


def _extract_neighbourhood(address: str) -> str:
    """
    Extract neighbourhood from Idealista address strings.
    Typical format: "Via X N, Neighbourhood, Milano MI"
    Returns the last meaningful segment before city suffixes.
    City segments are detected by their first token being a known city name.
    """
    # Use the module-level CITY_TOKENS so the heuristic adapts to
    # whichever city we're scanning (Roma → {"roma","rm","rome"}).
    _CITY_TOKENS = CITY_TOKENS
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

def _int_or_none(val) -> Optional[int]:
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
    rooms, bedrooms, bathrooms = parse_idealista_rooms(raw)

    # Floor — Idealista floor strings look like "Piano rialzato con ascensore"
    # or "6º piano con ascensore". Strip the "con/senza ascensore" suffix before
    # calling parse_floor() so the floor token is recognised.
    floor_text_raw = (raw.get("floor_text") or "").strip()
    floor_for_parse = _re.sub(
        r'\s+(?:con|senza)\s+ascensore.*', '', floor_text_raw,
        flags=_re.IGNORECASE
    ).strip()
    floor_n, floor_label = parse_floor(floor_for_parse)

    # Fallback: if the chip didn't surface a floor, look in the title and
    # description for Italian phrasings like "primo piano", "al 3 piano",
    # "6° piano", "piano terra", "rialzato", "interrato".
    if floor_n is None and floor_label is None:
        title_desc = (raw.get("title") or "") + " " + (raw.get("description") or "")
        floor_n, floor_label = _parse_floor_from_text(title_desc)

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

    is_auction, is_nuda = detect_auction_or_nuda(raw)
    # Fake / foreign-property bait detection — shared helper from
    # fetch_listings so the two scrapers stay in lock-step. Scans
    # title + first 1000 chars of description.
    try:
        from fetch_listings import detect_fake_listing as _detect_fake
        from fetch_listings import detect_misrepresented_address as _detect_misrep
        from fetch_listings import is_outside_city_bbox as _outside_bbox
        _is_fake = _detect_fake(
            title       = raw.get("title", "") or "",
            description = raw.get("description", "") or "",
            city        = CITY_KEY,
        )
    except Exception:
        _is_fake = False
        _detect_misrep = lambda **_: (False, "")  # noqa: E731
        _outside_bbox  = lambda *a, **_: False    # noqa: E731
    if _is_fake:
        print(f"  [fake] id_{listing_id} ({CITY_KEY}) — "
              f"{(raw.get('title') or '')[:70]}",
              flush=True)

    # Misrepresented-address detection — same two layers as fetch_listings.
    _is_misrep   = False
    _outside     = False
    _misrep_why  = ""
    if not _is_fake:
        _is_misrep, _misrep_why = _detect_misrep(
            title       = raw.get("title", "") or "",
            description = raw.get("description", "") or "",
            city        = CITY_KEY,
        )
        if _outside_bbox(lat, lon, CITY_KEY):
            _outside = True
            if not _misrep_why:
                _misrep_why = f"Coordinates outside {CITY_KEY} comune"
        if _is_misrep or _outside:
            _is_fake = True
            print(f"  [misrep-address] id_{listing_id} ({CITY_KEY}) — {_misrep_why}",
                  flush=True)

    return {
        # ── Identity ──────────────────────────────────────────────────────────
        "id":                 f"id_{listing_id}",   # "id_" prefix prevents collisions
        "source":             SOURCE,               # "idealista"
        "city":               CITY_KEY,
        "city_label":         CITY_LABEL,
        "city_key":           CITY_KEY,
        "title":              raw.get("title", "").strip(),
        "neighbourhood":      neighbourhood,
        "address":            address,
        "latitude":           lat,
        "longitude":          lon,
        "url":                url,
        "thumbnail":          raw.get("img") or None,
        "photos":             list(raw.get("photos") or ([raw["img"]] if raw.get("img") else [])),
        # ── Listing-type flags (rentals — usually False, kept for symmetry) ──
        "is_auction":         is_auction,
        "is_nuda_proprieta":  is_nuda,
        "is_fake":            _is_fake,
        "is_misrepresented_address": _is_misrep,
        "is_outside_city":           _outside,
        "misrep_reason":             _misrep_why or None,
        # ── Price ─────────────────────────────────────────────────────────────
        "rent_mo":            price,        # monthly rent € — same field name as fetch_rentals
        "sqm":                sqm,
        "ask_psqm":           ask_psqm,    # €/m²/month
        # Not a structured field on search cards; the card's description
        # preview sometimes states it ("spese condominiali € 150").
        "spese_condominiali": _parse_condo_from_description(raw.get("description") or ""),
        # ── Physical ──────────────────────────────────────────────────────────
        "rooms":              rooms,
        "bedrooms":           bedrooms,        # camere da letto
        "floor":              raw.get("floor_text") or None,
        "floor_n":            floor_n,
        "floor_label":        floor_label,
        "is_below_ground":    is_below_ground,
        "is_ground_floor":    is_ground_floor,
        "elevator":           elevator,
        "is_external":        None,
        "energy_class":       None,
        "year_built":         None,
        "bathrooms":          bathrooms,
        "has_balcony":        has_balcony,
        "has_parking":        has_parking,
        "heating_type":       None,
        "furnished":          furnished,
        "photo_count":        _int_or_none(raw.get("photo_count")),
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


# ── Sale listing parser ───────────────────────────────────────────────────────

def parse_idealista_sale_listing(raw: dict) -> Optional[dict]:
    """
    Convert raw DOM-extracted fields into the standard sale listing dict.
    Field names match fetch_listings.parse_sale() so both sources merge
    transparently into sales_latest.json.
    Returns None when price or sqm cannot be reliably parsed.
    """
    listing_id = str(raw.get("id", "")).strip()
    if not listing_id:
        return None

    # Total purchase price €
    price = _parse_sale_price(raw.get("price_text", ""))
    if not price:
        return None

    # Surface m²
    sqm = _parse_sqm(raw.get("size_text", ""))
    if not sqm:
        return None

    ask_psqm = round(price / sqm)   # €/m²  (purchase, not monthly)

    # Rooms
    rooms, bedrooms, bathrooms = parse_idealista_rooms(raw)

    # Floor
    floor_text_raw = (raw.get("floor_text") or "").strip()
    floor_for_parse = _re.sub(
        r'\s+(?:con|senza)\s+ascensore.*', '', floor_text_raw,
        flags=_re.IGNORECASE
    ).strip()
    floor_n, floor_label = parse_floor(floor_for_parse)

    # Fallback: parse Italian floor phrasings from title + description when the
    # listing's chip didn't expose a floor (≈6 % of Idealista sales).
    if floor_n is None and floor_label is None:
        title_desc = (raw.get("title") or "") + " " + (raw.get("description") or "")
        floor_n, floor_label = _parse_floor_from_text(title_desc)

    is_below_ground = floor_n is not None and floor_n < 0
    is_ground_floor  = floor_n == 0

    # URL — ensure absolute
    url = raw.get("url", "")
    if url and not url.startswith("http"):
        url = "https://www.idealista.it" + url

    # Neighbourhood + address
    address       = raw.get("address", "").strip()
    neighbourhood = _extract_neighbourhood(address)
    neighbourhood = _NEIGHBOURHOOD_SYNONYMS.get(neighbourhood, neighbourhood)

    # Coordinates
    lat = _safe_float(raw.get("latitude"))
    lon = _safe_float(raw.get("longitude"))

    # Feature flags
    tags     = [t.lower() for t in (raw.get("tags") or [])]
    desc     = raw.get("description", "").lower()
    all_text = " ".join(tags) + " " + desc + " " + floor_text_raw.lower()

    has_balcony = True if any(k in all_text for k in ("balcon", "terrazza", "giardino")) else None
    has_parking = True if any(k in all_text for k in ("box", "garage", "parcheggio", "posto auto")) else None
    elev        = True if any(k in all_text for k in ("ascensor", "lift", "elevator")) else None
    furnished   = True if any(k in all_text for k in ("arredato", "arredat", "furnished")) else None

    omi = match_omi(neighbourhood)

    is_auction, is_nuda = detect_auction_or_nuda(raw)
    # Same fake-listing scan as the rentals path above (shared helper).
    try:
        from fetch_listings import detect_fake_listing as _detect_fake
        from fetch_listings import detect_misrepresented_address as _detect_misrep
        from fetch_listings import is_outside_city_bbox as _outside_bbox
        _is_fake = _detect_fake(
            title       = raw.get("title", "") or "",
            description = raw.get("description", "") or "",
            city        = CITY_KEY,
        )
    except Exception:
        _is_fake = False
        _detect_misrep = lambda **_: (False, "")  # noqa: E731
        _outside_bbox  = lambda *a, **_: False    # noqa: E731
    if _is_fake:
        print(f"  [fake-sale] id_{listing_id} ({CITY_KEY}) — "
              f"{(raw.get('title') or '')[:70]}",
              flush=True)

    # Misrepresented-address detection — same two layers, sales path.
    _is_misrep  = False
    _outside    = False
    _misrep_why = ""
    if not _is_fake:
        _is_misrep, _misrep_why = _detect_misrep(
            title       = raw.get("title", "") or "",
            description = raw.get("description", "") or "",
            city        = CITY_KEY,
        )
        if _outside_bbox(lat, lon, CITY_KEY):
            _outside = True
            if not _misrep_why:
                _misrep_why = f"Coordinates outside {CITY_KEY} comune"
        if _is_misrep or _outside:
            _is_fake = True
            print(f"  [misrep-sale] id_{listing_id} ({CITY_KEY}) — {_misrep_why}",
                  flush=True)

    return {
        "id":              f"id_{listing_id}",
        "source":          SOURCE_SALE,          # "idealista_sale"
        "city":            CITY_KEY,
        "city_label":      CITY_LABEL,
        "city_key":        CITY_KEY,
        "title":           raw.get("title", "").strip(),
        "neighbourhood":   neighbourhood,
        "address":         address,
        "latitude":        lat,
        "longitude":       lon,
        "url":             url,
        "thumbnail":       raw.get("img") or None,
        "photos":          list(raw.get("photos") or ([raw["img"]] if raw.get("img") else [])),
        # Listing-type flags — used by dashboard filter to hide auctions /
        # nuda proprietà by default (see applySaleFilters in index.html).
        "is_auction":      is_auction,
        "is_nuda_proprieta": is_nuda,
        "is_fake":         _is_fake,
        "is_misrepresented_address": _is_misrep,
        "is_outside_city":           _outside,
        "misrep_reason":             _misrep_why or None,
        "price":           price,                # total purchase price €
        "sqm":             sqm,
        "ask_psqm":        ask_psqm,             # €/m²  (purchase, not monthly)
        "rooms":           rooms,
        "bedrooms":        bedrooms,           # camere da letto
        "floor":           raw.get("floor_text") or None,
        "floor_n":         floor_n,
        "floor_label":     floor_label,
        "is_below_ground": is_below_ground,
        "is_ground_floor": is_ground_floor,
        "elevator":        elev,
        "is_external":     None,
        "energy_class":    None,
        "year_built":      None,
        "bathrooms":       bathrooms,
        "has_balcony":     has_balcony,
        "has_parking":     has_parking,
        "heating_type":    None,
        "furnished":       furnished,
        "photo_count":     None,
        "days_on_market":  None,
        "condition":       None,
        "description":     (raw.get("description") or "")[:400],
        # OMI — neighbourhood-based fields (polygon fields filled by geo enrichment)
        "omi_fascia":      omi.get("fascia"),
        "omi_zona":        None,
        "omi_rmin":        omi.get("rmin"),
        "omi_rmax":        omi.get("rmax"),
        "omi_compr_min":   None,
        "omi_compr_max":   None,
        "omi_compr_mid":   None,
        # Tracking
        "fetched_at":      datetime.now().isoformat(timespec="seconds"),
        "first_seen_date": None,
    }


# ── Sale fetch loop ───────────────────────────────────────────────────────────

async def _fetch_sale_async(pages: int, area_slugs: list, max_price: int,
                            min_sqm: int, min_rooms: int,
                            delay: float, browser,
                            complete: dict | None = None) -> tuple:
    """
    Navigate Idealista vendita-case pages and return (listings, skipped_areas).
    Mirrors _fetch_async() but uses sale URL bases and sale listing parser.
    Zones may be subzones ("porta-vittoria/porta-romana"). complete: gets
    {zone: zone} for every zone read to its end (no "next" link on a short
    last page), as in _fetch_async.
    """
    all_items: list  = []
    seen_ids:  set   = set()
    skipped_areas    = []

    targets = area_slugs if area_slugs else [None]

    first_url = build_idealista_url(1, targets[0], max_price, min_sqm, min_rooms, mode="sale")
    print(f"\n    [debug] GET {first_url} (sale initial + CAPTCHA check)", flush=True)
    tab = await browser.get(first_url)
    await asyncio.sleep(2.0)

    if not await _check_and_handle_captcha(tab):
        return [], [{"name": "all", "reason": "CAPTCHA not solved in time"}]

    for area_slug in targets:
        label = area_slug or "all Milano (sale)"
        print(f"\n    area: {label}", end="", flush=True)

        try:
            area_ids_seen: set = set()
            canonical_slug     = area_slug
            area_got_results   = False
            finished           = False

            for page in range(1, pages + 1):
                url = build_idealista_url(page, canonical_slug, max_price, min_sqm, min_rooms,
                                          mode="sale")
                print(f"\n    [debug] GET {url}", flush=True)
                tab = await browser.get(url)
                await asyncio.sleep(2.0)

                if not await _check_and_handle_captcha(tab, timeout=60):
                    skipped_areas.append({"name": label,
                                          "reason": "CAPTCHA timeout mid-scan"})
                    print(f" [captcha-timeout — skipping area]", end="", flush=True)
                    break

                if page == 1 and area_slug:
                    try:
                        actual_href = await timed(tab.evaluate("window.location.href"),
                                                  what="read location")
                        if actual_href:
                            m = (_re.search(r"/milano(?:-milano)?/([^/?#]+)/con-", actual_href)
                                 or _re.search(r"/milano(?:-milano)?/([^/?#]+?)/?$", actual_href))
                            if m:
                                resolved = m.group(1)
                                if resolved != canonical_slug:
                                    print(f" [→{resolved}]", end="", flush=True)
                                    canonical_slug = resolved
                    except PageTimeout:
                        raise
                    except Exception:
                        pass

                raw_listings = await _extract_listings_from_page(tab)
                if not raw_listings:
                    print(f" (empty, done)", end="", flush=True)
                    break

                area_got_results = True

                page_ids = {r.get("id", "") for r in raw_listings if r.get("id")}
                if page > 1 and page_ids and page_ids.issubset(area_ids_seen):
                    print(f" (repeat page, done)", end="", flush=True)
                    break
                area_ids_seen.update(page_ids)

                new_this_page = 0
                for raw in raw_listings:
                    parsed = parse_idealista_sale_listing(raw)
                    if not parsed:
                        continue
                    lid = parsed["id"]
                    if lid in seen_ids:
                        continue
                    parsed["_fetched_area"] = canonical_slug or "all"
                    if area_slug:
                        parsed["scan_area"] = area_slug   # de-listing (sales_scan)
                    seen_ids.add(lid)
                    all_items.append(parsed)
                    new_this_page += 1

                try:
                    has_next = await timed(tab.evaluate(
                        "!!(document.querySelector('a.icon-arrow-right-after')"
                        "|| document.querySelector('li.next a')"
                        "|| document.querySelector('a[rel=\"next\"]')"
                        "|| document.querySelector('.pagination-next a'))"
                    ), what="find next link")
                except PageTimeout:
                    raise
                except Exception:
                    has_next = False

                print(f" p{page}(+{new_this_page})", end="", flush=True)
                if not has_next:
                    finished = len(raw_listings) < 30
                    break

            if finished and area_slug and complete is not None:
                complete[area_slug] = area_slug
            if not area_got_results and area_slug:
                reason = "no results — slug may be invalid or not found on Idealista.it"
                skipped_areas.append({"name": area_slug, "reason": reason})
                print(f"\n    [warn] No sale results for area '{label}'", flush=True)

        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            skipped_areas.append({"name": label, "reason": reason})
            print(f"\n    [error] Sale area '{label}' failed: {reason} — skipping", flush=True)
            # A hung or dead browser would fail every remaining area too.
            if isinstance(exc, PageTimeout) or not await browser.alive():
                await browser.restart()
                continue
            try:
                tab = await browser.get("about:blank")
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

    return all_items, skipped_areas


def fetch_idealista_sales(pages: int = 3, area_names: list = None, max_price: int = 0,
                          min_sqm: int = 0, min_rooms: int = 0,
                          delay: float = 3.0) -> tuple:
    """
    One-shot fetch of Milano for-sale listings from Idealista.it via nodriver.
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
        desc = ", ".join(valid_slugs)
        print(f"  [scan] {len(area_names)} area(s) → {len(valid_slugs)} valid for sale scan")
    else:
        desc = "all Milano"

    area_slugs = valid_slugs
    print(f"  Fetching Idealista sale listings ({desc})…", end="", flush=True)

    async def _run():
        browser = BrowserSession("idealista-sales")
        await browser.start()
        try:
            items, skipped = await _fetch_sale_async(
                pages, area_slugs, max_price, min_sqm, min_rooms, delay, browser
            )
        finally:
            browser.stop()
        return items, skipped

    t0 = time.time()
    items, fetch_skipped = asyncio.run(_run())
    skipped_areas = pre_skipped + fetch_skipped
    n_fetched = len(items)
    print(f"\n  [fetch]  Idealista sale: {n_fetched} listings fetched ({pages} pages)")

    if not items:
        print("  [warn]   No Idealista sale listings parsed — check selectors or CAPTCHA")
        return items, skipped_areas

    # Cache-aware geo enrichment (same pattern as rental fetch)
    INLINE_ENRICH_CAP = 200
    try:
        import enrichment_cache as _ecache
        from enrich_geo import enrich_batch as _enrich_batch

        _ecache.load()

        new_items = [l for l in items if _ecache.get(SOURCE_SALE, l["id"]) is None]
        n_cached  = n_fetched - len(new_items)
        print(f"  [cache]  {n_cached} already enriched, {len(new_items)} new")

        if new_items:
            if len(new_items) > INLINE_ENRICH_CAP:
                print(f"  [enrich] {len(new_items)} new listings — skipping inline enrichment "
                      f"(>{INLINE_ENRICH_CAP} cap). Use 'Enrich geo' in the dashboard.",
                      flush=True)
            else:
                print(f"  [enrich] Enriching {len(new_items)} new sale listings…", flush=True)
                t_enrich = time.time()
                try:
                    geo_results = _enrich_batch(new_items)
                    print(f"  [enrich] Done in {time.time() - t_enrich:.1f}s")
                    _ecache.bulk_save(
                        [(SOURCE_SALE, l["id"], g) for l, g in zip(new_items, geo_results)]
                    )
                except Exception as _geo_exc:
                    print(f"  [enrich] Geo enrichment failed "
                          f"({type(_geo_exc).__name__}): {_geo_exc}"
                          f" — saving without geo data", flush=True)

        for listing in items:
            cached = _ecache.get(SOURCE_SALE, listing["id"])
            if cached:
                listing.update({k: v for k, v in cached.items() if k not in _ecache.META_KEYS})

        try:
            import omi_lookup as _omi_lookup
            _need_omi = [
                l for l in items
                if l.get("omi_loc_mid") is None
                and l.get("latitude") and l.get("longitude")
            ]
            _omi_updates = []
            for _l in _need_omi:
                _zone, _src = _omi_lookup.lookup_for_city(float(_l["latitude"]),
                                                           float(_l["longitude"]),
                                                           city=CITY_KEY)
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
                        (SOURCE_SALE, _l["id"],
                         {**(_ecache.get(SOURCE_SALE, _l["id"]) or {}), **_omi_f})
                    )
            if _omi_updates:
                _ecache.bulk_save(_omi_updates)
                print(f"  [omi]    polygon fields applied to "
                      f"{len(_omi_updates)} sale listings")
        except Exception as _omi_exc:
            print(f"  [omi]    polygon step skipped: {_omi_exc}", file=sys.stderr)

    except ImportError:
        pass

    if skipped_areas:
        print(f"  [scan]   {len(skipped_areas)} area(s) skipped: "
              + ", ".join(s["name"] for s in skipped_areas))

    print(f"  [done]   Sale run complete in {time.time() - t0:.1f}s total")
    return items, skipped_areas


# ── Sale run cycle ────────────────────────────────────────────────────────────

def run_sale_once(args) -> list:
    """Fetch-score-write cycle for Idealista sale listings."""
    area_names = _load_idealista_areas()
    if not area_names:
        print("  [scan] No Idealista zones matched — scanning all Milano (sale)", flush=True)

    raw, skipped_areas = fetch_idealista_sales(
        pages=args.pages,
        area_names=area_names,
        max_price=args.max_rent or 0,
        min_sqm=args.min_sqm    or 0,
        min_rooms=args.min_rooms or 0,
        delay=args.delay,
    )

    if not raw:
        print("  ✗ No Idealista sale listings fetched.")
        return []

    # Load existing Immobiliare sale listings as comps pool
    existing_sales = []
    if SALE_OUTPUT_PATH.exists():
        try:
            all_sales = json.loads(SALE_OUTPUT_PATH.read_text())
            existing_sales = [l for l in all_sales if l.get("source") != SOURCE_SALE]
        except Exception:
            pass

    # Score using the full merged list as the comps pool
    comps_pool = raw + existing_sales
    if _score_all_sales:
        # score_all_sales uses all listings passed as both the listings and pool
        # We pass the full comps_pool so Idealista listings can compare against
        # Immobiliare listings as neighbours
        scored = _score_all_sales(comps_pool)
        # Keep only the Idealista sale entries as "scored" (the merged comps_pool
        # scoring is done in-place — existing_sales are already scored in the file)
        ideal_ids = {l["id"] for l in raw}
        scored = [l for l in scored if l["id"] in ideal_ids]
        if _explain_all_sales:
            _explain_all_sales(scored)
    else:
        scored = raw

    # Stamp first_seen_date + last_seen_date on every Idealista sale we just
    # scored, using the shared seen_ids_sales.json so staleness state survives
    # across runs (and across the two sale fetchers — Immobiliare + Idealista).
    from fetch_listings import (
        _load_seen_sales, _save_seen_sales,
        STALE_DAYS_THRESHOLD, REMOVE_AFTER_DAYS,
    )
    from datetime import date as _date, datetime as _dt
    today        = _date.today()
    today_str    = str(today)
    seen_sales   = _load_seen_sales()
    for l in scored:
        lid = str(l["id"])
        l["first_seen_date"] = seen_sales[lid]["first_seen_date"] if lid in seen_sales else today_str
        l["last_seen_date"]  = today_str
        seen_sales[lid] = {
            "first_seen_date": l["first_seen_date"],
            "last_seen_date":  today_str,
        }

    # Merge: keep existing non-Idealista-sale entries, replace/add Idealista-sale entries.
    # Existing Idealista entries that weren't re-seen this run survive with
    # their old last_seen_date — the staleness sweep below flags them.
    id_map = {l["id"]: l for l in existing_sales}   # non-Idealista (e.g. Immobiliare)
    # Preserve any previously-stored Idealista entries that weren't re-seen:
    if SALE_OUTPUT_PATH.exists():
        try:
            for l in json.loads(SALE_OUTPUT_PATH.read_text()):
                if l.get("source") == SOURCE_SALE and l["id"] not in {x["id"] for x in scored}:
                    id_map[l["id"]] = l
        except Exception:
            pass
    for l in scored:
        id_map[l["id"]] = l
    merged = list(id_map.values())

    # Staleness sweep across the merged list (covers BOTH Idealista entries we
    # didn't re-see AND Immobiliare entries we never touch from this side).
    stale_count = 0
    keep: list = []
    for l in merged:
        last_seen = l.get("last_seen_date")
        if not last_seen:
            l["days_since_seen"] = None
            l["is_stale"]        = False
            keep.append(l)
            continue
        try:
            ds = (today - _dt.fromisoformat(last_seen).date()).days
        except ValueError:
            l["days_since_seen"] = None
            l["is_stale"]        = False
            keep.append(l)
            continue
        l["days_since_seen"] = ds
        if ds >= REMOVE_AFTER_DAYS:
            seen_sales.pop(str(l.get("id", "")), None)
            continue
        l["is_stale"] = ds >= STALE_DAYS_THRESHOLD
        if l["is_stale"]:
            stale_count += 1
        keep.append(l)

    _save_seen_sales(seen_sales)
    dropped = len(merged) - len(keep)
    if dropped:
        print(f"  [trim]  removed {dropped} sales absent for {REMOVE_AFTER_DAYS}+ days")
    print(f"  [stale] {stale_count}/{len(keep)} sales flagged stale "
          f"(absent {STALE_DAYS_THRESHOLD}+ days)")

    merged = sorted(keep, key=lambda x: x.get("score_total", 0) or 0, reverse=True)

    # Write atomically — compact + null-strip to stay under the 25 MiB Cloudflare cap
    from dashboard_io import write_snapshot
    tmp = SALE_OUTPUT_PATH.with_suffix(".tmp")
    write_snapshot(tmp, merged)
    tmp.replace(SALE_OUTPUT_PATH)

    new_listings = [l for l in scored if l["id"] not in {e["id"] for e in existing_sales}]
    print(f"  ✓ {len(scored)} Idealista sale listings written · {len(new_listings)} new")
    return new_listings


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
                       delay: float, browser,
                       complete: dict | None = None,
                       stats: dict | None = None) -> tuple:
    """
    Navigate Idealista rental pages and return (listings, skipped_areas).
    Mirrors fetch_rentals._fetch_async() exactly, replacing __NEXT_DATA__
    extraction with live HTML DOM + optional embedded JSON parsing.

    complete: when given, gets {zone_slug: canonical_slug} for every zone
    read to its end — the last page has no "next" link AND is short (< a
    full page of 30). A full last page can be the site's pagination cap,
    an empty page a render that timed out: neither proves the end.
    stats: when given, counts page loads ("pages").
    """
    stats = {} if stats is None else stats
    stats.setdefault("pages", 0)
    stats.setdefault("parse_failed", 0)
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
    stats["pages"] += 1
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
            finished           = False   # read to the zone's end

            for page in range(1, pages + 1):
                url = build_idealista_url(page, canonical_slug, max_rent, min_sqm, min_rooms)
                print(f"\n    [debug] GET {url}", flush=True)
                tab = await browser.get(url)
                stats["pages"] += 1
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
                        actual_href = await timed(tab.evaluate("window.location.href"),
                                                  what="read location")
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
                    except PageTimeout:
                        raise
                    except Exception:
                        pass

                raw_listings = await _extract_listings_from_page(tab)
                if not raw_listings:
                    print(f" (empty, done)", end="", flush=True)
                    stats["parse_failed"] += page > 1
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
                    parsed["_via"] = "sweep"
                    if canonical_slug:
                        parsed["scan_area"] = canonical_slug   # kept on disk (de-listing)
                    seen_ids.add(lid)
                    all_items.append(parsed)
                    new_this_page += 1

                # Detect "next page" link — Idealista uses several different selectors
                try:
                    has_next = await timed(tab.evaluate(
                        "!!(document.querySelector('a.icon-arrow-right-after')"
                        "|| document.querySelector('li.next a')"
                        "|| document.querySelector('a[rel=\"next\"]')"
                        "|| document.querySelector('.pagination-next a'))"
                    ), what="find next link")
                except PageTimeout:
                    raise
                except Exception:
                    has_next = False

                print(f" p{page}(+{new_this_page})", end="", flush=True)
                if not has_next:
                    finished = len(raw_listings) < 30
                    break

            if finished and area_slug and complete is not None:
                complete[area_slug] = canonical_slug
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
            # A hung or dead browser would fail every remaining area too
            # (2026-08-24: Edge died and the scan kept looping on it).
            if isinstance(exc, PageTimeout) or not await browser.alive():
                await browser.restart()
                continue
            # Otherwise navigate to a blank page to reset CDP state
            try:
                tab = await browser.get("about:blank")
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

    return all_items, skipped_areas


# Daily scan (see fetch_rentals DAILY_*): per zone, newest first until a page
# brings nothing new, plus the rotating full sweep. No detail pages: they
# don't expose coordinates either (latitude: '' on every sample, 2026-10-14).
IDEALISTA_NEWEST     = "ordine=pubblicazione-desc"   # the site's own "Recenti" link
DAILY_ZONE_MAX_PAGES = 5


async def _daily_async(pages: int, area_slugs: list, max_rent: int, min_sqm: int,
                       min_rooms: int, delay: float, browser, known_ids: set) -> tuple:
    found: dict = {}
    print("\n    discover: newest first per zone", end="", flush=True)
    for i, slug in enumerate(area_slugs):
        for page in range(1, DAILY_ZONE_MAX_PAGES + 1):
            url = build_idealista_url(page, slug, max_rent, min_sqm, min_rooms)
            url += ("&" if "?" in url else "?") + IDEALISTA_NEWEST
            try:
                tab = await browser.get(url)
                await asyncio.sleep(2.0)
                if not await _check_and_handle_captcha(tab, timeout=60):
                    break
                raws = await _extract_listings_from_page(tab)
            except PageTimeout:
                await browser.restart()
                break
            except Exception as exc:
                print(f"\n    [warn] {slug} p{page} unreadable ({type(exc).__name__})", flush=True)
                break
            new_here = 0
            for raw in raws:
                parsed = parse_idealista_listing(raw)
                if not parsed:
                    continue
                parsed["_fetched_area"] = slug
                if parsed["id"] not in known_ids and parsed["id"] not in found:
                    new_here += 1
                found[parsed["id"]] = parsed
            print(f" {slug}:p{page}(+{new_here})", end="", flush=True)
            if not raws or not new_here:
                break
        if i < len(area_slugs) - 1:
            await asyncio.sleep(random.uniform(3.0, 6.0))

    sweep = sweep_slugs_for_today(area_slugs)
    print(f"\n    sweep: {', '.join(sweep)}", end="", flush=True)
    swept, skipped = await _fetch_async(pages, sweep, max_rent, min_sqm, min_rooms, delay, browser)
    for l in swept:
        found.setdefault(l["id"], l)
    return list(found.values()), skipped


# ── Spread scan (see fetch_rentals SPREAD_SCAN) ──────────────────────────────
# One zone per slot, the one swept longest ago, every 24 h / (zones + 1)
# (~2 h 10 min for 10 zones), read to its end in a minimized session, plus a
# city-wide newest-first discover every DISCOVER_EVERY_SEC. The daily scan
# swept 2–3 zones a night; Garibaldi–Porta Venezia still had a full 50th
# page on 2026-10-17, so a slot may read up to IDEALISTA_SLOT_MAX_PAGES.
# After each slot top listings get their photos read and the digest goes
# out, both shared with Immobiliare's daemon (fetch_rentals
# read_photos_after_slot / send_pending_digest).
SPREAD_SCAN                  = True
IDEALISTA_SLOT_MAX_PAGES     = 100
IDEALISTA_DISCOVER_MAX_PAGES = 10

# Filled by fetch_idealista() for the caller: complete sweeps, page loads,
# discover stats.
LAST_FETCH: dict = {}


def _discover_state_path() -> Path:
    return BASE_DIR / f".discover_state_idealista_{CITY_KEY}.json"


def load_discover_state() -> dict:
    """{"at": epoch of the last discover run, "ids": ids it read}."""
    try:
        return json.loads(_discover_state_path().read_text())
    except (OSError, ValueError):
        return {}


async def _discover_city_async(browser, known_ids: set, prev_ids: set, max_rent: int,
                               min_sqm: int, min_rooms: int) -> tuple:
    """City-wide search, newest published first. Stops on a page with
    nothing new or holding DISCOVER_OVERLAP_STOP of the previous run's ids.
    Returns ({id: listing}, stats)."""
    found: dict = {}
    stats = {"pages": 0, "new": 0, "per_page": [], "caught_up": False, "ids": []}
    print("\n    discover: city-wide, newest first", end="", flush=True)
    for page in range(1, IDEALISTA_DISCOVER_MAX_PAGES + 1):
        url = build_idealista_url(page, None, max_rent, min_sqm, min_rooms)
        url += ("&" if "?" in url else "?") + IDEALISTA_NEWEST
        try:
            tab = await browser.get(url)
            stats["pages"] += 1
            await asyncio.sleep(2.0)
            if not await _check_and_handle_captcha(tab, timeout=60):
                break
            raws = await _extract_listings_from_page(tab)
        except PageTimeout:
            await browser.restart()
            break
        except Exception as exc:
            print(f"\n    [warn] discover p{page} unreadable ({type(exc).__name__})", flush=True)
            break
        new_here = overlap = 0
        for raw in raws:
            parsed = parse_idealista_listing(raw)
            if not parsed:
                continue
            stats["ids"].append(parsed["id"])
            overlap += parsed["id"] in prev_ids
            parsed["_fetched_area"] = "all"
            parsed["_via"] = "discover"
            if parsed["id"] not in known_ids and parsed["id"] not in found:
                new_here += 1
            found[parsed["id"]] = parsed
        stats["new"] += new_here
        stats["per_page"].append(new_here)
        print(f" p{page}(+{new_here} new, {overlap} seen last run)", end="", flush=True)
        if not raws:
            break
        if not new_here or overlap >= DISCOVER_OVERLAP_STOP:
            stats["caught_up"] = True
            break
    return found, stats


async def _slot_async(pages: int, zone: str, discover: bool, max_rent: int,
                      min_sqm: int, min_rooms: int, delay: float, browser) -> tuple:
    """One spread-scan slot: discover (when due), then sweep `zone` to its
    end. Returns (listings, skipped)."""
    found: dict = {}
    if discover:
        prev = load_discover_state()
        found, dstats = await _discover_city_async(
            browser, set(load_seen_ids()), set(prev.get("ids") or []),
            max_rent, min_sqm, min_rooms)
        ids = dstats.pop("ids")
        if dstats["pages"]:
            _discover_state_path().write_text(json.dumps({"at": time.time(), "ids": ids}))
        LAST_FETCH["discover"] = dstats
    if not zone:                           # discover between slots
        return list(found.values()), []
    print(f"\n    sweep: {zone}", end="", flush=True)
    swept, skipped = await _fetch_async(pages, [zone], max_rent, min_sqm, min_rooms,
                                        delay, browser, complete=LAST_FETCH["complete"],
                                        stats=LAST_FETCH["sweep"])
    for l in swept:
        found[l["id"]] = l
    return list(found.values()), skipped


def fetch_idealista(pages: int = 3, area_names: list = None, max_rent: int = 0,
                    min_sqm: int = 0, min_rooms: int = 0,
                    delay: float = 3.0, mode: str = "full",
                    slot_area: str = None, discover: bool = False) -> tuple:
    """
    One-shot fetch of Milano rental listings from Idealista.it via nodriver.
    delay defaults to 3.0s — Idealista applies stricter bot detection.
    Returns (listings, skipped_areas).
    mode "slot" (spread scan): discover when `discover`, then sweep zone
    `slot_area` only, in a minimized window; see LAST_FETCH.
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

    LAST_FETCH.clear()
    LAST_FETCH.update({"complete": {}, "sweep": {}, "discover": None})

    async def _run():
        # Slots run all day: keep their window out of sight, like the drip.
        browser = BrowserSession("idealista", background=(mode == "slot"))
        await browser.start()
        try:
            if mode == "slot":
                return await _slot_async(pages, slot_area, discover, max_rent,
                                         min_sqm, min_rooms, delay, browser)
            if mode == "daily" and area_slugs:
                items, skipped = await _daily_async(
                    pages, area_slugs, max_rent, min_sqm, min_rooms, delay, browser,
                    known_ids=set(load_seen_ids()))
                return items, skipped
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
                listing.update({k: v for k, v in cached.items() if k not in _ecache.META_KEYS})

        # OMI polygon fields for listings still missing them (fast, in-memory)
        try:
            import omi_lookup as _omi_lookup
            _need_omi = [
                l for l in items
                if l.get("omi_loc_mid") is None
                and l.get("latitude") and l.get("longitude")
            ]
            _omi_updates = []
            for _l in _need_omi:
                _zone, _src = _omi_lookup.lookup_for_city(float(_l["latitude"]),
                                                           float(_l["longitude"]),
                                                           city=CITY_KEY)
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

def _idealista_area_names(args) -> list:
    if args.areas:
        return [a.strip() for a in args.areas.split(",") if a.strip()]
    # Default: derive Idealista zones from the active Immobiliare areas
    # (area_settings.json — single source of truth, no separate Idealista picker).
    # _load_idealista_areas() maps each active Immobiliare neighbourhood to the
    # Idealista macro-zone that covers it, then returns the zone names to scan.
    area_names = _load_idealista_areas()
    if not area_names:
        print("  [scan] No Idealista zones matched — scanning all Milano", flush=True)
    return area_names


def _zone_slugs(area_names: list) -> list:
    return [s for s in (to_url_slug(n) for n in area_names) if s]


def _other_source_comps() -> list:
    """Immobiliare rows as comps for an Idealista batch (most Idealista rows
    have no coordinates yet when scored)."""
    try:
        existing_all = json.loads(OUTPUT_PATH.read_text()) if OUTPUT_PATH.exists() else []
    except Exception:
        return []
    immo_comps = [l for l in existing_all if l.get("source") != SOURCE]
    if immo_comps:
        print(f"  [comps]  {len(immo_comps)} Immobiliare listing(s) loaded as comps pool",
              flush=True)
    return immo_comps


def run_once(args) -> list:
    """Execute one fetch-score-write cycle. Returns newly seen listings."""
    area_names = _idealista_area_names(args)

    raw, skipped_areas = fetch_idealista(
        pages=args.pages,
        area_names=area_names,
        max_rent=args.max_rent   or 0,
        min_sqm=args.min_sqm    or 0,
        min_rooms=args.min_rooms or 0,
        delay=args.delay,
        mode=args.scan,
    )

    if not raw:
        print("  ✗ No Idealista listings fetched.")
        write_idealista_status(new_count=0, skipped_areas=skipped_areas)
        return []

    # Load existing Immobiliare listings so they can serve as comps for the
    # newly-fetched Idealista batch.  Without this, score_all() would only see
    # the Idealista batch as its comps pool — most of those lack coordinates at
    # this point, so the comps benchmark falls back to "No local comps".
    immo_comps = _other_source_comps()

    # Score through the same pipeline as Immobiliare listings.
    # comps_pool = newly-fetched Idealista listings + existing Immobiliare data
    # so radius-based comps finds neighbours even when Idealista coords are still
    # pending geo enrichment (Immobiliare listings already have coordinates).
    comps_pool = raw + immo_comps
    scored = score_all(raw, comps_pool=comps_pool)

    # Stamp first_seen_date — shared seen_ids.json with fetch_rentals;
    # "id_" prefix ensures no collision with Immobiliare numeric IDs.
    seen      = load_seen_ids()
    today_str = str(date.today())
    for l in scored:
        lid = l["id"]
        l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str
        # Seen in this scan. Staleness marking and archive_old_listings key
        # off last_seen_date; Idealista rentals never carried it, so all
        # 13k looked "never seen" (Immobiliare + Idealista sales stamp it).
        l["last_seen_date"]  = today_str
        l["is_stale"]        = False

    # Merge into rentals_latest.json — Immobiliare entries are untouched.
    # No de-listing from here yet: write_output de-lists only from areas
    # read in full, which this scan doesn't report; Idealista moves to that
    # after the Immobiliare spread-scan pilot.
    write_output(scored, source=SOURCE)

    # Update shared seen_ids.json
    new_listings = [l for l in scored if l["id"] not in seen]
    for l in scored:
        seen[l["id"]] = {"first_seen_date": l["first_seen_date"],
                         "last_seen_date":  today_str}
    save_seen_ids(seen)

    # Coverage for fetch_rentals' stale/trim pass (it judges Idealista rows too).
    zone_slugs = _zone_slugs(area_names)
    attempted  = sweep_slugs_for_today(zone_slugs) if args.scan == "daily" else zone_slugs
    failed     = {s.get("name") for s in skipped_areas}
    record_sweeps("idealista", zone_slugs, [z for z in attempted if z not in failed])

    write_idealista_status(new_count=len(new_listings), skipped_areas=skipped_areas)
    print(f"  ✓ {len(scored)} Idealista listings written · {len(new_listings)} new")
    rescore_output()

    # Last scan of the night: read the photos of new top listings (score
    # ≥ 90 + Hidden Gems; photo_eval.py) and rescore so a contradicting
    # verdict caps its score before the morning.
    try:
        from photo_eval import nightly as _read_photos
        if _read_photos(log=lambda m: print("  " + m, flush=True)):
            rescore_output()
    except Exception as exc:
        print(f"  [photos] skipped: {exc}", flush=True)

    # (The Netlify deploy that used to follow is gone: the dashboard moved to
    # Cloudflare, every deploy failed with 401/403, and the online data comes
    # from Supabase anyway.)

    return new_listings


def run_slot(args, zone: str | None, do_discover: bool) -> dict:
    """One spread-scan slot: discover when due, sweep `zone`, merge,
    rescore. zone None: discover only (between slots), written only when it
    found new listings. Appends its numbers to the slot log and returns them."""
    t0 = time.time()
    area_names = _idealista_area_names(args)
    raw, skipped = fetch_idealista(
        pages=max(args.pages or 0, IDEALISTA_SLOT_MAX_PAGES), area_names=area_names,
        max_rent=args.max_rent or 0, min_sqm=args.min_sqm or 0,
        min_rooms=args.min_rooms or 0, delay=args.delay,
        mode="slot", slot_area=zone, discover=do_discover)
    complete = dict(LAST_FETCH.get("complete") or {})
    sweep = LAST_FETCH.get("sweep") or {}
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "source": SOURCE,
           "kind": "slot" if zone else "discover", "area": zone, "complete": zone in complete, "pages": sweep.get("pages", 0),
           "parse_failed": sweep.get("parse_failed", 0),
           "skipped": [s.get("reason") for s in skipped],
           "discover": LAST_FETCH.get("discover"),
           "found": len(raw), "found_sweep": sum(l.get("_via") == "sweep" for l in raw),
           "new": 0}
    seen = load_seen_ids()
    if not zone and not any(l["id"] not in seen for l in raw):
        raw = []                           # discover found nothing new: nothing to write
    if raw:
        scored = score_all(raw, comps_pool=raw + _other_source_comps())
        today_str = str(date.today())
        for l in scored:
            lid = l["id"]
            l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str
            l["last_seen_date"] = today_str
            l["is_stale"] = False
        rec.update(write_output(scored, source=SOURCE,
                                swept_areas=set(complete.values()) or None))
        new = [l for l in scored if l["id"] not in seen]
        for l in scored:
            seen[l["id"]] = {"first_seen_date": l["first_seen_date"],
                             "last_seen_date": today_str}
        save_seen_ids(seen)
        rec.update(new=len(new),
                   new_via_discover=sum(l.get("_via") == "discover" for l in new),
                   new_via_sweep=sum(l.get("_via") == "sweep" for l in new),
                   new_ids=[l["id"] for l in new][:200])
        _queue_digest([l["id"] for l in new])
    if zone:
        record_sweeps("idealista", _zone_slugs(area_names), list(complete), tried_slugs=[zone])
    if zone or raw:      # a discover with nothing new leaves the dashboard alone
        write_idealista_status(new_count=rec["new"], skipped_areas=skipped)
    if raw:
        rescore_output()
    rec["seconds"] = round(time.time() - t0)
    with open(_slot_log_path(), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _spread_loop(args, log) -> None:
    """Spread scan: a slot every gap, the detail drip in between."""
    import scan_window
    from fetch_rentals import _sleep_outside_window

    def _gap() -> int:
        # Every zone once a night: the window's hours over the zones.
        n = len(_zone_slugs(_idealista_area_names(args))) or 1
        return SLOT_GAP_SEC or int(scan_window.hours() * 3600 / (n + 1))

    log(f"Idealista daemon started — spread scan at night ({scan_window.describe()}): one zone every "
        f"{_gap() // 60} min, discover every {DISCOVER_EVERY_SEC / 60:g} min, output {OUTPUT_PATH}")
    next_slot = last_scan_at("idealista") + _gap()
    while True:
        until = scan_window.paused_until("idealista")
        if until > time.time():
            log(f"Idealista paused until {datetime.fromtimestamp(until):%d %b %H:%M} — no pages")
            time.sleep(min(until - time.time(), 3600) + 5)
            next_slot = max(next_slot, time.time() + 120)
            continue
        if not scan_window.in_window():
            _sleep_outside_window(args, "idealista", log)
            next_slot = time.time() + 120     # the Immobiliare daemon starts first
        if time.time() < next_slot:
            log(f"Next slot at {datetime.fromtimestamp(next_slot):%Y-%m-%d %H:%M}")
            try:
                _between_slots(args, "idealista", next_slot, log, run_slot, load_discover_state)
            except KeyboardInterrupt:
                log("Interrupted during sleep — stopping Idealista daemon.")
                raise
            if not scan_window.in_window():
                continue
        zone = next_slot_area("idealista", _zone_slugs(_idealista_area_names(args)))
        do_discover = time.time() - (load_discover_state().get("at") or 0) >= DISCOVER_EVERY_SEC
        log(f"Slot: {zone}{' + discover' if do_discover else ''}")
        ok, blocked = False, True
        try:
            rec = run_marked("idealista", run_slot, args, zone, do_discover)
            ok = rec["complete"]
            # Back off only when the site pushed back — skipped / unreadable
            # pages or nothing at all — not for a sweep that merely can't
            # prove it reached the end (e.g. Idealista's full last page).
            blocked = bool(rec["skipped"] or rec["parse_failed"] or not rec["found"])
            d = rec.get("discover")
            log(f"Slot done — {zone}: {rec['found']} listing(s), {rec['new']} new"
                + (f" (discover: {d['new']} new in {d['pages']} page(s))" if d else "")
                + f", {rec.get('hidden', 0)} hidden, {rec.get('missed', 0)} missed once"
                + ("" if ok else " · zone NOT read to its end — retried later"))
            mark_scan_done("idealista")
            read_photos_after_slot(log)
            send_pending_digest(args, log)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                log("Interrupted — stopping Idealista daemon.")
                raise
            log(f"✗ Slot error: {e}\n{traceback.format_exc()}")
        next_slot = time.time() + (2 * _gap() if blocked else _gap())


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

    if SPREAD_SCAN and args.scan == "daily" and _zone_slugs(_idealista_area_names(args)):
        try:
            _spread_loop(args, log)
        finally:
            log_fh.close()
        return

    log(f"Idealista daemon started — one scan every {DAEMON_INTERVAL_SEC // 3600} h, "
        f"output {OUTPUT_PATH}")

    next_run = last_scan_at("idealista") + DAEMON_INTERVAL_SEC
    while True:
        if time.time() < next_run:
            log(f"Next scan at {datetime.fromtimestamp(next_run):%Y-%m-%d %H:%M}")
            try:
                wait_and_drip("idealista", next_run, log)
            except KeyboardInterrupt:
                log("Interrupted during sleep — stopping Idealista daemon.")
                log_fh.close()
                raise
        log("Running Idealista scan…")
        new_listings = []
        scan_ok = False
        try:
            new_listings = run_marked("idealista", run_once, args)

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
            scan_ok = True

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

        if scan_ok:
            mark_scan_done("idealista")
            next_run = time.time() + DAEMON_INTERVAL_SEC
        else:
            next_run = time.time() + DAEMON_RETRY_SEC


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch rental/sale listings for an Italian city from Idealista.it and score against OMI."
    )
    p.add_argument("--city",      type=str,   default="milano",
                   choices=list(CITY_REGISTRY.keys()),
                   help="City to scan (default: milano)")
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
    p.add_argument("--scan",      choices=("full", "daily"), default=None,
                   help="full = every page of every zone; daily = newest per zone "
                        "+ rotating sweep (default with --daemon)")
    p.add_argument("--daemon",    action="store_true",
                   help="Loop forever, fetching every 60 minutes")
    p.add_argument("--email",     action="store_true",
                   help="Send daily email digest (requires --daemon; see email_digest.py)")
    p.add_argument("--netlify",   action="store_true",
                   help="Deploy dashboard/ to Netlify after each scan "
                        "(requires netlify_config.json with site_id + token)")
    p.add_argument("--mode",     choices=["rental", "sale"], default="rental",
                   help="rental (default) or sale (vendita-case)")
    args = p.parse_args()
    if args.scan is None:
        args.scan = "daily" if args.daemon else "full"
    return args


def main():
    args = parse_args()
    # Rebind every per-city module global (URLs, paths, tokens) before
    # the scan logic kicks in. This also recurses into fetch_rentals
    # so its OUTPUT_PATH / SEEN_IDS_PATH match.
    _apply_city(args.city)

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
    print(f"  Mode     : {'daemon (every %d h)' % (DAEMON_INTERVAL_SEC // 3600) if args.daemon else 'one-shot'}")
    if args.netlify:
        print(f"  Netlify  : enabled (→ direct deploy after each scan)")
    print(f"{'─'*52}")
    print(f"  NOTE: Idealista may show a CAPTCHA on first visit.")
    print(f"        Solve it in the Edge window — the scan resumes automatically.")
    print(f"{'─'*52}\n")

    if args.mode == "sale":
        run_sale_once(args)
    elif args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
