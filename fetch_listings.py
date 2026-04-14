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

import argparse
import asyncio
import csv
import json
import re as _re
import sys
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import nodriver as uc

EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

# ── City config ───────────────────────────────────────────────────────────────
# url_slug: the /vendita-case/{slug}/ URL segment
CITIES = {
    "napoli":  {"label": "Napoli",  "url_slug": "napoli"},
    "milano":  {"label": "Milano",  "url_slug": "milano"},
    "roma":    {"label": "Roma",    "url_slug": "roma"},
    "torino":  {"label": "Torino",  "url_slug": "torino"},
    "firenze": {"label": "Firenze", "url_slug": "firenze"},
    "bologna": {"label": "Bologna", "url_slug": "bologna"},
    "palermo": {"label": "Palermo", "url_slug": "palermo"},
    "bari":    {"label": "Bari",    "url_slug": "bari"},
    "catania": {"label": "Catania", "url_slug": "catania"},
    "verona":  {"label": "Verona",  "url_slug": "verona"},
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


# ── Immobiliare.it API fetch ───────────────────────────────────────────────────

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

    elevator = prop.get("elevator") or prop.get("hasElevator")

    # condition
    condition_raw = (
        prop.get("ga4Condition")
        or prop.get("condition")
        or re_data.get("typology", {}).get("name", "")
    )

    listing_id = re_data.get("id", "")
    url = f"https://www.immobiliare.it/annunci/{listing_id}/" if listing_id else ""

    omi = match_omi(city_key, neighbourhood)

    return {
        "id":            str(listing_id),
        "city":          city_label,
        "city_key":      city_key,
        "title":         re_data.get("title", ""),
        "neighbourhood": neighbourhood,
        "address":       address,
        "latitude":      latitude,
        "longitude":     longitude,
        "price":         price,
        "sqm":           sqm,
        "ask_psqm":      ask_psqm,
        "rooms":         rooms,
        "floor":         floor,
        "elevator":      elevator,
        "condition":     condition_raw,
        "url":           url,
        "omi":           omi,   # temporary – removed before CSV export
    }


async def _fetch_city_async(city_key: str, pages: int, extra_filters: dict,
                             delay: float, browser) -> list:
    """Async inner: navigate pages for one city and extract __NEXT_DATA__."""
    cfg = CITIES[city_key]
    slug = cfg["url_slug"]
    all_items = []
    max_pages = pages

    page_ids_seen = set()   # IDs seen in previous pages — detects recycled content

    for page in range(1, pages + 1):
        # Embed filters directly in the URL — the site pre-filters server-side,
        # so we get fewer results per page and need fewer pages overall.
        params = {}
        if extra_filters.get("max_price"): params["prezzoMassimo"]    = extra_filters["max_price"]
        if extra_filters.get("min_price"): params["prezzoMinimo"]     = extra_filters["min_price"]
        if extra_filters.get("min_sqm"):   params["superficieMinima"] = extra_filters["min_sqm"]
        if extra_filters.get("max_sqm"):   params["superficieMassima"]= extra_filters["max_sqm"]
        if extra_filters.get("min_rooms"): params["localiMinimo"]     = extra_filters["min_rooms"]
        if page > 1:                       params["pag"]              = page

        url = f"https://www.immobiliare.it/vendita-case/{slug}/"
        if params:
            url += "?" + urlencode(params)

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

        # Detect recycled content: if every ID on this page was seen in a prior page,
        # the site is serving page 1 again (pag= param being ignored).
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
            all_items.append(parsed)

        site_max  = data.get("maxPages") or pages
        max_pages = min(pages, site_max)
        print(f" p{page}/{max_pages}", end="", flush=True)

        if page >= max_pages:
            break

    return all_items


def fetch_city(city_key: str, pages: int = 3, extra_filters: dict = None,
               delay: float = 2.5) -> list:
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
                city_key, pages, extra_filters or {}, delay, browser
            )
        finally:
            browser.stop()
        return items

    items = asyncio.run(_run())
    print(f"  → {len(items)} listings")
    return items


# ── Export ────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "id", "city", "neighbourhood", "address", "price", "sqm", "ask_psqm",
    "rooms", "floor", "elevator", "condition", "url",
    "omi_zone", "omi_fascia", "omi_bench", "omi_bmin", "omi_bmax",
    "omi_rmin", "omi_rmax", "vs_omi_pct", "vs_omi_label",
    "omi_rent_raw", "surf_coeff", "cond_coeff",
    "est_rent_mo", "est_yield_pct",
    "fascia_pct", "fascia_label",
    "score_price", "score_yield", "score_fascia", "score_total",
    "title",
]


def export(listings: list, prefix: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = f"{prefix}_{ts}.csv"
    json_path = f"{prefix}_{ts}.json"

    # CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(listings)

    # JSON (clean: drop internal `omi` dict, keep flat scored fields)
    json_listings = [{k: v for k, v in l.items() if k != "omi"} for l in listings]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_listings, f, ensure_ascii=False, indent=2)

    # Always mirror to dashboard/latest.json so the HTML dashboard auto-refreshes
    import os as _os
    dashboard_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard")
    _os.makedirs(dashboard_dir, exist_ok=True)
    latest_path = _os.path.join(dashboard_dir, "latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(json_listings, f, ensure_ascii=False, indent=2)

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
    p.add_argument("--delay",      type=float, default=2.5,     help="Seconds to wait after each page load (default 2.5)")
    p.add_argument("--output",     default="listings",          help="Output file prefix (default: listings)")
    return p.parse_args()


def main():
    args = parse_args()

    # Determine which cities to fetch
    if args.all_cities:
        cities = list(CITIES.keys())
    elif args.cities:
        cities = args.cities
    elif args.city:
        cities = [args.city]
    else:
        cities = ["napoli", "milano"]   # default

    # Build client-side filter dict (applied after parsing each listing)
    extra = {}
    if args.max_price: extra["max_price"] = args.max_price
    if args.min_price: extra["min_price"] = args.min_price
    if args.min_sqm:   extra["min_sqm"]   = args.min_sqm
    if args.max_sqm:   extra["max_sqm"]   = args.max_sqm
    if args.min_rooms: extra["min_rooms"] = args.min_rooms

    print(f"\n{'─'*52}")
    print(f"  Immobiliare Scorer — fetch run")
    print(f"  Cities : {', '.join(c.title() for c in cities)}")
    print(f"  Pages  : {args.pages} per city (~{args.pages*25} listings max)")
    if extra:
        print(f"  Filters: {extra}")
    print(f"{'─'*52}\n")

    # Fetch all cities
    all_raw = []
    for city_key in cities:
        raw = fetch_city(city_key, pages=args.pages,
                         extra_filters=extra or None, delay=args.delay)
        all_raw.extend(raw)

    if not all_raw:
        print("\n✗ No listings fetched. Check connectivity and city IDs.")
        sys.exit(1)

    print(f"\n  Total raw listings: {len(all_raw)}")

    # Score (needs full list for within-fascia percentile)
    scored = []
    for l in all_raw:
        scores = score_listing(l, all_raw)
        scored.append({**l, **scores})

    # Sort by score descending
    scored.sort(key=lambda x: x.get("score_total", 0), reverse=True)

    # Export
    csv_path, json_path = export(scored, args.output)

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
