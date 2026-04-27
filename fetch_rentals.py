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
import os
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
SEEN_IDS_PATH        = BASE_DIR / "seen_ids.json"
STATUS_PATH          = BASE_DIR / "scanner_status.json"
DIGEST_SENT_PATH     = BASE_DIR / ".digest_sent_date"
OUTPUT_PATH          = DASHBOARD_DIR / "rentals_latest.json"
CUSTOM_MAPPINGS_PATH = BASE_DIR / "custom_omi_mappings.json"
AREA_SETTINGS_PATH   = BASE_DIR / "area_settings.json"

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


def _load_active_areas() -> list:
    """Return display names of active areas from area_settings.json (new format).
    Falls back to an empty list so the scanner fetches all of Milano."""
    if AREA_SETTINGS_PATH.exists():
        try:
            data = json.loads(AREA_SETTINGS_PATH.read_text())
            if isinstance(data, dict) and "areas" in data:
                return [a["name"] for a in data["areas"]
                        if isinstance(a, dict) and a.get("active") and a.get("name")]
        except Exception:
            pass
    return []
CITY_KEY   = "milano"
CITY_LABEL = "Milano"
CITY_SLUG  = "milano"

DAEMON_INTERVAL_SEC = 60 * 60   # 60 minutes
DIGEST_HOUR         = 8          # send daily digest at 08:xx local time

# ── OMI rent data (Milano, 2° sem 2025) ────────────────────────────────────────
# rmin/rmax: €/m²/MONTH  –  source: Agenzia delle Entrate, published 16/03/2026
# Values are "Abitazioni civili, stato conservativo NORMALE (più frequente di zona)"
# fetched from https://www1.agenziaentrate.gov.it/servizi/Consultazione/risultato.php
#
# OMI fascia mapping (internal A/B/C ≠ official OMI B/C/D/E):
#   A = OMI fascia B/C (Centrale / Semicentrale premium)
#   B = OMI fascia C/D (Semicentrale outer / Periferica)
#   C = OMI fascia D/E (Periferica outer / Suburbana)
OMI_RENT = {
    # ── Fascia A – Centro storico / Semicentro premium ─────────────────────
    "brera":            dict(fascia="A", rmin=26.0, rmax=35.0),   # B15
    "duomo":            dict(fascia="A", rmin=25.0, rmax=35.0),   # B12
    "centro":           dict(fascia="A", rmin=25.0, rmax=35.0),   # B12
    "porta venezia":    dict(fascia="A", rmin=16.0, rmax=23.5),   # C12
    "buenos aires":     dict(fascia="A", rmin=16.0, rmax=23.5),   # C12
    "isola":            dict(fascia="A", rmin=16.0, rmax=24.0),   # C14 Porta Nuova
    "navigli":          dict(fascia="A", rmin=15.0, rmax=22.0),   # B21
    "porta ticinese":   dict(fascia="A", rmin=15.0, rmax=22.0),   # B21
    "tortona":          dict(fascia="A", rmin=15.0, rmax=20.0),   # C18 Solari/Porta Genova
    "porta romana":     dict(fascia="A", rmin=17.0, rmax=22.0),   # B20
    "moscova":          dict(fascia="A", rmin=21.5, rmax=29.0),   # B18
    "garibaldi":        dict(fascia="A", rmin=16.0, rmax=24.0),   # C14 Porta Nuova
    "corso como":       dict(fascia="A", rmin=16.0, rmax=24.0),   # C14 Porta Nuova
    "arco della pace":  dict(fascia="A", rmin=17.5, rmax=26.5),   # B17
    "guastalla":        dict(fascia="A", rmin=19.0, rmax=26.0),   # B13 Università/S.Lorenzo
    "ticinese":         dict(fascia="A", rmin=15.0, rmax=22.0),   # B21
    "corso genova":     dict(fascia="A", rmin=15.0, rmax=22.0),   # B21
    "indipendenza":     dict(fascia="A", rmin=16.0, rmax=23.5),   # C12 Buenos Aires area
    "piave":            dict(fascia="A", rmin=16.0, rmax=23.5),   # C12 Buenos Aires area
    "tricolore":        dict(fascia="A", rmin=16.0, rmax=23.5),   # C12 Porta Venezia area
    "vincenzo monti":   dict(fascia="A", rmin=16.0, rmax=18.5),   # C17 Washington/Pagano
    "washington":       dict(fascia="A", rmin=16.0, rmax=18.5),   # C17
    "solari":           dict(fascia="A", rmin=15.0, rmax=20.0),   # C18 Solari/Porta Genova
    "paolo sarpi":      dict(fascia="A", rmin=13.5, rmax=18.0),   # C16 Cenisio/Sarpi
    "centrale":         dict(fascia="A", rmin=13.5, rmax=19.0),   # C15 Stazione Centrale
    "amendola":         dict(fascia="A", rmin=16.0, rmax=18.5),   # C17 Washington/Pagano
    "buonarroti":       dict(fascia="A", rmin=16.0, rmax=18.5),   # C17 Washington/Pagano
    "melchiorre":       dict(fascia="A", rmin=13.5, rmax=19.0),   # C15 Stazione Centrale
    # ── Fascia B – Semicentro / Prima periferia ────────────────────────────
    "città studi":      dict(fascia="B", rmin=13.5, rmax=21.0),   # C19 Sarfatti/Crema area
    "citta studi":      dict(fascia="B", rmin=13.5, rmax=21.0),   # C19
    "lambrate":         dict(fascia="B", rmin=12.0, rmax=17.0),   # D13
    "loreto":           dict(fascia="B", rmin=12.0, rmax=15.5),   # D12 Piola/Argonne
    "medaglie d'oro":   dict(fascia="B", rmin=12.0, rmax=15.5),   # D12
    "bovisa":           dict(fascia="B", rmin=10.5, rmax=14.5),   # D31
    "dergano":          dict(fascia="B", rmin=9.5,  rmax=12.0),   # D32 Affori/Comasina area
    "affori":           dict(fascia="B", rmin=9.5,  rmax=12.0),   # D32
    "niguarda":         dict(fascia="B", rmin=11.0, rmax=14.5),   # D33
    "bicocca":          dict(fascia="B", rmin=11.0, rmax=15.0),   # D34 Sarca/Bicocca
    "via padova":       dict(fascia="B", rmin=11.0, rmax=15.5),   # D10 Feltre/Udine area
    "feltre":           dict(fascia="B", rmin=11.0, rmax=15.5),   # D10
    "cimiano":          dict(fascia="B", rmin=11.0, rmax=15.5),   # D10
    "san siro":         dict(fascia="B", rmin=12.5, rmax=16.0),   # D24 Segesta/San Siro area
    "montenero":        dict(fascia="B", rmin=19.0, rmax=27.0),   # B19 Porta Vittoria
    "argonne":          dict(fascia="B", rmin=12.0, rmax=15.5),   # D12 Piola/Argonne
    "corsica":          dict(fascia="B", rmin=12.0, rmax=15.5),   # D12
    "maggiolina":       dict(fascia="B", rmin=9.5,  rmax=14.0),   # D36 Maggiolina/Trotter
    "monte rosa":       dict(fascia="B", rmin=11.5, rmax=15.5),   # D28 Ippodromo/Monte Stella
    "lotto":            dict(fascia="B", rmin=11.5, rmax=15.5),   # D28
    "casoretto":        dict(fascia="B", rmin=11.0, rmax=15.5),   # D10
    "precotto":         dict(fascia="B", rmin=11.0, rmax=15.5),   # D10
    "rovereto":         dict(fascia="B", rmin=9.5,  rmax=14.0),   # D36
    "turro":            dict(fascia="B", rmin=9.5,  rmax=14.0),   # D36
    "parco trotter":    dict(fascia="B", rmin=9.5,  rmax=14.0),   # D36
    "pasteur":          dict(fascia="B", rmin=9.5,  rmax=14.0),   # D36
    "udine":            dict(fascia="B", rmin=11.0, rmax=15.5),   # D10
    "ghisolfa":         dict(fascia="B", rmin=10.5, rmax=14.5),   # D31 Bovisa area
    "cenisio":          dict(fascia="B", rmin=13.5, rmax=18.0),   # C16 Cenisio/Farini/Sarpi
    "plebisciti":       dict(fascia="B", rmin=12.0, rmax=15.5),   # D12
    "pezzotti":         dict(fascia="B", rmin=10.5, rmax=15.0),   # D20 Ortles/Bazzi
    "ca granda":        dict(fascia="B", rmin=11.0, rmax=15.0),   # D34 Sarca/Bicocca
    "ca' granda":       dict(fascia="B", rmin=11.0, rmax=15.0),   # D34
    "tre castelli":     dict(fascia="B", rmin=11.0, rmax=15.0),   # D34
    "villa san giovanni": dict(fascia="B", rmin=9.0, rmax=14.0),  # D35 Crescenzago/Gorla
    "siena":            dict(fascia="B", rmin=9.0,  rmax=14.0),   # D35
    # ── Fascia C – Periferia / Suburbana ───────────────────────────────────
    "famagosta":        dict(fascia="C", rmin=10.5, rmax=15.0),   # D21 Barona/Famagosta
    "lorenteggio":      dict(fascia="C", rmin=10.5, rmax=14.0),   # D25
    "giambellino":      dict(fascia="C", rmin=10.5, rmax=14.0),   # D25
    "rogoredo":         dict(fascia="C", rmin=11.0, rmax=14.0),   # D38 Santa Giulia/Rogoredo
    "mecenate":         dict(fascia="C", rmin=12.0, rmax=15.0),   # D37 Forlanini/Mecenate
    "forlanini":        dict(fascia="C", rmin=12.0, rmax=15.0),   # D37
    "quarto oggiaro":   dict(fascia="C", rmin=7.5,  rmax=10.0),   # E8
    "comasina":         dict(fascia="C", rmin=9.5,  rmax=12.0),   # D32
    "bruzzano":         dict(fascia="C", rmin=9.5,  rmax=12.0),   # D32
    "romolo":           dict(fascia="C", rmin=11.0, rmax=15.0),   # D18 Vigentino/Chiesa Rossa
    "bonola":           dict(fascia="C", rmin=10.0, rmax=14.0),   # E6 Gallaratese/Bonola
    "trenno":           dict(fascia="C", rmin=10.0, rmax=14.0),   # E6
    "vigentino":        dict(fascia="C", rmin=11.0, rmax=15.0),   # D18
    "corvetto":         dict(fascia="C", rmin=11.0, rmax=15.0),   # D16 Ortomercato area
    "ripamonti":        dict(fascia="C", rmin=11.0, rmax=15.0),   # D16
    "gratosoglio":      dict(fascia="C", rmin=6.5,  rmax=10.0),   # E7 Missaglia/Gratosoglio
    "bisceglie":        dict(fascia="C", rmin=10.5, rmax=14.0),   # D25
    "certosa":          dict(fascia="C", rmin=10.0, rmax=14.5),   # D40 Musocco/Certosa
    "uptown":           dict(fascia="C", rmin=11.0, rmax=14.0),   # D38 Santa Giulia area
    "cascina merlata":  dict(fascia="C", rmin=14.0, rmax=21.0),   # D39 (premium new dev)
    "quartiere adriano": dict(fascia="C", rmin=9.0, rmax=14.0),   # D35 Crescenzago/Adriano
    "adriano":          dict(fascia="C", rmin=9.0,  rmax=14.0),   # D35
    "musocco":          dict(fascia="C", rmin=10.0, rmax=14.5),   # D40
    "cermenate":        dict(fascia="C", rmin=11.0, rmax=15.0),   # D16
    "abbiategrasso":    dict(fascia="C", rmin=10.5, rmax=14.0),   # D25
}

FALLBACK = dict(fascia="B", rmin=11.0, rmax=15.0)


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

_custom_map_cache:      Optional[dict] = None
_custom_map_mtime:      float          = 0.0


def _load_custom_mappings() -> dict:
    """Return the custom OMI mappings dict, reloading from disk when the file changes."""
    global _custom_map_cache, _custom_map_mtime
    p = AREA_SETTINGS_PATH if AREA_SETTINGS_PATH.exists() else CUSTOM_MAPPINGS_PATH
    if p.exists():
        mtime = p.stat().st_mtime
        if _custom_map_cache is None or mtime > _custom_map_mtime:
            try:
                with open(p) as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get("mappings"), dict):
                    # Legacy area_settings.json format with explicit OMI zone overrides.
                    mappings = {}
                    for nb, entry in raw["mappings"].items():
                        if isinstance(entry, dict):
                            mappings[nb] = entry
                    _custom_map_cache = mappings
                elif isinstance(raw, dict) and "areas" in raw:
                    # New simplified format — no custom OMI overrides; use built-in lookup.
                    _custom_map_cache = {}
                else:
                    _custom_map_cache = raw if isinstance(raw, dict) else {}
                _custom_map_mtime = mtime
            except Exception:
                _custom_map_cache = {}
    return _custom_map_cache or {}


def match_omi(neighbourhood: str) -> dict:
    """
    Match neighbourhood to the most specific OMI zone (longest keyword wins).

    Lookup order:
      1. custom_omi_mappings.json  — user-assigned mappings (exact lower-case match)
      2. OMI_RENT keyword search   — built-in dict (substring match, longest wins)
      3. FALLBACK                  — city average
    """
    nb = neighbourhood.lower().strip()

    # 1. Custom mappings (exact match on the raw neighbourhood string)
    custom = _load_custom_mappings()
    if nb in custom:
        entry = custom[nb]
        return {
            "zone":   entry.get("zone_code", nb),
            "fascia": entry["fascia"],
            "rmin":   entry["rmin"],
            "rmax":   entry["rmax"],
        }

    # 2. Built-in OMI_RENT keyword search
    best_key, best_len = None, 0
    for keyword, data in OMI_RENT.items():
        if nb == keyword or keyword in nb:
            if len(keyword) > best_len:
                best_key, best_len = keyword, len(keyword)
    if best_key:
        return {"zone": best_key, **OMI_RENT[best_key]}

    # 3. Fallback
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

    # Rooms — try numeric field first, then parse from Italian title
    rooms_raw = prop.get("rooms") or prop.get("roomsNumber") or prop.get("numRooms")
    try:
        rooms = int(rooms_raw) if rooms_raw is not None else None
    except (TypeError, ValueError):
        rooms = None
    if rooms is None:
        _ROOM_NAMES = {
            "monolocale": 1, "bilocale": 2, "trilocale": 3,
            "quadrilocale": 4, "pentalocale": 5, "esalocale": 6,
        }
        title_lower = str(re_data.get("title", "")).lower()
        for word, n in _ROOM_NAMES.items():
            if word in title_lower:
                rooms = n
                break
        if rooms is None:
            m = _re.search(r'(\d)\s*local[ei]', title_lower)
            if m:
                rooms = int(m.group(1))

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

    # ── New enriched fields ────────────────────────────────────────────────────
    # floor_n + floor_label: parsed from raw floor field
    floor_n, floor_label = parse_floor(floor_raw)
    is_below_ground = floor_n is not None and floor_n < 0
    is_ground_floor = floor_n == 0

    # elevator: normalise to bool
    elevator_raw = prop.get("elevator") or prop.get("hasElevator")
    elevator_bool = None
    if elevator_raw is not None:
        elevator_bool = bool(elevator_raw) if not isinstance(elevator_raw, str) else elevator_raw.lower() not in ("false", "no", "0", "")

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

    # bathrooms
    baths_raw = prop.get("bathrooms") or prop.get("bathRooms")
    bathrooms = None
    if baths_raw is not None:
        try:
            bathrooms = int(baths_raw)
        except (TypeError, ValueError):
            pass

    # ── features[] array  ─────────────────────────────────────────────────────
    # Immobiliare.it sometimes returns a features list:
    #   [{"type": "balcony", "label": "Balcone"}, {"type": "parking", ...}, ...]
    # Collect feature type strings into a set for quick lookup.
    _feature_types: set = set()
    for feat in (prop.get("features") or re_data.get("features") or []):
        ft = feat.get("type") or feat.get("name") or ""
        _feature_types.add(str(ft).lower())
        # Also check label for Italian keywords
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
        has_balcony = bool({"balcony", "terrace", "garden", "terr", "balcon", "terrazza", "giardino"} & _feature_types)

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

    # heating_type: also check features
    if heating_type is None and _feature_types:
        if {"autonomo", "autonom"} & _feature_types:
            heating_type = "autonomous"
        elif {"centralizzato", "central"} & _feature_types:
            heating_type = "centralised"

    # furnished
    furn_raw = (prop.get("furnished") or prop.get("isFurnished") or prop.get("arredato")
                or re_data.get("furnished"))
    furnished = None
    if furn_raw is not None:
        if isinstance(furn_raw, bool):
            furnished = furn_raw
        elif isinstance(furn_raw, str):
            furnished = furn_raw.lower() in ("true", "yes", "arredato", "1", "si", "sì")
    if furnished is None and _feature_types:
        if "arredato" in _feature_types:
            furnished = True

    # photo_count — count photos; fall back to 1 if thumbnail was resolved but list missing
    photo_count = len(photos)

    # days_on_market (from first_seen or publication date if available)
    days_on_market = None
    for date_field in ("firstSeenDate", "publicationDate", "insertionDate", "created_at",
                       "createdAt", "insertDate", "datePublished"):
        raw_date = re_data.get(date_field) or prop.get(date_field)
        if raw_date:
            try:
                from datetime import date as _date
                pub = datetime.fromisoformat(str(raw_date)[:10]).date()
                days_on_market = (_date.today() - pub).days
                break
            except Exception:
                pass

    return {
        "id":                 listing_id,
        "source":             "immobiliare",
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
        "floor_n":            floor_n,
        "floor_label":        floor_label,
        "is_below_ground":    is_below_ground,
        "is_ground_floor":    is_ground_floor,
        "elevator":           elevator_bool,
        "is_external":        is_external,
        "energy_class":       energy_class,
        "year_built":         year_built,
        "bathrooms":          bathrooms,
        "has_balcony":        has_balcony,
        "has_parking":        has_parking,
        "heating_type":       heating_type,
        "furnished":          furnished,
        "photo_count":        photo_count,
        "days_on_market":     days_on_market,
        "condition":          condition_raw,
        "thumbnail":          thumbnail,    # first listing photo URL (or None)
        "url":                url,
        "omi":                omi,           # dropped before JSON export
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
    }


# ── Fetch (nodriver) ───────────────────────────────────────────────────────────

async def _fetch_async(pages: int, area_slugs: list, max_rent: int,
                       min_sqm: int, max_sqm: int, min_rooms: int,
                       delay: float, browser) -> tuple:
    """
    Navigate Immobiliare.it rental pages and return (listings, skipped_areas).

    skipped_areas: list of {"name": slug, "reason": str} for any area that
    produced no results or threw an exception. A single area failing never
    stops the scan — it is logged and skipped.

    Filters (max_rent, min_sqm, max_sqm) are embedded directly in the URL so
    the site pre-filters results — fewer pages needed.

    If area_slugs is non-empty, one URL series is fetched per area slug
    (e.g. /affitto-case/milano/navigli/?prezzoMassimo=2000).
    Results are deduplicated by listing ID.
    """
    all_items     = []
    seen_ids      = set()   # global dedup across all areas
    skipped_areas = []      # {"name", "reason"} for areas that failed or returned nothing

    # None sentinel = no area path → fetch whole city
    targets = area_slugs if area_slugs else [None]

    for area_slug in targets:
        label = area_slug or "all Milano"
        print(f"\n    area: {label}", end="", flush=True)

        try:
            area_ids_seen    = set()   # IDs seen so far within THIS area's pages
            # canonical_slug: resolved by the site on the first request (may differ
            # from area_slug when the site redirects e.g. porta-romana →
            # porta-romana-medaglie-d-oro, which drops pag= from the URL).
            # We use this resolved slug for pages 2, 3, … so pagination works.
            canonical_slug   = area_slug
            area_got_results = False

            for page in range(1, pages + 1):
                url = build_rental_url(page, canonical_slug, max_rent, min_sqm, max_sqm, min_rooms)
                print(f"\n    [debug] GET {url}", flush=True)
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
                    print(f"\n    [warn] __NEXT_DATA__ parse failed (area={label}, page={page}): {e}")
                    break

                results = data.get("results", [])
                if not results:
                    print(f" (empty, done)", end="", flush=True)
                    break

                area_got_results = True

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
                    # Tag with the resolved area slug so write_output can remove
                    # stale listings from this area that don't appear in the new scan.
                    parsed["_fetched_area"] = canonical_slug
                    seen_ids.add(parsed["id"])
                    all_items.append(parsed)
                    new_this_page += 1

                # maxPages: guard against None or 0 coming back from the site
                site_max = data.get("maxPages") or pages
                max_pg   = min(pages, site_max)
                print(f" p{page}/{max_pg}(+{new_this_page})", end="", flush=True)
                if page >= max_pg:
                    break

            # Flag areas that returned nothing across all pages
            if not area_got_results and area_slug:
                reason = "no results — slug may be invalid or area not found on Immobiliare.it"
                skipped_areas.append({"name": area_slug, "reason": reason})
                print(f"\n    [warn] No results for area '{label}' — {reason}", flush=True)

        except Exception as exc:
            # One area failing must never stop the rest of the scan
            reason = f"{type(exc).__name__}: {exc}"
            skipped_areas.append({"name": area_slug or "all Milano", "reason": reason})
            print(f"\n    [error] Area '{label}' failed: {reason} — skipping", flush=True)
            continue

    return all_items, skipped_areas


def fetch_rentals(pages: int = 3, area_names: list = None, max_rent: int = 0,
                  min_sqm: int = 0, max_sqm: int = 0, min_rooms: int = 0,
                  delay: float = 2.5) -> tuple:
    """
    One-shot fetch of Milano rental listings using Edge via nodriver.
    area_names: list of display names, e.g. ["Navigli", "Brera"].
                Each is converted to a URL slug and fetched as a separate URL series.
    Returns (listings, skipped_areas) — skipped_areas is a list of
    {"name": slug, "reason": str} for any area that was skipped.
    """
    # ── Fix 2: validate area slugs before scanning ─────────────────────────────
    pre_skipped: list = []
    valid_slugs: list = []
    for name in (area_names or []):
        name = name.strip()
        if not name:
            continue
        slug = to_url_slug(name)
        if not slug:
            pre_skipped.append({"name": name, "reason": "converts to empty URL slug"})
            print(f"  [warn] Area '{name}' has no valid Immobiliare URL slug — skipped.")
        else:
            valid_slugs.append(slug)

    if area_names and valid_slugs:
        n_valid   = len(valid_slugs)
        n_skipped = len(pre_skipped)
        desc = ", ".join(valid_slugs)
        print(f"  [scan] {len(area_names)} area(s) requested · "
              f"{n_valid} valid · {n_skipped} skipped (bad slug)")
        if pre_skipped:
            print(f"  [scan] Skipped: {', '.join(s['name'] for s in pre_skipped)}")
    else:
        desc = "all Milano"

    area_slugs = valid_slugs
    print(f"  Fetching rentals ({desc})...", end="", flush=True)

    async def _run():
        browser = await uc.start(
            browser_executable_path=EDGE_PATH,
            headless=False,
            lang="it-IT",
        )
        try:
            items, skipped = await _fetch_async(
                pages, area_slugs, max_rent, min_sqm, max_sqm, min_rooms, delay, browser
            )
        finally:
            browser.stop()
        return items, skipped

    t0 = time.time()
    items, fetch_skipped = asyncio.run(_run())
    skipped_areas = pre_skipped + fetch_skipped
    n_fetched = len(items)
    print(f"\n  [fetch]  Milano: {n_fetched} listings fetched ({pages} pages)")

    # ── Cache-aware geo enrichment ─────────────────────────────────────────
    # Inline enrichment is intentionally capped: the background geo-enrichment
    # process in api.py (triggered from the dashboard) is designed to handle
    # large batches with incremental saves.  Enriching thousands of listings
    # inline would block write_output for hours and risk losing all fetched data
    # if the process is killed.  Only enrich inline for small incremental batches
    # (i.e. normal daemon cadence), and let the dashboard button handle the rest.
    INLINE_ENRICH_CAP = 50

    try:
        import enrichment_cache as _ecache
        from enrich_geo import enrich_batch as _enrich_batch

        _ecache.load()

        new_items  = [l for l in items if _ecache.get("immobiliare", l["id"]) is None]
        n_cached   = n_fetched - len(new_items)
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
                        [("immobiliare", l["id"], g) for l, g in zip(new_items, geo_results)]
                    )
                except Exception as _geo_exc:
                    print(f"  [enrich] Geo enrichment failed ({type(_geo_exc).__name__}): {_geo_exc}"
                          f" — saving listings without geo data", flush=True)

        # Merge cached geo into every listing
        for listing in items:
            cached = _ecache.get("immobiliare", listing["id"])
            if cached:
                listing.update({k: v for k, v in cached.items() if k != "enriched_at"})

        # Apply OMI polygon fields to any listing still missing them.
        # This is pure in-memory (no network) and takes < 1 s for 1 000+ listings.
        # It covers: (a) cached listings whose cache entry predates omi_lookup,
        #            (b) new listings whose enrich_geo call failed the omi step.
        try:
            import omi_lookup as _omi_lookup
            if _omi_lookup.ZONES:
                _need_omi = [l for l in items if l.get("omi_loc_mid") is None
                             and l.get("latitude") and l.get("longitude")]
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
                        _omi_updates.append(("immobiliare", _l["id"],
                                             {**(_ecache.get("immobiliare", _l["id"]) or {}),
                                              **_omi_f}))
                if _omi_updates:
                    _ecache.bulk_save(_omi_updates)
                    print(f"  [omi]    polygon fields applied to {len(_omi_updates)} listings")
        except Exception as _omi_exc:
            print(f"  [omi]    polygon step skipped: {_omi_exc}", file=sys.stderr)

        print(f"  [merge]  {n_fetched} listings enriched and ready")

    except ImportError:
        pass

    if skipped_areas:
        print(f"  [scan]   {len(skipped_areas)} area(s) skipped: "
              + ", ".join(s['name'] for s in skipped_areas))

    print(f"  [done]   Run complete in {time.time() - t0:.1f}s total")
    return items, skipped_areas


# ── Scoring pass ───────────────────────────────────────────────────────────────

def score_all(raw: list) -> list:
    try:
        from scoring import score_all as _score_all
        from explain import explain_all
        scored = _score_all(raw)
        explain_all(scored)
        return scored
    except ImportError:
        pass
    scored = []
    for l in raw:
        s = score_rental(l, raw)
        scored.append({**l, **s})
    scored.sort(key=lambda x: x.get("score_total", 0) or 0, reverse=True)
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


def write_output(listings: list, source: str = "immobiliare",
                 scanned_area_slugs: set = None):
    """Merge listings into rentals_latest.json, preserving entries from other sources.

    scanned_area_slugs: when provided (area-specific scan), any existing listing
    whose _fetched_area is in this set but whose id is not in the new results is
    considered de-listed and removed from the DB.
    """
    DASHBOARD_DIR.mkdir(exist_ok=True)
    existing: list = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text())
        except Exception:
            pass

    new_ids = {l["id"] for l in listings}
    stale_removed = 0
    kept = []
    for l in existing:
        if l.get("source") in (source, None):
            if l["id"] in new_ids:
                continue   # superseded by fresh version
            if scanned_area_slugs and l.get("_fetched_area") in scanned_area_slugs:
                stale_removed += 1
                continue   # de-listed: was in scanned area but not returned this run
        kept.append(l)

    if stale_removed:
        print(f"  [stale]  Removed {stale_removed} de-listed listing(s) from scanned areas")

    merged = kept + listings

    # Defensive dedup — new listings (appended last) win over any stale duplicates
    seen_dedup: set = set()
    deduped = []
    for l in reversed(merged):
        if l["id"] not in seen_dedup:
            seen_dedup.add(l["id"])
            deduped.append(l)
    merged = list(reversed(deduped))

    merged.sort(key=lambda x: x.get("score_total", 0), reverse=True)
    clean = [{k: v for k, v in l.items() if k != "omi"} for l in merged]
    OUTPUT_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2))


def write_status(new_count: int, total_seen: int, skipped_areas: list = None):
    payload = json.dumps({
        "last_run":      datetime.now().isoformat(timespec="seconds"),
        "new_count":     new_count,
        "total_seen":    total_seen,
        "skipped_areas": skipped_areas or [],
    })
    STATUS_PATH.write_text(payload)
    # Also write into dashboard/ so Netlify picks it up
    (DASHBOARD_DIR / "scanner_status.json").write_text(payload)


# ── Run cycle ──────────────────────────────────────────────────────────────────

def run_once(args) -> list:
    """Execute one fetch-score-write cycle. Returns newly seen listings."""
    if args.areas:
        area_names = [a.strip() for a in args.areas.split(",") if a.strip()]
    else:
        # No --areas flag: read active areas from area_settings.json
        area_names = _load_active_areas()
    raw, skipped_areas = fetch_rentals(
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
        write_status(new_count=0, total_seen=len(load_seen_ids()),
                     skipped_areas=skipped_areas)
        return []

    scored = score_all(raw)

    # Never overwrite the existing JSON with fewer listings than what's already there.
    # This protects against a bad scan (e.g. a scraping blip) wiping out good data.
    existing = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text())
        except Exception:
            pass
    existing_same = [l for l in existing if l.get("source", "immobiliare") == "immobiliare"]
    # Only guard against shrinkage on full-city scans. Area-specific scans are
    # intentionally smaller — blocking them would discard valid new data.
    if not area_names and len(scored) < len(existing_same):
        print(f"  ⚠  Scan returned {len(scored)} listings vs {len(existing_same)} immobiliare on disk — keeping existing data")
        write_status(new_count=0, total_seen=len(load_seen_ids()),
                     skipped_areas=skipped_areas)
        return []

    # Derive which area slugs were actually scanned from the listings themselves
    # (canonical slugs set on each listing during _fetch_async after redirect resolution).
    # Pass None for full-city scans (area slug = None) to skip stale removal.
    fetched_slugs = {l.get("_fetched_area") for l in scored if l.get("_fetched_area")}
    write_output(scored, scanned_area_slugs=fetched_slugs or None)

    seen         = load_seen_ids()
    new_listings = [l for l in scored if l["id"] not in seen]
    seen.update(l["id"] for l in scored)
    save_seen_ids(seen)

    write_status(len(new_listings), len(seen), skipped_areas=skipped_areas)
    print(f"  ✓ {len(scored)} rentals written · {len(new_listings)} new")

    # Deploy to Netlify — only when we have actual listings to show
    if NETLIFY_CONFIG_PATH.exists() and scored:
        _netlify_deploy()

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


# ── Netlify deploy helper ──────────────────────────────────────────────────────

NETLIFY_CONFIG_PATH = BASE_DIR / "netlify_config.json"

def _netlify_deploy():
    """
    Push the full dashboard/ directory straight to Netlify via the Deploy API.
    Netlify only uploads files whose SHA1 has changed — unchanged files are
    served from cache, so this is fast even for large builds.

    Requires netlify_config.json:
        { "site_id": "...", "token": "..." }
    """
    import hashlib
    import urllib.request
    import urllib.error

    # Hard guard — never deploy if rentals file is missing or empty
    if not OUTPUT_PATH.exists():
        print("  [netlify] rentals_latest.json missing — skipping deploy")
        return
    try:
        if not json.loads(OUTPUT_PATH.read_text()):
            print("  [netlify] rentals_latest.json is empty — skipping deploy")
            return
    except Exception:
        print("  [netlify] rentals_latest.json unreadable — skipping deploy")
        return

    if not NETLIFY_CONFIG_PATH.exists():
        print("  [netlify] netlify_config.json not found — skipping deploy")
        return
    try:
        cfg = json.loads(NETLIFY_CONFIG_PATH.read_text())
    except Exception as e:
        print(f"  [netlify] bad config: {e}")
        return

    site_id = cfg.get("site_id", "").strip()
    token   = cfg.get("token",   "").strip()
    if not site_id or not token:
        print("  [netlify] site_id / token missing in netlify_config.json — skipping")
        return

    # Gather every file in dashboard/ plus netlify.toml for redirects/headers
    file_map: dict[str, Path] = {}
    for f in DASHBOARD_DIR.rglob("*"):
        if f.is_file():
            netlify_path = "/" + f.relative_to(DASHBOARD_DIR).as_posix()
            file_map[netlify_path] = f
    toml = BASE_DIR / "netlify.toml"
    if toml.exists():
        file_map["/netlify.toml"] = toml

    if not file_map:
        print("  [netlify] dashboard/ is empty — nothing to deploy")
        return

    # SHA1 all files; Netlify will tell us which ones it already has cached
    contents: dict[str, bytes] = {}
    hashes:   dict[str, str]   = {}
    for path_key, file_path in file_map.items():
        data = file_path.read_bytes()
        contents[path_key] = data
        hashes[path_key]   = hashlib.sha1(data).hexdigest()

    auth = {"Authorization": f"Bearer {token}"}

    # ── Step 1: create deploy (send file manifest) ────────────────────────────
    body = json.dumps({"files": hashes}).encode()
    req  = urllib.request.Request(
        f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
        data=body,
        headers={**auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            deploy = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [netlify] ✗ create deploy failed ({e.code}): {e.read()[:300].decode()}")
        return
    except Exception as e:
        print(f"  [netlify] ✗ network error: {e}")
        return

    deploy_id = deploy["id"]
    required  = set(deploy.get("required", []))   # SHA1s Netlify needs uploaded
    print(f"  [netlify] deploy {deploy_id} — uploading {len(required)} changed file(s)…")

    # ── Step 2: upload only the files Netlify doesn't already have ────────────
    for path_key, data in contents.items():
        if hashes[path_key] not in required:
            continue
        clean_path = path_key.lstrip("/")
        req = urllib.request.Request(
            f"https://api.netlify.com/api/v1/deploys/{deploy_id}/files/{clean_path}",
            data=data,
            headers={**auth, "Content-Type": "application/octet-stream"},
            method="PUT",
        )
        try:
            urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            print(f"  [netlify] ✗ upload {path_key} failed ({e.code})")
            return
        except Exception as e:
            print(f"  [netlify] ✗ upload {path_key} error: {e}")
            return

    print(f"  [netlify] ✓ {len(file_map)} file(s) deployed — site is live")


# ── Daemon ─────────────────────────────────────────────────────────────────────

def daemon_loop(args):
    import traceback
    from email_digest import send_digest, load_config as load_email_config

    log_path = BASE_DIR / "scanner.log"
    log_fh   = open(log_path, "a", buffering=1)   # line-buffered

    def log(msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        print(line, file=log_fh, flush=True)

    log(f"Daemon started — interval {DAEMON_INTERVAL_SEC // 60} min, output {OUTPUT_PATH}")

    while True:
        log("Running scan…")
        new_listings = []
        try:
            new_listings = run_once(args)

            # Re-read email config on every iteration so dashboard changes take effect
            email_cfg  = load_email_config()

            # Send digest once per calendar day on the first scan that finds new listings.
            # No hour restriction — send as soon as new listings appear that day.
            def _should_send():
                today = str(date.today())
                return not (DIGEST_SENT_PATH.exists()
                            and DIGEST_SENT_PATH.read_text().strip() == today)

            want_email = email_cfg.get("enabled", False) or args.email
            if want_email and new_listings and _should_send():
                log(f"Sending digest for {len(new_listings)} new listing(s)…")
                send_digest(new_listings, email_cfg)
                mark_digest_sent()

            # Netlify deploy is handled inside run_once() automatically

            log(f"Scan done — {len(new_listings)} new listing(s)")

        except BaseException as e:
            # Catch everything (including SystemExit/KeyboardInterrupt) so the
            # loop survives transient crashes. KeyboardInterrupt re-raised to allow Ctrl-C.
            if isinstance(e, KeyboardInterrupt):
                log("Interrupted — stopping daemon.")
                log_fh.close()
                raise
            log(f"✗ Scan error: {e}\n{traceback.format_exc()}")
            try:
                write_status(new_count=0, total_seen=len(load_seen_ids()))
            except Exception:
                pass

        log(f"Sleeping {DAEMON_INTERVAL_SEC // 60} min…")
        try:
            time.sleep(DAEMON_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Interrupted during sleep — stopping daemon.")
            log_fh.close()
            raise


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
    p.add_argument("--netlify", action="store_true",
                   help="After each scan, deploy dashboard/ straight to Netlify via the "
                        "Deploy API (requires netlify_config.json with site_id + token)")
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
    if args.netlify:
        print(f"  Netlify : enabled (→ direct deploy after each scan)")
    print(f"{'─'*52}\n")

    if args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
