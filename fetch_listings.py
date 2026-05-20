#!/usr/bin/env python3
"""
immobiliare_scorer/fetch_listings.py
─────────────────────────────────────
Fetches sale listings from Immobiliare.it (unofficial API),
scores each one against OMI benchmark data, and exports to CSV + JSON.

Usage:
    python fetch_listings.py                        # Napoli + Milano, defaults
    python fetch_listings.py --city napoli          # single city
    python fetch_listings.py --city milano --pages 5
    python fetch_listings.py --city napoli --max-price 300000 --min-sqm 50
    python fetch_listings.py --all-cities           # every configured city
    python fetch_listings.py --output my_run        # custom output prefix

Output files:
    listings_YYYYMMDD_HHMMSS.csv    → import into Excel / the dashboard
    listings_YYYYMMDD_HHMMSS.json   → feed into the HTML scorer tool
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re as _re
import sys
import unicodedata
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import nodriver as uc

# Microsoft Edge is preferred over Chrome — Immobiliare's bot detection lets
# Edge through more reliably. Override via $BROWSER_EXECUTABLE_PATH.
EDGE_PATH = os.environ.get(
    "BROWSER_EXECUTABLE_PATH",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

# ── Floor parsing ──────────────────────────────────────────────────────────────

_FLOOR_MAP: dict[str, int] = {
    'T': 0, 'PT': 0, 'TERRA': 0, 'PIANO TERRA': 0,
    'P.T.': 0, 'PT.': 0, 'RDC': 0, 'GROUND': 0, 'G': 0,
    'R': 1, 'RIALZATO': 1, 'PR': 1, 'PIANO RIALZATO': 1,
    'S': -1, 'S1': -1, 'SEMI': -1, 'SEMINTERRATO': -1,
    'I': -2, 'INTERRATO': -2, 'SOTTOSUOLO': -2,
    'S2': -2, 'S3': -2, 'S4': -2, 'S5': -2,
}
_RIALZATO_TOKENS: frozenset = frozenset({'R', 'RIALZATO', 'PR', 'PIANO RIALZATO'})
_RE_FLOOR_NUM = _re.compile(r'-?\d+')


def _floor_token(tok: str) -> tuple:
    t = tok.strip().upper()
    if t in _FLOOR_MAP:
        return _FLOOR_MAP[t], t in _RIALZATO_TOKENS
    m = _RE_FLOOR_NUM.search(t)
    if m:
        return int(m.group()), False
    return None, False


def parse_floor(raw_floor) -> tuple:
    """
    Parse an Immobiliare.it floor field → (floor_n: int|None, floor_label: str|None).
    Handles single codes (T, R, S), sub-basement codes (S2-S5),
    compound ranges ('S, 3', '4 - 5', 'T, R'), and dict abbreviations.
    Returns (None, None) when unparseable.
    """
    if raw_floor is None:
        return None, None
    if isinstance(raw_floor, dict):
        raw_floor = raw_floor.get('abbreviation') or raw_floor.get('value') or ''
    s = str(raw_floor).strip()
    if not s or s.upper() in ('NONE', 'N/A', ''):
        return None, None
    tokens = _re.split(r',\s*|\s+[-–]\s+', s)
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        return None, None
    parsed: list = []
    has_rialzato = False
    for tok in tokens:
        fn, is_r = _floor_token(tok)
        if fn is not None:
            parsed.append(fn)
        if is_r:
            has_rialzato = True
    if not parsed:
        return None, None
    floor_n = min(parsed)
    if floor_n <= -2:
        label = 'Interrato'
    elif floor_n == -1:
        label = 'Seminterrato'
    elif floor_n == 0:
        label = 'Piano terra'
    elif floor_n == 1 and has_rialzato:
        label = 'Piano rialzato'
    else:
        label = f'{floor_n}° piano'
    return floor_n, label


def to_url_slug(name: str) -> str:
    """Normalise a neighbourhood name to a URL path segment.

    Examples: 'Città Studi' → 'citta-studi', 'Porta Venezia' → 'porta-venezia'
    """
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower().strip()
    name = _re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")

# ── Fake / foreign-property bait detection ────────────────────────────────────
# Some agencies post listings claiming to be in Milan / Rome but actually
# advertise foreign real estate (Albania / Dubai / Montenegro / etc.).
# They score artificially high because their prices look "way below comps"
# next to Italian benchmarks. Detect them at scrape time and flag
# is_fake=True; scoring.score_sale_listing short-circuits the flag the
# same way it does for is_auction / is_nuda_proprieta.
#
# Two signal families:
#   1. Foreign country / city mentions — always a fake in an Italian city
#      search.
#   2. Coastal-amenity language ("vista mare", "spiaggia privata") — fake
#      in inland cities (Milano, Roma), legitimate in coastal ones
#      (Napoli, La Maddalena). Whitelist via _SEA_VIEW_OK_CITIES.
#   3. All-caps title heuristic — scam agencies write the title in
#      SHOUTING because they're chasing impressions. Real Italian agents
#      use title case.
_FAKE_FOREIGN_KEYWORDS: tuple[str, ...] = (
    # Albania
    "albania", "albanian", "tirana", "durres", "saranda", "vlora", "vlore",
    # UAE / Gulf
    "dubai", "emirati arabi", "uae", "abu dhabi",
    # Other Balkans / Med
    "montenegro", "podgorica", "budva",
    "turchia", "turkey", "istanbul", "antalya",
    "portogallo", "portugal", "lisbona", "porto",
    "grecia", "greece", "atene", "athens", "creta",
    "croazia", "croatia", "zagabria", "dubrovnik",
    "marocco", "morocco", "marrakech", "casablanca",
    # Bait language
    "shop like a billionaire",
    "invest abroad",
    "acquista all'estero",
    "investi all'estero",
    "resort & residence",
    "luxury resort abroad",
)
_SEA_VIEW_KEYWORDS: tuple[str, ...] = (
    "spiaggia privata",   # private beach — impossible inland
    "marina privata",     # private marina
    "vista mare",         # sea view — only legitimate in coastal cities
    "fronte mare",        # seafront
    "sul mare",           # on the sea
    "costa mare",         # coast
)
_SEA_VIEW_OK_CITIES: frozenset[str] = frozenset({"napoli", "la_maddalena"})


def detect_fake_listing(title: str, description: str, city: str = "milano") -> bool:
    """
    Returns True if this looks like a fraudulent bait-and-switch — an
    Italian city listing that actually advertises foreign property or
    impossible amenities (sea view in Milan etc.).

    Only scans the first 1000 chars of the description so we don't false
    positive on listings that mention a foreign country incidentally
    ("renovated by Albanian craftsmen", "the previous owner moved to
    Portugal", etc.) deep in the body.
    """
    text  = ((title or "") + " " + (description or "")[:1000]).lower()
    city  = (city or "milano").lower()

    # 1. Foreign country / city mentions — always fake in an IT search.
    if any(kw in text for kw in _FAKE_FOREIGN_KEYWORDS):
        return True

    # 2. Coastal-amenity language — only flag for inland cities.
    if city not in _SEA_VIEW_OK_CITIES:
        if any(kw in text for kw in _SEA_VIEW_KEYWORDS):
            return True

    # 3. All-caps title heuristic. Skip very short titles (≤ 3 words)
    #    because "VILLA IN VENDITA" can come from a legit agent header.
    title_words = (title or "").split()
    if len(title_words) >= 4:
        long_words = [w for w in title_words if len(w) > 2]
        if long_words:
            caps_ratio = sum(1 for w in long_words if w.isupper()) / len(long_words)
            if caps_ratio > 0.5:    # >50% of long words ALL-CAPS → scam pattern
                return True

    return False


# ── Misrepresented-address detection ──────────────────────────────────────────
# A second fraud pattern, separate from outright "foreign property" bait:
# agencies post a Milan title but the actual property sits in a nearby comune
# (Opera, Rozzano, San Donato, etc.). Comps then read the listing as a
# bargain on the Milan curve when in fact it's normally priced for its real
# location. Three signals — text, distance phrase, coordinate bbox.

# Comuni near each major city but legally separate (own ISTAT codes, own
# OMI tables). Listings claiming to be in {city} but mentioning one of
# these comuni in the description are almost certainly misrepresented.
# Comparable lookups should not run against {city} comps for them.
MILAN_ADJACENT_COMUNI: frozenset[str] = frozenset({
    # Hinterland sud
    "opera", "rozzano", "locate di triulzi", "locate triulzi",
    "san donato milanese", "san giuliano milanese", "pieve emanuele",
    "pantigliate", "mediglia", "buccinasco", "corsico", "cesano boscone",
    "trezzano sul naviglio", "assago",
    # Hinterland est
    "segrate", "pioltello", "cologno monzese", "sesto san giovanni",
    "cinisello balsamo", "cusano milanino", "cormano", "bresso",
    "vimodrone", "cernusco sul naviglio",
    # Hinterland nord
    "paderno dugnano", "rho", "pero", "settimo milanese",
    "novate milanese", "baranzate",
    # Hinterland ovest
    "cusago", "corbetta", "magenta", "arluno",
})

ROMA_ADJACENT_COMUNI: frozenset[str] = frozenset({
    "fiumicino", "ciampino", "guidonia", "guidonia montecelio",
    "pomezia", "tivoli", "monterotondo", "frascati", "albano",
    "castel gandolfo", "marino", "grottaferrata",
})

NAPOLI_ADJACENT_COMUNI: frozenset[str] = frozenset({
    "pozzuoli", "casoria", "afragola", "portici", "ercolano",
    "torre del greco", "castellammare di stabia", "pompei",
    "san giorgio a cremano", "casalnuovo di napoli", "marano di napoli",
    "arzano", "frattamaggiore", "mugnano di napoli", "qualiano",
})

ADJACENT_COMUNI_BY_CITY: dict[str, frozenset[str]] = {
    "milano": MILAN_ADJACENT_COMUNI,
    "roma":   ROMA_ADJACENT_COMUNI,
    "napoli": NAPOLI_ADJACENT_COMUNI,
}

# Tight per-comune bounding boxes (the actual administrative boundary, not
# the loose Lombardy/Lazio rectangle the map filter uses). Coordinates
# outside these win a "definitely not in this city" verdict regardless of
# what the title says — the strongest of the three signals.
CITY_COMUNE_BBOX: dict[str, dict[str, float]] = {
    "milano": {"lat_min": 45.388, "lat_max": 45.535,
               "lng_min":  9.065, "lng_max":  9.280},
    "roma":   {"lat_min": 41.755, "lat_max": 42.085,
               "lng_min": 12.235, "lng_max": 12.730},
    "napoli": {"lat_min": 40.782, "lat_max": 40.920,
               "lng_min": 14.140, "lng_max": 14.380},
    "la_maddalena": {"lat_min": 41.165, "lat_max": 41.250,
                     "lng_min":  9.345, "lng_max":  9.475},
}

# Distance phrase: "a X km da Milano" / "a soli X km" — listings that
# admit they're 3+ km away from the claimed city are misrepresented.
# "a (soli) X km (ca/circa) da [milano/roma/napoli]"
#   - The optional "circa"/"ca." carries its own leading space so the
#     pattern still matches "a 8 km da Milano" without it.
#   - "\d*" lets the decimal "5,5" notation through ("a 5,5 km da Milano").
_DISTANCE_PATTERN = _re.compile(
    r"a\s+(?:solo|soli)?\s*(\d+)[\s,.]?\d*\s*km(?:\s+(?:ca\.?|circa))?\s+da"
)


def detect_misrepresented_address(title: str,
                                  description: str,
                                  city: str = "milano") -> tuple[bool, str]:
    """
    Returns (is_misrepresented, reason). Three signals, checked in order:
      A) Comune name immediately followed by its province code parenthetical
         — e.g. "Opera (MI)" inside a "Milano" listing.
      B) "a X km da [city]" admission with X ≥ 3.
      C) Adjacent comune name in the first 300 chars (description preamble).

    Conservative on false-positives: legitimate Milan listings sometimes
    say "Milano (MI)" or reference a comune as a landmark deep in the body
    — neither pattern fires.
    """
    adjacent = ADJACENT_COMUNI_BY_CITY.get((city or "milano").lower())
    if not adjacent:
        return False, ""

    desc_lower = (description or "")[:1500].lower()
    if not desc_lower:
        return False, ""

    # A) Comune + province parenthetical. The (MI) / (RM) / (NA) sticker is
    #    a deliberate marker agencies write so the buyer doesn't get confused
    #    — which is exactly the smoking gun we want to flag.
    for comune in adjacent:
        for marker in (f"{comune} (mi)", f"{comune}(mi)", f"{comune}, mi",
                       f"{comune} (rm)", f"{comune}, rm",
                       f"{comune} (na)", f"{comune}, na"):
            if marker in desc_lower:
                return True, f'Listed as {city} but description says "{comune}"'

    # B) Distance-disclaimer phrase. Anything < 3 km is plausible Milan
    #    (a 2-km radius covers a lot of the city); ≥ 3 km is definitively
    #    out of the central comune.
    m = _DISTANCE_PATTERN.search(desc_lower)
    if m:
        try:
            km = int(m.group(1))
        except (TypeError, ValueError):
            km = 0
        if km >= 3:
            return True, f"Description admits property is {km} km away from claimed location"

    # C) Adjacent comune name in the opening 300 chars without the
    #    province sticker. Catches "Ad Opera proponiamo bilocale…"-style
    #    openings where the marker convention isn't used.
    head = desc_lower[:300]
    for comune in adjacent:
        if f" {comune} " in head or head.startswith(comune + " "):
            return True, f'Description opens with reference to "{comune}"'

    return False, ""


def is_outside_city_bbox(lat: float | None,
                         lng: float | None,
                         city: str) -> bool:
    """
    True when the listing has coordinates AND those coordinates fall
    outside the city's comune bounding box. Listings without coords
    return False (we can't prove they're misrepresented either way).
    """
    bbox = CITY_COMUNE_BBOX.get((city or "milano").lower())
    if not bbox or not lat or not lng:
        return False
    return not (bbox["lat_min"] <= lat <= bbox["lat_max"] and
                bbox["lng_min"] <= lng <= bbox["lng_max"])


# ── City config ───────────────────────────────────────────────────────────────
# url_slug: the /vendita-case/{slug}/ URL segment
CITIES = {
    "napoli":       {"label": "Napoli",       "url_slug": "napoli"},
    "milano":       {"label": "Milano",       "url_slug": "milano"},
    "roma":         {"label": "Roma",         "url_slug": "roma"},
    "torino":       {"label": "Torino",       "url_slug": "torino"},
    "firenze":      {"label": "Firenze",      "url_slug": "firenze"},
    "bologna":      {"label": "Bologna",      "url_slug": "bologna"},
    "palermo":      {"label": "Palermo",      "url_slug": "palermo"},
    "bari":         {"label": "Bari",         "url_slug": "bari"},
    "catania":      {"label": "Catania",      "url_slug": "catania"},
    "verona":       {"label": "Verona",       "url_slug": "verona"},
    # Added in multi-city migration 007 — tiny island market.
    "la_maddalena": {"label": "La Maddalena", "url_slug": "la-maddalena"},
}

# ── OMI benchmark data (Agenzia Entrate, 2° sem 2025) ─────────────────────────
# Structure: city_key → neighbourhood_keyword → {fascia, bmin, bmax, rmin, rmax}
# bmin/bmax: purchase €/m²   rmin/rmax: rent €/m²/month
# Fascia corrected vs raw geographic ring:
#   Sanità, Tribunali, Materdei, Avvocata, Garibaldi → B (not A)
#   Scampia, Ponticelli, Barra → C (not B)
#   Porta Romana Milano → A (not B)

OMI = {
    "napoli": {
        "centro storico": dict(fascia="A", bmin=3200, bmax=4800, rmin=11.0, rmax=17.0),
        "chiaia":         dict(fascia="A", bmin=4200, bmax=6500, rmin=14.0, rmax=22.0),
        "posillipo":      dict(fascia="A", bmin=4200, bmax=6500, rmin=14.0, rmax=22.0),
        "vomero":         dict(fascia="A", bmin=3500, bmax=5200, rmin=12.0, rmax=18.0),
        "arenella":       dict(fascia="A", bmin=3500, bmax=5200, rmin=12.0, rmax=18.0),
        "mergellina":     dict(fascia="A", bmin=3800, bmax=5800, rmin=13.0, rmax=20.0),
        # corrected A→B
        "avvocata":       dict(fascia="B", bmin=1800, bmax=2600, rmin=7.0,  rmax=10.5),
        "garibaldi":      dict(fascia="B", bmin=1700, bmax=2500, rmin=6.5,  rmax=10.0),
        "materdei":       dict(fascia="B", bmin=1900, bmax=2800, rmin=7.5,  rmax=11.5),
        "sanità":         dict(fascia="B", bmin=1600, bmax=2400, rmin=6.5,  rmax=10.0),
        "sanita":         dict(fascia="B", bmin=1600, bmax=2400, rmin=6.5,  rmax=10.0),
        "tribunali":      dict(fascia="B", bmin=1800, bmax=2700, rmin=7.0,  rmax=11.0),
        # correct B
        "fuorigrotta":    dict(fascia="B", bmin=1800, bmax=2800, rmin=7.0,  rmax=11.0),
        "bagnoli":        dict(fascia="B", bmin=1800, bmax=2800, rmin=7.0,  rmax=11.0),
        "soccavo":        dict(fascia="B", bmin=1400, bmax=2200, rmin=6.0,  rmax=9.5),
        "pianura":        dict(fascia="B", bmin=1400, bmax=2200, rmin=6.0,  rmax=9.5),
        "secondigliano":  dict(fascia="B", bmin=900,  bmax=1500, rmin=4.0,  rmax=7.0),
        # corrected B→C
        "scampia":        dict(fascia="C", bmin=750,  bmax=1200, rmin=3.5,  rmax=6.0),
        "ponticelli":     dict(fascia="C", bmin=900,  bmax=1450, rmin=4.0,  rmax=6.5),
        "barra":          dict(fascia="C", bmin=900,  bmax=1450, rmin=4.0,  rmax=6.5),
        "miano":          dict(fascia="C", bmin=850,  bmax=1350, rmin=3.8,  rmax=6.5),
        "chiaiano":       dict(fascia="C", bmin=850,  bmax=1350, rmin=3.8,  rmax=6.5),
        "pozzuoli":       dict(fascia="C", bmin=1200, bmax=2000, rmin=5.0,  rmax=8.5),
        # microzones seen in live data — added after 2026-04 fetch
        "monte di dio":   dict(fascia="A", bmin=4000, bmax=6200, rmin=13.5, rmax=21.0),
        "marechiaro":     dict(fascia="A", bmin=4200, bmax=6500, rmin=14.0, rmax=22.0),
        "amedeo":         dict(fascia="A", bmin=4000, bmax=6000, rmin=13.0, rmax=20.0),
        "parco margherita": dict(fascia="A", bmin=4000, bmax=6000, rmin=13.0, rmax=20.0),
        "rione alto":     dict(fascia="B", bmin=2000, bmax=3000, rmin=7.5,  rmax=11.5),
        "monte santo":    dict(fascia="B", bmin=1900, bmax=2800, rmin=7.5,  rmax=11.5),
        "ospedaliera":    dict(fascia="B", bmin=2200, bmax=3200, rmin=8.0,  rmax=12.0),
        "porto":          dict(fascia="B", bmin=2000, bmax=3000, rmin=7.5,  rmax=11.5),
        "municipio":      dict(fascia="B", bmin=2000, bmax=3000, rmin=7.5,  rmax=11.5),
        "arenaccia":      dict(fascia="B", bmin=1500, bmax=2300, rmin=6.0,  rmax=9.5),
        "zona industriale": dict(fascia="B", bmin=1600, bmax=2500, rmin=6.5, rmax=10.0),
        "piscinola":      dict(fascia="C", bmin=850,  bmax=1350, rmin=3.8,  rmax=6.5),
        "san carlo":      dict(fascia="B", bmin=1500, bmax=2300, rmin=6.0,  rmax=9.5),
        "ponti rossi":    dict(fascia="B", bmin=1400, bmax=2100, rmin=5.5,  rmax=9.0),
        "orefici":        dict(fascia="B", bmin=2000, bmax=3000, rmin=7.5,  rmax=11.5),
        "poggioreale":    dict(fascia="B", bmin=1200, bmax=1900, rmin=5.0,  rmax=8.0),
        "mercato":        dict(fascia="B", bmin=1700, bmax=2600, rmin=6.5,  rmax=10.5),
    },

    "milano": {
        "brera":          dict(fascia="A", bmin=9000, bmax=14000, rmin=28.0, rmax=42.0),
        "duomo":          dict(fascia="A", bmin=8500, bmax=13000, rmin=26.0, rmax=40.0),
        "centro":         dict(fascia="A", bmin=8500, bmax=13000, rmin=26.0, rmax=40.0),
        "porta venezia":  dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "buenos aires":   dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "isola":          dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "navigli":        dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "porta ticinese": dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "tortona":        dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "porta romana":   dict(fascia="A", bmin=4800, bmax=7200,  rmin=16.0, rmax=24.0),
        "città studi":    dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "citta studi":    dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "lambrate":       dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "loreto":         dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "medaglie d'oro": dict(fascia="B", bmin=3800, bmax=5800,  rmin=13.0, rmax=20.0),
        "bovisa":         dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "dergano":        dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "affori":         dict(fascia="B", bmin=2500, bmax=3800,  rmin=9.0,  rmax=14.0),
        "niguarda":       dict(fascia="B", bmin=2500, bmax=3800,  rmin=9.0,  rmax=14.0),
        "bicocca":        dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "via padova":     dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "feltre":         dict(fascia="B", bmin=3200, bmax=4500,  rmin=12.0, rmax=18.0),
        "cimiano":        dict(fascia="B", bmin=3200, bmax=4500,  rmin=12.0, rmax=18.0),
        "famagosta":      dict(fascia="C", bmin=2400, bmax=3600,  rmin=9.0,  rmax=14.0),
        "lorenteggio":    dict(fascia="C", bmin=2400, bmax=3600,  rmin=9.0,  rmax=14.0),
        "giambellino":    dict(fascia="C", bmin=2400, bmax=3600,  rmin=9.0,  rmax=14.0),
        "rogoredo":       dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "mecenate":       dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "forlanini":      dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "quarto oggiaro": dict(fascia="C", bmin=1800, bmax=2800,  rmin=7.0,  rmax=11.0),
        "comasina":       dict(fascia="C", bmin=1800, bmax=2800,  rmin=7.0,  rmax=11.0),
        "bruzzano":       dict(fascia="C", bmin=1800, bmax=2800,  rmin=7.0,  rmax=11.0),
        "romolo":         dict(fascia="C", bmin=2400, bmax=3600,  rmin=9.0,  rmax=14.0),
        "bonola":         dict(fascia="C", bmin=2000, bmax=3200,  rmin=7.5,  rmax=12.0),
        "trenno":         dict(fascia="C", bmin=2000, bmax=3200,  rmin=7.5,  rmax=12.0),
        # Fascia A — central/prestige microzones
        "moscova":        dict(fascia="A", bmin=6000, bmax=9000,  rmin=20.0, rmax=30.0),
        "garibaldi":      dict(fascia="A", bmin=6000, bmax=9500,  rmin=20.0, rmax=32.0),
        "corso como":     dict(fascia="A", bmin=6000, bmax=9500,  rmin=20.0, rmax=32.0),
        "arco della pace": dict(fascia="A", bmin=5500, bmax=8500, rmin=18.0, rmax=28.0),
        "guastalla":      dict(fascia="A", bmin=6000, bmax=9000,  rmin=20.0, rmax=30.0),
        "ticinese":       dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "corso genova":   dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "indipendenza":   dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "piave":          dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "tricolore":      dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "vincenzo monti": dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "washington":     dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "solari":         dict(fascia="A", bmin=5000, bmax=7800,  rmin=17.0, rmax=26.0),
        "paolo sarpi":    dict(fascia="A", bmin=4800, bmax=7500,  rmin=16.0, rmax=25.0),
        "centrale":       dict(fascia="A", bmin=5000, bmax=7500,  rmin=17.0, rmax=25.0),
        "amendola":       dict(fascia="A", bmin=4800, bmax=7500,  rmin=16.0, rmax=25.0),
        "buonarroti":     dict(fascia="A", bmin=4800, bmax=7500,  rmin=16.0, rmax=25.0),
        "melchiorre":     dict(fascia="A", bmin=4500, bmax=7000,  rmin=15.0, rmax=23.0),
        # Fascia B — inner-ring microzones
        "san siro":       dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "montenero":      dict(fascia="B", bmin=3500, bmax=5500,  rmin=13.0, rmax=20.0),
        "argonne":        dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "corsica":        dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "maggiolina":     dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "monte rosa":     dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "lotto":          dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "casoretto":      dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "precotto":       dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "rovereto":       dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "turro":          dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "parco trotter":  dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "pasteur":        dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "udine":          dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "ghisolfa":       dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "cenisio":        dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "plebisciti":     dict(fascia="B", bmin=3000, bmax=4500,  rmin=11.0, rmax=17.0),
        "pezzotti":       dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "ca granda":      dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "ca' granda":     dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "tre castelli":   dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        "villa san giovanni": dict(fascia="B", bmin=2800, bmax=4200, rmin=10.0, rmax=15.0),
        "siena":          dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=15.0),
        # Fascia C — outer/peripheral microzones
        "vigentino":      dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "corvetto":       dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "ripamonti":      dict(fascia="C", bmin=2400, bmax=3600,  rmin=9.0,  rmax=14.0),
        "gratosoglio":    dict(fascia="C", bmin=1800, bmax=2800,  rmin=7.0,  rmax=11.0),
        "bisceglie":      dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "certosa":        dict(fascia="C", bmin=2000, bmax=3200,  rmin=7.5,  rmax=12.0),
        "uptown":         dict(fascia="C", bmin=2200, bmax=3600,  rmin=8.0,  rmax=13.0),
        "cascina merlata": dict(fascia="C", bmin=2200, bmax=3600, rmin=8.0,  rmax=13.0),
        "quartiere adriano": dict(fascia="C", bmin=2000, bmax=3200, rmin=7.5, rmax=12.0),
        "adriano":        dict(fascia="C", bmin=2000, bmax=3200,  rmin=7.5,  rmax=12.0),
        "musocco":        dict(fascia="C", bmin=2000, bmax=3200,  rmin=7.5,  rmax=12.0),
        "cermenate":      dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
        "abbiategrasso":  dict(fascia="C", bmin=2200, bmax=3400,  rmin=8.0,  rmax=13.0),
    },
    "roma": {
        "centro storico": dict(fascia="A", bmin=7000, bmax=12000, rmin=22.0, rmax=35.0),
        "prati":          dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "parioli":        dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "flaminio":       dict(fascia="A", bmin=5500, bmax=8500,  rmin=18.0, rmax=28.0),
        "trastevere":     dict(fascia="A", bmin=5000, bmax=8000,  rmin=17.0, rmax=26.0),
        "testaccio":      dict(fascia="A", bmin=5000, bmax=8000,  rmin=17.0, rmax=26.0),
        "pigneto":        dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=16.0),
        "prenestino":     dict(fascia="B", bmin=2800, bmax=4200,  rmin=10.0, rmax=16.0),
        "nomentano":      dict(fascia="B", bmin=3500, bmax=5500,  rmin=13.0, rmax=20.0),
        "ostiense":       dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "garbatella":     dict(fascia="B", bmin=3200, bmax=5000,  rmin=12.0, rmax=18.0),
        "tiburtino":      dict(fascia="B", bmin=2200, bmax=3500,  rmin=8.5,  rmax=13.0),
        "tor bella monaca": dict(fascia="C", bmin=1500, bmax=2400, rmin=6.0, rmax=10.0),
        "spinaceto":      dict(fascia="C", bmin=1600, bmax=2600,  rmin=6.5,  rmax=10.5),
        "primavalle":     dict(fascia="C", bmin=1800, bmax=2900,  rmin=7.0,  rmax=11.0),
    },
}

# City fallback averages (used when neighbourhood doesn't match any keyword)
CITY_FALLBACKS = {
    "napoli":  dict(fascia="B", bmin=1800, bmax=2800, rmin=7.0,  rmax=11.0),
    "milano":  dict(fascia="B", bmin=3500, bmax=5500, rmin=12.0, rmax=18.0),
    "roma":    dict(fascia="B", bmin=3000, bmax=4800, rmin=11.0, rmax=17.0),
    "torino":  dict(fascia="B", bmin=1500, bmax=2400, rmin=6.0,  rmax=10.0),
    "firenze": dict(fascia="B", bmin=2200, bmax=3400, rmin=8.0,  rmax=13.0),
    "bologna": dict(fascia="B", bmin=2000, bmax=3200, rmin=7.5,  rmax=12.0),
    "palermo": dict(fascia="B", bmin=1000, bmax=1800, rmin=4.5,  rmax=7.5),
    "bari":    dict(fascia="B", bmin=1300, bmax=2200, rmin=5.5,  rmax=9.0),
    "catania": dict(fascia="B", bmin=1000, bmax=1800, rmin=4.0,  rmax=7.0),
    "verona":  dict(fascia="B", bmin=1800, bmax=2800, rmin=7.0,  rmax=11.0),
}


# ── OMI correction coefficients ──────────────────────────────────────────────

def surface_coeff(sqm: int) -> float:
    """
    OMI surface discount factor.
    Smaller units command higher €/m² rent; larger units discount.
    """
    if sqm < 50:    return 1.20
    if sqm <= 85:   return 1.00
    if sqm <= 115:  return 0.90
    if sqm <= 145:  return 0.82
    return 0.75


def condition_coeff(condition: str) -> float:
    """
    OMI condition discount factor applied to the rent estimate.
    Matches against the ga4Condition / condition string from the listing.
    Default 0.85 (conservative) when condition is unknown.
    """
    c = (condition or "").lower()
    # Check best condition first to avoid substring ambiguity
    if any(k in c for k in ("ristrutturato", "ottimo", "nuovo", "eccellente")):
        return 1.00
    if any(k in c for k in ("da ristrutturare", "fatiscente")):
        return 0.70
    if any(k in c for k in ("buono", "abitabile", "normale", "discreto")):
        return 0.85
    return 0.85   # unknown / missing → conservative default


def _normalize_condition(raw: str) -> str:
    """Map raw Italian condition text to canonical scoring value."""
    c = (raw or "").lower()
    if "da ristrutturare" in c:
        return "da_ristrutturare"
    if "fatiscente" in c:
        return "fatiscente"
    if any(k in c for k in ("ristrutturato",)):
        return "ristrutturato"
    if any(k in c for k in ("nuovo", "eccellente", "ottimo")):
        return "ottimo"
    if any(k in c for k in ("buono", "normale")):
        return "buono"
    if any(k in c for k in ("abitabile", "discreto")):
        return "abitabile"
    return raw or ""   # keep raw text if nothing matched


# ── OMI matching ──────────────────────────────────────────────────────────────

def match_omi(city_key: str, neighbourhood: str) -> dict:
    """
    Match a neighbourhood string to the best OMI zone entry.
    Returns the OMI dict plus a `zone` key with the matched name.
    Falls back to city average if no keyword matches.
    """
    city_data = OMI.get(city_key, {})
    nb_lower = neighbourhood.lower().strip()

    # Match: keyword must appear inside the neighbourhood string (not the reverse).
    # This avoids "chiaia" matching "chiaiano" because "chiaia" ⊂ "chiaiano" falsely.
    # We prefer the longest matching keyword (most specific zone name wins).
    best_key = None
    best_len = 0
    for keyword in city_data:
        # exact match OR keyword is a substring of neighbourhood
        if nb_lower == keyword or keyword in nb_lower:
            if len(keyword) > best_len:
                best_key = keyword
                best_len = len(keyword)

    if best_key:
        return {"zone": best_key, **city_data[best_key]}

    # fallback
    fb = CITY_FALLBACKS.get(city_key, dict(fascia="B", bmin=1500, bmax=3000, rmin=6.0, rmax=12.0))
    return {"zone": "city average", **fb}


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_listing(listing: dict, all_listings: list) -> dict:
    """
    Compute scores for a single listing.
    all_listings is needed to compute within-fascia percentile.
    Returns a dict of scoring fields to merge into the listing.
    """
    omi = listing["omi"]
    ask_psqm = listing["ask_psqm"]
    if ask_psqm is None or ask_psqm <= 0:
        return {}

    omi_bench    = (omi["bmin"] + omi["bmax"]) / 2
    omi_rent_mid = (omi["rmin"] + omi["rmax"]) / 2

    vs_omi = (ask_psqm - omi_bench) / omi_bench          # negative = cheaper

    # Raw OMI rent (no corrections) – kept as a reference figure
    omi_rent_raw = omi_rent_mid * listing["sqm"]

    # Apply OMI correction coefficients
    surf_c = surface_coeff(listing["sqm"])
    cond_c = condition_coeff(listing.get("condition", ""))
    est_rent_mo = omi_rent_raw * surf_c * cond_c
    est_yield   = (est_rent_mo * 12 / listing["price"]) * 100

    # price score: −20% under → 100, at bench → 50, +20% over → 0
    price_score = max(0, min(100, 50 - vs_omi * 250))

    # yield score: 7% → 100, 5% → 70, 4% → 50, 2.5% → 0
    yield_score = max(0, min(100, (est_yield - 2.5) / 4.5 * 100))

    # within-fascia percentile
    fascia = omi["fascia"]
    fascia_peers = [
        l["ask_psqm"] for l in all_listings
        if l.get("omi", {}).get("fascia") == fascia
        and l.get("ask_psqm") and l["ask_psqm"] > 0
    ]
    fascia_peers_sorted = sorted(fascia_peers)
    if fascia_peers_sorted:
        rank = sum(1 for v in fascia_peers_sorted if v <= ask_psqm)
        fascia_pct = round(rank / len(fascia_peers_sorted) * 100)
    else:
        fascia_pct = 50

    fascia_score = 100 - fascia_pct   # cheaper in fascia = higher score

    total = round(price_score * 0.40 + yield_score * 0.35 + fascia_score * 0.25)

    vs_omi_label = (
        f"{abs(vs_omi*100):.0f}% below OMI" if vs_omi < -0.10
        else "at OMI benchmark" if vs_omi < 0.10
        else f"{vs_omi*100:.0f}% above OMI"
    )
    fascia_label = (
        f"cheap in fascia {fascia}" if fascia_pct <= 33
        else f"mid in fascia {fascia}" if fascia_pct <= 66
        else f"expensive in fascia {fascia}"
    )

    return {
        "omi_zone":       omi["zone"],
        "omi_fascia":     fascia,
        "omi_bench":      round(omi_bench),
        "omi_bmin":       omi["bmin"],
        "omi_bmax":       omi["bmax"],
        "omi_rmin":       omi["rmin"],
        "omi_rmax":       omi["rmax"],
        "vs_omi_pct":     round(vs_omi * 100, 1),
        "vs_omi_label":   vs_omi_label,
        "omi_rent_raw":   round(omi_rent_raw),   # raw OMI rent, no corrections
        "surf_coeff":     surf_c,
        "cond_coeff":     cond_c,
        "est_rent_mo":    round(est_rent_mo),    # corrected rent
        "est_yield_pct":  round(est_yield, 2),
        "fascia_pct":     fascia_pct,
        "fascia_label":   fascia_label,
        "score_price":    round(price_score),
        "score_yield":    round(yield_score),
        "score_fascia":   round(fascia_score),
        "score_total":    total,
    }


# ── Advertiser parser ──────────────────────────────────────────────────────────

_advertiser_warn_logged = False

def parse_advertiser(re_data: dict) -> dict:
    global _advertiser_warn_logged
    wrapper = re_data.get("advertiser") or {}
    adv = wrapper.get("agency") or wrapper.get("private") or wrapper.get("supervisor") or {}
    agency_id   = str(adv.get("id") or "")
    agency_name = (adv.get("displayName") or adv.get("name") or "")
    agency_type = (adv.get("type") or adv.get("label") or "")
    agency_url  = (adv.get("url") or adv.get("profileUrl") or "")
    if not (agency_id or agency_name) and not _advertiser_warn_logged:
        print("  [warn] advertiser fields missing — agency_id/name will be blank")
        _advertiser_warn_logged = True
    return {
        "agency_id":   agency_id,
        "agency_name": agency_name,
        "agency_type": agency_type,
        "agency_url":  agency_url,
    }


# ── Immobiliare.it API fetch ───────────────────────────────────────────────────

def parse_photo_urls(photos_raw: list, cap: int = 8) -> list:
    """
    Extract up to `cap` photo URLs from an Immobiliare multimedia.photos list.
    Prefers the largest available size for each photo and falls back through
    the size variants gracefully so older fixtures still resolve.
    """
    if not isinstance(photos_raw, list):
        return []
    urls = []
    for p in photos_raw[:cap]:
        if not isinstance(p, dict):
            continue
        size_urls = p.get("urls") or {}
        u = (size_urls.get("large")
             or size_urls.get("medium")
             or size_urls.get("small")
             or p.get("url")
             or p.get("src"))
        if isinstance(u, str) and u:
            urls.append(u)
    return urls


def parse_listing(item: dict, city_key: str, city_label: str) -> Optional[dict]:
    """Extract and normalise fields from a single __NEXT_DATA__ result item."""
    re_data = item.get("realEstate", {})
    props = re_data.get("properties", [{}])
    prop = props[0] if props else {}
    location = prop.get("location", {})
    price_data = re_data.get("price", {})

    # price — value may be int or string
    price = price_data.get("value")
    if not price:
        return None
    try:
        price = int(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    # surface — comes as "130 m²" string or plain int
    sqm_raw = prop.get("surface") or prop.get("surfaceValue")
    sqm = None
    if sqm_raw is not None:
        try:
            sqm = int(_re.sub(r"[^\d]", "", str(sqm_raw)))
        except (ValueError, AttributeError):
            pass
    if not sqm or sqm <= 0:
        return None

    ask_psqm = round(price / sqm)

    # rooms
    rooms_raw = prop.get("rooms")
    try:
        rooms = int(rooms_raw) if rooms_raw is not None else None
    except (TypeError, ValueError):
        rooms = None

    # neighbourhood — macrozone/microzone are plain strings in the real API
    def _str_or_name(val):
        if isinstance(val, dict):
            return val.get("name", "")
        return str(val).strip() if val else ""

    microzone  = _str_or_name(location.get("microzone"))
    macrozone  = _str_or_name(location.get("macrozone"))
    neighbourhood = microzone or macrozone or location.get("city", "")

    address   = location.get("address", "")
    latitude  = location.get("latitude")
    longitude = location.get("longitude")

    # floor — may be string or dict with "abbreviation"
    floor_raw = prop.get("floor")
    if isinstance(floor_raw, dict):
        floor = floor_raw.get("abbreviation")
    else:
        floor = str(floor_raw).strip() if floor_raw is not None else None

    # floor_n + floor_label: parsed from raw floor field
    floor_n, floor_label = parse_floor(floor_raw)
    is_below_ground = floor_n is not None and floor_n < 0
    is_ground_floor = floor_n == 0

    # elevator — normalise to bool
    elevator_raw = prop.get("elevator") or prop.get("hasElevator")
    elevator_bool = None
    if elevator_raw is not None:
        elevator_bool = (
            bool(elevator_raw)
            if not isinstance(elevator_raw, str)
            else elevator_raw.lower() not in ("false", "no", "0", "")
        )

    # is_external
    ext_raw = prop.get("isExternal") or prop.get("external") or prop.get("ga4Exposure")
    is_external = None
    if ext_raw is not None:
        if isinstance(ext_raw, bool):
            is_external = ext_raw
        elif isinstance(ext_raw, str):
            is_external = ext_raw.lower() in ("true", "yes", "esterno", "1")

    # energy_class
    energy_class = (
        prop.get("energyClass") or prop.get("energy_class")
        or re_data.get("energyClass") or ""
    )
    energy_class = str(energy_class).strip().upper() if energy_class else None

    # year_built
    year_built = prop.get("yearBuilt") or prop.get("year_built") or re_data.get("yearBuilt")
    if year_built is not None:
        try:
            year_built = int(year_built)
        except (TypeError, ValueError):
            year_built = None

    # bathrooms (= "bagni")
    baths_raw = prop.get("bathrooms") or prop.get("bathRooms")
    bathrooms = None
    if baths_raw is not None:
        try:
            bathrooms = int(baths_raw)
        except (TypeError, ValueError):
            pass

    # bedrooms (= "camere da letto" — bedrooms only, NOT total locali)
    beds_raw = prop.get("bedRoomsNumber") or prop.get("bedrooms")
    bedrooms = None
    if beds_raw not in (None, '', '0', 0):
        try:
            bedrooms = int(beds_raw)
        except (TypeError, ValueError):
            pass
    if bedrooms is not None and rooms is not None and bedrooms > rooms:
        bedrooms = None

    # features[] array — collect type/label keywords for quick lookup
    _feature_types: set = set()
    for feat in (prop.get("features") or re_data.get("features") or []):
        ft = feat.get("type") or feat.get("name") or ""
        _feature_types.add(str(ft).lower())
        lbl = str(feat.get("label") or "").lower()
        for kw in ("balcon", "terrazza", "giardino", "box", "garage", "parcheggio",
                   "arredato", "autonomo", "centralizzato"):
            if kw in lbl:
                _feature_types.add(kw)

    # has_balcony
    has_balcony = None
    for field in ("hasBalcony", "balcony", "hasTerrace", "terrace", "hasGarden", "garden"):
        v = prop.get(field)
        if v is not None:
            has_balcony = bool(v) if not isinstance(v, str) else v.lower() not in ("false", "no", "0", "")
            if has_balcony:
                break
    if has_balcony is None and _feature_types:
        has_balcony = bool({"balcony", "terrace", "garden", "balcon", "terrazza", "giardino"} & _feature_types)

    # has_parking
    has_parking = None
    for field in ("hasParking", "parking", "garage", "hasGarage"):
        v = prop.get(field)
        if v is not None:
            has_parking = bool(v) if not isinstance(v, str) else v.lower() not in ("false", "no", "0", "")
            if has_parking:
                break
    if has_parking is None and _feature_types:
        has_parking = bool({"parking", "garage", "box", "parcheggio"} & _feature_types)

    # heating_type
    heat_raw = prop.get("heatingType") or prop.get("heating") or ""
    heat_str = str(heat_raw).lower()
    if "autonom" in heat_str or "individual" in heat_str or "indipendent" in heat_str:
        heating_type = "autonomous"
    elif "central" in heat_str or "condominil" in heat_str:
        heating_type = "centralised"
    elif heat_str:
        heating_type = "unknown"
    else:
        heating_type = None
    if heating_type is None and _feature_types:
        if {"autonomo", "autonom"} & _feature_types:
            heating_type = "autonomous"
        elif {"centralizzato", "central"} & _feature_types:
            heating_type = "centralised"

    # furnished
    furn_raw = (prop.get("furnished") or prop.get("isFurnished")
                or prop.get("arredato") or re_data.get("furnished"))
    furnished = None
    if furn_raw is not None:
        if isinstance(furn_raw, bool):
            furnished = furn_raw
        elif isinstance(furn_raw, str):
            furnished = furn_raw.lower() in ("true", "yes", "arredato", "1", "si", "sì")
    if furnished is None and "arredato" in _feature_types:
        furnished = True

    # thumbnail + photo gallery — both come from properties[0].multimedia.photos
    # Cap at 8 URLs to keep JSON output size reasonable (~150 chars each).
    photos      = prop.get("multimedia", {}).get("photos", [])
    photo_urls  = parse_photo_urls(photos)
    thumbnail   = photo_urls[0] if photo_urls else None
    photo_count = len(photos) if isinstance(photos, list) else 0

    # published_date + days_on_market
    published_date = None
    days_on_market = None
    for date_field in ("dataModifica", "pubblicazione", "dataInserimento",
                       "publicationDate", "insertionDate", "created_at",
                       "createdAt", "insertDate", "datePublished", "firstSeenDate"):
        raw_date = re_data.get(date_field) or prop.get(date_field)
        if raw_date:
            try:
                pub_str = str(raw_date)[:10]
                pub = datetime.fromisoformat(pub_str).date()
                published_date = pub_str
                days_on_market = (date.today() - pub).days
                break
            except Exception:
                pass

    # condition
    condition_raw = (
        prop.get("ga4Condition")
        or prop.get("condition")
        or re_data.get("typology", {}).get("name", "")
    )
    condition = _normalize_condition(condition_raw)

    listing_id = re_data.get("id", "")
    url = f"https://www.immobiliare.it/annunci/{listing_id}/" if listing_id else ""

    # Detect auctions + nuda proprietà.
    # OLD behaviour: silent drop. Problem — Immobiliare's `__NEXT_DATA__`
    # doesn't always label auctions with "asta" in title/typology, so a
    # subset of judicial-sale listings (€105k Scala-Manzoni etc.) slipped
    # through and polluted the scored feed.
    # NEW behaviour: capture them but flag is_auction / is_nuda_proprieta
    # so the dashboard's existing "Show auctions / nuda proprietà" filters
    # hide them by default while keeping the rows in the DB for users who
    # want them.
    _typology_name = (re_data.get("typology") or {}).get("name", "").lower()
    _title_lower   = (re_data.get("title") or "").lower()
    _contract      = str(re_data.get("contractType", "")).lower()
    _category      = str(re_data.get("category", "")
                         or (re_data.get("category") or {}).get("name", "")
                         if isinstance(re_data.get("category"), dict)
                         else re_data.get("category", "")).lower()
    # Immobiliare exposes the listing description on either prop or re_data;
    # take the first 400 chars to keep the keyword scan cheap. The full
    # description goes into the listing dict too (downstream UI uses it).
    description    = (prop.get("description")
                      or re_data.get("description")
                      or "")
    _desc_head     = description[:400].lower() if description else ""

    is_auction = bool(
        re_data.get("isAuction") or re_data.get("auction")
        or prop.get("isAuction")  or prop.get("auction")
        or "asta" in _typology_name
        or "asta" in _title_lower
        or "asta" in _contract
        or "asta" in _category
        or "auction" in _contract
        or "auction" in _category
    )
    # Nuda proprietà: Immobiliare almost never puts it in the title (0/65
    # in our Milan sample); it surfaces in the description body. Match the
    # same regex Idealista's parser uses so the two scrapers agree on
    # what counts. "nuda prop" catches the abbreviated form some agents
    # use; "usufrutto" catches the seller-retains-life-tenancy variant
    # that's economically equivalent.
    _NUDA_TOKENS = ("nuda propriet", "nuda prop", "usufrutto",
                    "diritto di abitazione", "diritto d'uso", "diritto duso")
    _hay_for_nuda = " ".join((_typology_name, _title_lower, _contract,
                              _category, _desc_head))
    is_nuda = any(tok in _hay_for_nuda for tok in _NUDA_TOKENS)

    # Mislabelled rental drop: a "sale" listing with price/sqm < €100/m²
    # is a monthly-rental figure that got scraped into the sales feed
    # (Immobiliare occasionally surfaces cross-category results). No
    # legitimate sale anywhere in Italy is < €100/m² total — even auction
    # floors land in the hundreds-of-€/m². Drop entirely; flagging would
    # confuse downstream scoring.
    if sqm and sqm > 0:
        _psqm = price / sqm
        if _psqm < 100:
            print(f"  [skip] {listing_id} ({city_key}): €{price}/€{sqm}m² = "
                  f"€{_psqm:.0f}/m² — looks like a monthly rental in the sales feed",
                  flush=True)
            return None

    # Sanity floor: a sale below the per-city OMI minimum is almost
    # certainly an auction in disguise. Flag as auction so it's hidden
    # behind the same filter — the price-floor gate in scoring.py will
    # belt-and-suspenders it with _excluded too.
    _PSQM_FLOOR_BY_CITY = {
        "milano": 800, "roma": 600, "napoli": 400, "la_maddalena": 800,
    }
    _floor = _PSQM_FLOOR_BY_CITY.get(city_key, 400)
    if sqm and sqm > 0 and (price / sqm) < _floor and not is_auction:
        is_auction = True   # silently re-classify as auction
        print(f"  [auction-by-price] {listing_id} ({city_key}): €{price/sqm:.0f}/m² "
              f"< €{_floor}/m² floor → flagged is_auction=True", flush=True)

    # Fake / foreign-property bait detection — sets is_fake=True for
    # listings whose title or description mentions a foreign country
    # (Albania / Dubai / Montenegro / etc.) or an impossible amenity
    # for the listed city (sea view in Milan, private marina in Rome).
    # scoring.score_sale_listing short-circuits on this flag the same
    # way it does for is_auction / is_nuda_proprieta.
    is_fake = detect_fake_listing(
        title       = re_data.get("title") or "",
        description = description,
        city        = city_key,
    )
    if is_fake:
        print(f"  [fake] {listing_id} ({city_key}) — "
              f"{(re_data.get('title') or '')[:70]}",
              flush=True)

    # Misrepresented-address detection — Milan-titled listings that
    # actually sit in Opera / Rozzano / San Donato / etc. Two layers:
    #   1) text — comune name + province sticker in description
    #   2) geometry — coordinates outside the city comune bbox (strongest)
    # Either signal sets is_fake so the existing exclusion path filters
    # them out the same way as foreign-bait listings.
    is_misrep_addr   = False
    is_outside_city  = False
    misrep_reason    = ""
    if not is_fake:
        is_misrep_addr, misrep_reason = detect_misrepresented_address(
            title       = re_data.get("title") or "",
            description = description,
            city        = city_key,
        )
        if is_outside_city_bbox(latitude, longitude, city_key):
            is_outside_city = True
            if not misrep_reason:
                misrep_reason = f"Coordinates outside {city_key} comune"
        if is_misrep_addr or is_outside_city:
            is_fake = True
            print(f"  [misrep-address] {listing_id} ({city_key}) — {misrep_reason}",
                  flush=True)

    omi = match_omi(city_key, neighbourhood)

    return {
        "is_auction":         is_auction,
        "is_nuda_proprieta":  is_nuda,
        "is_fake":            is_fake,
        "is_misrepresented_address": is_misrep_addr,
        "is_outside_city":           is_outside_city,
        "misrep_reason":             misrep_reason or None,
        "id":              str(listing_id),
        "source":          "sale",
        # `city` = lowercase code (Supabase listings.city); `city_label`
        # = display name. `city_key` kept for back-compat callers.
        "city":            city_key,
        "city_label":      city_label,
        "city_key":        city_key,
        "title":           re_data.get("title", ""),
        "description":     description,
        "neighbourhood":   neighbourhood,
        "address":         address,
        "latitude":        latitude,
        "longitude":       longitude,
        "price":           price,
        "sqm":             sqm,
        "ask_psqm":        ask_psqm,
        "rooms":           rooms,
        "bedrooms":        bedrooms,        # camere da letto (bedrooms only)
        "floor":           floor,
        "floor_n":         floor_n,
        "floor_label":     floor_label,
        "is_below_ground": is_below_ground,
        "is_ground_floor": is_ground_floor,
        "elevator":        elevator_bool,
        "is_external":     is_external,
        "energy_class":    energy_class,
        "year_built":      year_built,
        "bathrooms":       bathrooms,
        "has_balcony":     has_balcony,
        "has_parking":     has_parking,
        "heating_type":    heating_type,
        "furnished":       furnished,
        "photo_count":     photo_count,
        "days_on_market":  days_on_market,
        "published_date":  published_date,
        "condition":       condition,
        "thumbnail":       thumbnail,
        "photos":          photo_urls,   # up to 8 photo URLs for the gallery
        "url":             url,
        "fetched_at":      datetime.now().isoformat(timespec="seconds"),
        "omi":             omi,   # temporary – removed before CSV export
        **parse_advertiser(re_data),
    }


async def _fetch_city_async(city_key: str, pages: int, extra_filters: dict,
                             delay: float, browser,
                             area_slugs: list | None = None) -> list:
    """Async inner: navigate pages for one city and extract __NEXT_DATA__.

    When area_slugs is provided each area is fetched in its own page loop;
    a shared seen_ids set prevents cross-area duplicates.
    """
    cfg = CITIES[city_key]
    slug = cfg["url_slug"]
    all_items = []

    # Cross-area deduplication: IDs accumulated across all areas.
    seen_ids: set = set()

    # None sentinel means city-level (no area sub-path)
    targets = area_slugs if area_slugs else [None]

    for area_slug in targets:
        page_ids_seen: set = set()   # recycled-page detector, reset per area
        max_pages = pages

        for page in range(1, pages + 1):
            # Embed filters directly in the URL — the site pre-filters server-side,
            # so we get fewer results per page and need fewer pages overall.
            params = {}
            if extra_filters.get("max_price"): params["prezzoMassimo"]     = extra_filters["max_price"]
            if extra_filters.get("min_price"): params["prezzoMinimo"]      = extra_filters["min_price"]
            if extra_filters.get("min_sqm"):   params["superficieMinima"]  = extra_filters["min_sqm"]
            if extra_filters.get("max_sqm"):   params["superficieMassima"] = extra_filters["max_sqm"]
            if extra_filters.get("min_rooms"): params["localiMinimo"]      = extra_filters["min_rooms"]
            if page > 1:                       params["pag"]               = page

            base = (
                f"https://www.immobiliare.it/vendita-case/{slug}/{area_slug}/"
                if area_slug else
                f"https://www.immobiliare.it/vendita-case/{slug}/"
            )
            url = base + ("?" + urlencode(params) if params else "")

            tab = await browser.get(url)
            await asyncio.sleep(delay)

            try:
                raw = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
                nd = json.loads(raw)
                queries = nd["props"]["pageProps"]["dehydratedState"]["queries"]
                data = queries[0]["state"]["data"]
            except Exception as e:
                print(f"\n  ⚠  Could not parse __NEXT_DATA__ on page {page}: {e}")
                break

            results = data.get("results", [])
            if not results:
                print(f" (empty, done)", end="", flush=True)
                break

            # Detect recycled content: if every ID on this page was seen in a prior
            # page of this same area, the site is looping (pag= param being ignored).
            page_ids = {
                str(item.get("realEstate", {}).get("id", ""))
                for item in results
                if item.get("realEstate", {}).get("id")
            }
            if page > 1 and page_ids and page_ids.issubset(page_ids_seen):
                print(f" (repeat page, done)", end="", flush=True)
                break
            page_ids_seen.update(page_ids)

            # Client-side guard (belt-and-suspenders in case site leaks a listing through)
            for item in results:
                parsed = parse_listing(item, city_key, cfg["label"])
                if not parsed:
                    continue
                if parsed["id"] in seen_ids:
                    continue
                if extra_filters.get("max_price") and parsed["price"] > extra_filters["max_price"]:
                    continue
                if extra_filters.get("min_price") and parsed["price"] < extra_filters["min_price"]:
                    continue
                if extra_filters.get("min_sqm") and parsed["sqm"] < extra_filters["min_sqm"]:
                    continue
                if extra_filters.get("max_sqm") and parsed["sqm"] > extra_filters["max_sqm"]:
                    continue
                if extra_filters.get("min_rooms") and (parsed["rooms"] or 0) < extra_filters["min_rooms"]:
                    continue
                seen_ids.add(parsed["id"])
                all_items.append(parsed)

            site_max  = data.get("maxPages") or pages
            max_pages = min(pages, site_max)
            area_tag  = f"/{area_slug}" if area_slug else ""
            print(f" p{page}/{max_pages}{area_tag}", end="", flush=True)

            if page >= max_pages:
                break

    return all_items


def fetch_city(city_key: str, pages: int = 3, extra_filters: dict = None,
               delay: float = 2.5, area_slugs: list | None = None) -> list:
    """Fetch listings for one city using a real Edge browser via nodriver."""
    cfg = CITIES[city_key]
    print(f"  Fetching {cfg['label']}...", end="", flush=True)

    async def _run():
        browser = await uc.start(
            browser_executable_path=EDGE_PATH,
            headless=False,
            lang="it-IT",
        )
        try:
            items = await _fetch_city_async(
                city_key, pages, extra_filters or {}, delay, browser, area_slugs
            )
        finally:
            browser.stop()
        return items

    items = asyncio.run(_run())
    print(f"  → {len(items)} listings")
    return items


# ── Export ────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "id", "source", "city", "neighbourhood", "address", "price", "sqm", "ask_psqm",
    "rooms", "floor", "floor_n", "elevator", "is_external", "energy_class",
    "year_built", "bathrooms", "has_balcony", "has_parking", "heating_type",
    "furnished", "photo_count", "days_on_market", "condition", "thumbnail", "url",
    "fetched_at",
    "omi_zone", "omi_fascia", "omi_bench", "omi_bmin", "omi_bmax",
    "omi_rmin", "omi_rmax", "vs_omi_pct", "vs_omi_label",
    "omi_rent_raw", "surf_coeff", "cond_coeff",
    "est_rent_mo", "est_yield_pct",
    "fascia_pct", "fascia_label",
    "score_price", "score_yield", "score_fascia", "score_total",
    "title",
]


# ── Staleness tracking — shared seen_ids_sales.json ──────────────────────────
SEEN_IDS_SALES_PATH = Path(__file__).parent / "seen_ids_sales.json"

# Shared with fetch_rentals.py — see those constants for the rationale.
STALE_DAYS_THRESHOLD = 7
REMOVE_AFTER_DAYS    = 30


def _load_seen_sales() -> dict:
    if SEEN_IDS_SALES_PATH.exists():
        try:
            raw = json.loads(SEEN_IDS_SALES_PATH.read_text())
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_seen_sales(seen: dict) -> None:
    SEEN_IDS_SALES_PATH.write_text(json.dumps(seen, ensure_ascii=False))


def _stamp_and_apply_staleness(scored: list, latest_path: str) -> list:
    """
    1. Read existing sales_latest.json (if any). Listings from previous scans
       that didn't appear in this run keep their old last_seen_date.
    2. Stamp first_seen_date / last_seen_date onto every just-scanned listing.
    3. Merge new scan over existing entries (Idealista entries preserved).
    4. Mark stale (≥ STALE_DAYS_THRESHOLD days unseen) and trim (≥
       REMOVE_AFTER_DAYS days unseen).
    5. Persist the updated seen_ids_sales.json.

    Returns the merged listing list.
    """
    from datetime import date as _date, datetime as _dt
    today        = _date.today()
    today_str    = str(today)
    seen         = _load_seen_sales()
    new_ids      = {str(l["id"]) for l in scored if l.get("id") is not None}

    # Stamp first/last seen on every listing we just scored
    for l in scored:
        lid = str(l["id"])
        l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str
        l["last_seen_date"]  = today_str
        seen[lid] = {
            "first_seen_date": l["first_seen_date"],
            "last_seen_date":  today_str,
        }

    # Merge: pull in previously-known listings that didn't appear in this scan
    existing: list = []
    try:
        with open(latest_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception:
        pass

    # Decide which source(s) this scan covered. fetch_listings always emits
    # Immobiliare sales — Idealista has its own pipeline that runs separately.
    SCAN_SOURCE = "immobiliare_sale"
    kept = []
    for l in existing:
        lid = str(l.get("id", ""))
        if lid in new_ids:
            continue   # superseded by fresh entry
        if l.get("source") and l["source"] != SCAN_SOURCE:
            kept.append(l)   # different source — preserve untouched
            continue
        # Same source, NOT in this scan → kept (potentially stale).
        # last_seen_date is whatever was already on the listing dict; the
        # mark/trim pass below will flag it appropriately.
        kept.append(l)

    merged = kept + scored

    # Mark stale + trim too-old entries
    stale_count   = 0
    keep_after_trim: list = []
    for l in merged:
        last_seen = l.get("last_seen_date")
        if not last_seen:
            l["days_since_seen"] = None
            l["is_stale"]        = False
            keep_after_trim.append(l)
            continue
        try:
            ds = (today - _dt.fromisoformat(last_seen).date()).days
        except ValueError:
            l["days_since_seen"] = None
            l["is_stale"]        = False
            keep_after_trim.append(l)
            continue
        l["days_since_seen"] = ds
        if ds >= REMOVE_AFTER_DAYS:
            seen.pop(str(l.get("id", "")), None)
            continue   # drop entirely
        l["is_stale"] = ds >= STALE_DAYS_THRESHOLD
        if l["is_stale"]:
            stale_count += 1
        keep_after_trim.append(l)

    dropped = len(merged) - len(keep_after_trim)
    if dropped:
        print(f"  [trim]  removed {dropped} sales absent for {REMOVE_AFTER_DAYS}+ days")
    print(f"  [stale] {stale_count}/{len(keep_after_trim)} sales flagged stale "
          f"(absent {STALE_DAYS_THRESHOLD}+ days)")

    _save_seen_sales(seen)
    return keep_after_trim


def export(listings: list, prefix: str, city: str | None = None):
    """
    Export scored listings. `city` (if set) is used to derive the per-city
    dashboard mirror path (e.g. dashboard/milano_sales_latest.json).
    When city is None the legacy combined dashboard/sales_latest.json is
    used — back-compat for --all-cities runs.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = f"{prefix}_{ts}.csv"
    json_path = f"{prefix}_{ts}.json"

    # CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(listings)

    # Dedup real-world duplicates: same property listed by multiple agencies
    from collections import defaultdict as _dd
    geo_groups: dict = _dd(list)
    for i, l in enumerate(listings):
        lat, lon = l.get("latitude"), l.get("longitude")
        if lat is not None and lon is not None and l.get("rooms") and l.get("sqm") and l.get("price"):
            geo_groups[(round(lat, 3), round(lon, 3), l["rooms"], l["sqm"], l["price"])].append(i)
    remove_geo: set = set()
    for idxs in geo_groups.values():
        if len(idxs) > 1:
            idxs.sort(key=lambda i: (listings[i].get("first_seen_date") or "9999", str(listings[i].get("id", ""))))
            remove_geo.update(idxs[1:])
    if remove_geo:
        print(f"  [dedup]  Removed {len(remove_geo)} real-world duplicate listing(s)")
        listings = [l for i, l in enumerate(listings) if i not in remove_geo]

    # JSON (clean: drop internal `omi` dict, keep flat scored fields)
    json_listings = [{k: v for k, v in l.items() if k != "omi"} for l in listings]
    # Use compact JSON + null-strip so the snapshot stays under the 25 MiB
    # Cloudflare Pages per-file cap.
    from dashboard_io import write_snapshot
    write_snapshot(json_path, json_listings)

    # Mirror to dashboard/sales_latest.json. Now goes through the staleness
    # tracker which:
    #   - stamps first_seen_date + last_seen_date on every just-scored listing,
    #   - merges with the prior file (preserves Idealista entries + Immobiliare
    #     entries not in *this* scan so they have a chance to be re-seen),
    #   - marks 7-day-unseen entries as is_stale,
    #   - trims 30-day-unseen entries entirely.
    import os as _os
    dashboard_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard")
    _os.makedirs(dashboard_dir, exist_ok=True)
    latest_name = f"{city}_sales_latest.json" if city else "sales_latest.json"
    latest_path = _os.path.join(dashboard_dir, latest_name)
    merged = _stamp_and_apply_staleness(json_listings, latest_path)
    write_snapshot(latest_path, merged)

    return csv_path, json_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch Immobiliare.it listings and score against OMI benchmarks."
    )
    p.add_argument("--city",       choices=list(CITIES.keys()), help="Single city to fetch")
    p.add_argument("--cities",     nargs="+", choices=list(CITIES.keys()), help="One or more cities (e.g. --cities napoli milano)")
    p.add_argument("--all-cities", action="store_true",         help="Fetch all configured cities")
    p.add_argument("--pages",      type=int, default=3,         help="Pages per city (25 listings/page, default 3)")
    p.add_argument("--max-price",  type=int,                    help="Max listing price €")
    p.add_argument("--min-price",  type=int,                    help="Min listing price €")
    p.add_argument("--min-sqm",    type=int,                    help="Min surface m²")
    p.add_argument("--max-sqm",    type=int,                    help="Max surface m²")
    p.add_argument("--min-rooms",  type=int,                    help="Min number of rooms")
    p.add_argument("--areas",      help="Comma-separated area names to filter by (e.g. 'isola,navigli,brera')")
    p.add_argument("--delay",      type=float, default=2.5,     help="Seconds to wait after each page load (default 2.5)")
    p.add_argument("--output",     default="listings",          help="Output file prefix (default: listings)")
    return p.parse_args()


def _load_sale_prefs() -> dict:
    """Load sale_fetch_prefs.json written by the dashboard (localhost:8000)."""
    path = Path(__file__).parent / "sale_fetch_prefs.json"
    if path.exists():
        try:
            return json.load(open(path))
        except Exception:
            pass
    return {}


def main():
    args = parse_args()

    # Fall back to dashboard-persisted prefs when no CLI city flags are given
    _prefs = _load_sale_prefs() if not (args.all_cities or args.cities or args.city) else {}

    # Determine which cities to fetch
    if args.all_cities:
        cities = list(CITIES.keys())
    elif args.cities:
        cities = args.cities
    elif args.city:
        cities = [args.city]
    elif _prefs.get("cities"):
        cities = _prefs["cities"]
    else:
        cities = ["milano"]   # last-resort default

    # Pages: use prefs when arg is at its default value
    pages = args.pages
    if pages == 3 and _prefs.get("pages"):
        try:
            pages = int(_prefs["pages"])
        except (ValueError, TypeError):
            pass

    # Build client-side filter dict (applied after parsing each listing)
    extra = {}
    if args.max_price:          extra["max_price"] = args.max_price
    elif _prefs.get("maxPrice"): extra["max_price"] = int(_prefs["maxPrice"])
    if args.min_price:          extra["min_price"] = args.min_price
    elif _prefs.get("minPrice"): extra["min_price"] = int(_prefs["minPrice"])
    if args.min_sqm:            extra["min_sqm"]   = args.min_sqm
    elif _prefs.get("minSqm"):   extra["min_sqm"]   = int(_prefs["minSqm"])
    if args.max_sqm:            extra["max_sqm"]   = args.max_sqm
    elif _prefs.get("maxSqm"):   extra["max_sqm"]   = int(_prefs["maxSqm"])
    if args.min_rooms:          extra["min_rooms"] = args.min_rooms
    elif _prefs.get("minRooms"): extra["min_rooms"] = int(_prefs["minRooms"])

    # Parse --areas into URL slugs (CLI overrides prefs)
    area_slugs = None
    if args.areas:
        raw_areas = [a.strip() for a in args.areas.split(",") if a.strip()]
        area_slugs = [to_url_slug(a) for a in raw_areas]
    elif _prefs.get("areas"):
        area_slugs = [to_url_slug(a) for a in _prefs["areas"] if a.strip()]

    print(f"\n{'─'*52}")
    print(f"  Immobiliare Scorer — fetch run")
    print(f"  Cities : {', '.join(c.title() for c in cities)}")
    print(f"  Pages  : {pages} per city (~{pages*25} listings max)")
    if area_slugs:
        print(f"  Areas  : {', '.join(area_slugs)}")
    if extra:
        print(f"  Filters: {extra}")
    print(f"{'─'*52}\n")

    # Fetch all cities
    all_raw = []
    for city_key in cities:
        raw = fetch_city(city_key, pages=pages,
                         extra_filters=extra or None, delay=args.delay,
                         area_slugs=area_slugs)
        all_raw.extend(raw)

    if not all_raw:
        print("\n✗ No listings fetched. Check connectivity and city IDs.")
        sys.exit(1)

    print(f"\n  Total raw listings: {len(all_raw)}")

    # OMI polygon pass — fills omi_zona/fascia/loc_mid/compr_mid for any
    # listing with coordinates but no OMI yet. Critical for non-Milan
    # cities: match_omi() above uses Milan-specific keyword tables, so
    # Roma / Napoli / La Maddalena sales would otherwise have 0% OMI
    # coverage. Mirrors the polygon-application step in fetch_rentals.
    try:
        import omi_lookup as _omi_lookup
        _omi_hits = 0
        for _l in all_raw:
            if _l.get("omi_zona"):
                continue
            _lat = _l.get("latitude")
            _lng = _l.get("longitude")
            if _lat is None or _lng is None:
                continue
            _city = _l.get("city") or _l.get("city_key") or "milano"
            try:
                _zone, _src = _omi_lookup.lookup_for_city(
                    float(_lat), float(_lng), city=_city,
                )
            except Exception:
                _zone, _src = None, "failed"
            if _zone:
                _l["omi_zona"]      = _zone.get("zona")
                _l["omi_fascia"]    = _zone.get("fascia")
                _l["omi_descr"]     = _zone.get("descr")
                _l["omi_loc_min"]   = _zone.get("loc_min")
                _l["omi_loc_max"]   = _zone.get("loc_max")
                _l["omi_loc_mid"]   = _zone.get("loc_mid")
                _l["omi_compr_min"] = _zone.get("compr_min")
                _l["omi_compr_max"] = _zone.get("compr_max")
                _l["omi_compr_mid"] = _zone.get("compr_mid")
                _l["omi_source"]    = _src
                _omi_hits += 1
        if _omi_hits:
            print(f"  [omi]  polygon fields applied to {_omi_hits} listings")
    except ImportError:
        pass
    except Exception as _exc:
        print(f"  [omi]  polygon pass skipped: {_exc}")

    # Score (needs full list for within-fascia percentile)
    scored = []
    for l in all_raw:
        scores = score_listing(l, all_raw)
        scored.append({**l, **scores})

    # Sort by score descending
    scored.sort(key=lambda x: x.get("score_total", 0), reverse=True)

    # Export — pass the city for per-city dashboard mirror filename when
    # the run scanned a single city (matrix workflow path). Multi-city
    # runs fall back to legacy combined sales_latest.json.
    _single_city = cities[0] if len(cities) == 1 else None
    csv_path, json_path = export(scored, args.output, city=_single_city)

    # Summary
    top5 = scored[:5]
    print(f"\n{'─'*52}")
    print(f"  ✓ Exported {len(scored)} listings")
    print(f"    CSV  → {csv_path}")
    print(f"    JSON → {json_path}")
    print(f"\n  Top 5 by score:")
    for i, l in enumerate(top5, 1):
        yield_str = f"{l.get('est_yield_pct','?')}%" if l.get('est_yield_pct') else "n/a"
        print(
            f"  {i}. [{l.get('score_total','?'):>3}] "
            f"{l['city']:<8} {l['neighbourhood']:<22} "
            f"€{l['price']:>9,}  {l['sqm']:>4}m²  "
            f"yield {yield_str:<6}  {l.get('vs_omi_label','')}"
        )
    print(f"{'─'*52}\n")
    print("  Dashboard → python serve.py  →  http://localhost:8000/")
    print("  dashboard/latest.json updated automatically each run.")
    print("  Or open the CSV in Excel to filter and sort.\n")


if __name__ == "__main__":
    main()
