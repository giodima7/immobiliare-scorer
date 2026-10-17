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

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re as _re
import sys
import time
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import random

import nodriver as uc

from browser_session import BrowserSession, PageTimeout, timed

# Slug mapper translates internal area names (e.g. "cadore") into the real
# Immobiliare.it macro-area slug ("porta-romana-cadore-montenero") so the
# search URL actually matches the site's URL scheme. Without it scans land
# on 0-result pages or fetch the same macro-area three times in a row.
from area_slug_mapper import (
    group_by_immobiliare_slug,
    resolve_immobiliare_slug,
    verify_slug_mapping,
)

# ── Paths ──────────────────────────────────────────────────────────────────────
# Multi-city expansion (migration 007): paths that vary per scanned
# city — SEEN_IDS_PATH, OUTPUT_PATH — are reassigned by _apply_city()
# from main() once --city is parsed. Module-level helpers read the
# globals at call time, so the late-binding pattern is safe.
BASE_DIR         = Path(__file__).parent
DASHBOARD_DIR    = BASE_DIR / "dashboard"
STATUS_PATH          = BASE_DIR / "scanner_status.json"
DIGEST_SENT_PATH     = BASE_DIR / ".digest_sent_date"
CUSTOM_MAPPINGS_PATH = BASE_DIR / "custom_omi_mappings.json"
AREA_SETTINGS_PATH   = BASE_DIR / "area_settings.json"
# Defaults — overwritten by _apply_city() once main() parses --city.
SEEN_IDS_PATH    = BASE_DIR / "seen_ids_milano.json"
OUTPUT_PATH      = DASHBOARD_DIR / "milano_rentals_latest.json"

# ── Staleness thresholds ──────────────────────────────────────────────────────
# A listing is "stale" (probably off-market) once it has been absent from
# every scan for STALE_DAYS_THRESHOLD consecutive days. The dashboard hides
# stale listings by default but keeps them in the JSON.
# A listing is "removed" once it has been absent for REMOVE_AFTER_DAYS days —
# at that point it's definitively gone and we trim it from the JSON to keep
# the file bounded.
STALE_DAYS_THRESHOLD = 7
REMOVE_AFTER_DAYS    = 30

# Microsoft Edge is preferred over Chrome — Immobiliare's bot detection lets
# Edge through more reliably. Override via $BROWSER_EXECUTABLE_PATH.
EDGE_PATH = os.environ.get(
    "BROWSER_EXECUTABLE_PATH",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
# Backwards-compat alias — fetch_idealista.py still imports CHROME_PATH.
CHROME_PATH = EDGE_PATH

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
    """
    Return active areas for the current CITY_KEY from
    area_settings_{city}.json (or legacy area_settings.json for Milan).

    Supports both shapes:
      • New:    {"active_areas": ["macro-slug-1", "macro-slug-2", ...]}
                — Settings UI redesign writes this directly.
      • Legacy: {"areas": [{"id", "name", "active"}, ...]}
                — pre-redesign format with display names + active flag.

    The downstream group_by_immobiliare_slug() step makes both shapes
    converge on the same macro-slug list, so this loader just hands
    back the raw strings.

    Crucially: when scanning a non-Milan city and no per-city area file
    exists, we DON'T read the Milan area_settings — those names won't
    resolve as Idealista/Immobiliare URLs in another city, and the
    scanner would 0-out the whole run trying to scrape Milan zones in
    Rome.
    """
    per_city = BASE_DIR / f"area_settings_{CITY_KEY}.json"
    paths = []
    if per_city.exists():
        paths.append(per_city)
    if CITY_KEY == "milano" and AREA_SETTINGS_PATH.exists():
        paths.append(AREA_SETTINGS_PATH)
    for p in paths:
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                continue
            # New shape wins when both keys are present.
            if isinstance(data.get("active_areas"), list):
                return [s for s in data["active_areas"]
                        if isinstance(s, str) and s.strip()]
            if "areas" in data:
                return [a["name"] for a in data["areas"]
                        if isinstance(a, dict) and a.get("active") and a.get("name")]
        except Exception:
            continue
    return []
# ── City registry ──────────────────────────────────────────────────────────────
# Mirrors the Supabase cities table and the entries in omi_lookup.py's
# CITY_REGISTRY. Lookup at startup translates the --city arg into the
# label / URL slug the Immobiliare.it scraper needs.
CITY_REGISTRY: dict[str, dict] = {
    "milano":       {"label": "Milano",       "slug": "milano"},
    "roma":         {"label": "Roma",         "slug": "roma"},
    "napoli":       {"label": "Napoli",       "slug": "napoli"},
    "la_maddalena": {"label": "La Maddalena", "slug": "la-maddalena"},
}

# Defaults — overwritten by _apply_city() from main() once --city parses.
CITY_KEY   = "milano"
CITY_LABEL = "Milano"
CITY_SLUG  = "milano"


def _apply_city(city: str) -> None:
    """
    Update every per-city module global from a single --city arg. Called
    at the top of main() before any scan / fetch / OMI logic runs.

    Reassigning module-level globals (rather than passing city through
    every function signature) keeps the change small relative to the
    1,600-line file and avoids breaking any internal callers — Python's
    late binding means helpers see the updated values.
    """
    global CITY_KEY, CITY_LABEL, CITY_SLUG, SEEN_IDS_PATH, OUTPUT_PATH
    if city not in CITY_REGISTRY:
        raise ValueError(f"unknown city '{city}'; known: {list(CITY_REGISTRY)}")
    cfg = CITY_REGISTRY[city]
    CITY_KEY      = city
    CITY_LABEL    = cfg["label"]
    CITY_SLUG     = cfg["slug"]
    SEEN_IDS_PATH = BASE_DIR      / f"seen_ids_{city}.json"
    OUTPUT_PATH   = DASHBOARD_DIR / f"{city}_rentals_latest.json"

DAEMON_INTERVAL_SEC = 24 * 60 * 60   # one scan per day
DAEMON_RETRY_SEC    = 60 * 60        # after a failed scan


def _last_scan_path(source: str) -> Path:
    return BASE_DIR / f".last_scan_{source}_{CITY_KEY}"


def last_scan_at(source: str) -> float:
    """Wall-clock time of the last successful daemon scan (0 = never).
    Persisted so restarting api.py — which restarts the daemons — doesn't
    trigger an extra scan."""
    try:
        return float(_last_scan_path(source).read_text().strip())
    except (OSError, ValueError):
        return 0.0


def mark_scan_done(source: str) -> None:
    _last_scan_path(source).write_text(str(time.time()))


def sleep_until(deadline: float) -> None:
    """Sleep until wall-clock `deadline`. time.sleep() alone runs on a clock
    that stops while the laptop sleeps — the old 60-min sleep stretched so
    much that ~26 scans ran in 100 days. Re-checking the wall clock every
    minute makes an overdue scan start soon after wake."""
    while (left := deadline - time.time()) > 0:
        time.sleep(min(60.0, left))


def run_marked(source: str, fn, *args):
    """Run a scan with a `.scan_running_<source>` marker present, so the
    dashboard can tell "scanning now" from "daemon alive" (api.py)."""
    marker = BASE_DIR / f".scan_running_{source}"
    marker.write_text(str(os.getpid()))
    try:
        return fn(*args)
    finally:
        marker.unlink(missing_ok=True)


def wait_and_drip(source: str, deadline: float, log=print, after_batch=None) -> None:
    """Wait for the next scan, fetching listing pages meanwhile
    (listing_details.drip_until). Falls back to a plain wait on any error."""
    try:
        from listing_details import drip_until
        drip_until(source, deadline, log, after_batch=after_batch)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log(f"[details] drip stopped ({type(exc).__name__}: {exc}) — plain wait")
    sleep_until(deadline)
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


def _parse_count_plus(raw) -> Optional[int]:
    """Parse Immobiliare's count fields tolerantly.

    Immobiliare's search payload returns string values like '3', '5+',
    '3+' for the rooms / bathrooms / bedRoomsNumber fields. Plain
    int() chokes on the trailing '+' and used to silently null large
    apartments (158 listings). Strip non-digits before converting so
    '5+' → 5 (treated as the lower bound, matching what the source
    page displays in its spec sheet).
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    s = _re.sub(r"[^\d]", "", str(raw))
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def build_rental_url(page: int, area_slug: str = None,
                     max_rent: int = 0, min_sqm: int = 0, max_sqm: int = 0,
                     min_rooms: int = 0, newest: bool = False) -> str:
    """Build a fully parameterised Immobiliare.it rental search URL.
    newest: most recently published first (the site's "Più recenti")."""
    base = f"https://www.immobiliare.it/affitto-case/{CITY_SLUG}/"
    if area_slug:
        base += f"{area_slug}/"
    params = {}
    if max_rent:  params["prezzoMassimo"]    = max_rent
    if min_sqm:   params["superficieMinima"] = min_sqm
    if max_sqm:   params["superficieMassima"]= max_sqm
    if min_rooms: params["localiMinimo"]     = min_rooms
    if newest:
        params["criterio"], params["ordine"] = "data", "desc"
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


# ── Parser helpers ─────────────────────────────────────────────────────────────

# Immobiliare's search-page __NEXT_DATA__ payload does NOT carry condo fees as
# a structured field — only `value/formattedValue/priceRange/visible` under
# `price`. The fees, when stated, live in the free-text `description`. Parse
# the most common Italian phrasings here.

_CONDO_KEYWORD_RX = _re.compile(
    r"\b(spese(?:\s*(?:condomin\w*|cond\.li|cond\b|annu\w+|mensil\w+))?|condominio)\b"
    r"[\s:\-‐-―−]*"
    r"(?:di\s+|pari\s+a\s+)?"
    r"(?:€|eur\w*\s*)?"
    r"\s*(\d[\d.,]*)\s*"
    r"(?:€|eur\w*)?",
    _re.IGNORECASE,
)
_PERIOD_ANNUAL_RX  = _re.compile(r"annu\w+|/\s*anno|all['’]?\s*anno", _re.IGNORECASE)
_PERIOD_MONTHLY_RX = _re.compile(r"mensil\w+|/\s*mese|al\s+mese|/\s*mo\b", _re.IGNORECASE)
_NEGATIVE_CONTEXT_RX = _re.compile(
    r"da\s+definire|a\s+parte|esclus[aei]|incluse?",
    _re.IGNORECASE,
)
def _cost_per_month(raw) -> Optional[int]:
    """'€ 300/mese' → 300, '€ 2.400/anno' → 200 (Immobiliare's costs block)."""
    digits = _re.sub(r"[^\d]", "", str(raw or "").split(",")[0])
    if not digits:
        return None
    value = int(digits)
    return round(value / 12) if "anno" in str(raw).lower() else value


_CONDO_INCLUDED_RX = _re.compile(
    r"(?<!non )\b(?:spese(?:\s+condominiali)?|condominio)\s+(?:sono\s+)?(?:incluse|inclusa|incluso|compres[aeio])\b"
    r"|\b(?:canone|affitto|prezzo)\s+(?:è\s+)?comprensiv[oa]\s+(?:delle\s+|di\s+)?spese\b", _re.I)


def condo_included(text) -> bool:
    """The listing says the rent already covers the condo fees."""
    return bool(text) and bool(_CONDO_INCLUDED_RX.search(str(text).replace("\xa0", " ")))


def _parse_condo_from_description(desc):
    """
    Extract monthly condo fees from an Italian listing description.
    Strategy: keyword-anchored regex (`spese`/`condominio`) finds candidate
    matches; period (monthly vs annual) is decided from a 50-char window
    around each match. Sanity range: 20-1000 EUR/month.
    Returns int or None.
    """
    if not desc or not isinstance(desc, str):
        return None
    text = desc.replace("\xa0", " ")  # non-breaking space -> regular space
    for m in _CONDO_KEYWORD_RX.finditer(text):
        start, end = m.span()
        ctx = text[max(0, start - 10): min(len(text), end + 40)]
        # Skip "spese da definire", "spese a parte", "spese escluse/incluse"
        if _NEGATIVE_CONTEXT_RX.search(ctx):
            continue
        num = m.group(2)
        # Italian: ',' decimal, '.' thousand separator
        if "," in num:
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(".", "")
        try:
            val = float(num)
        except ValueError:
            continue
        # Default monthly; switch to annual when context says so explicitly
        if (_PERIOD_ANNUAL_RX.search(ctx) and
                not _PERIOD_MONTHLY_RX.search(ctx)):
            val = val / 12
        if 20 <= val <= 1000:
            return int(round(val))
    # Number before the keyword: "€ 950 + 200 per oneri condominiali",
    # "+ € 150 di spese condominiali".
    for m in _CONDO_AMOUNT_FIRST_RX.finditer(text):
        val = float(m.group(1).replace(".", "").replace(",", "."))
        ctx = text[m.start(): min(len(text), m.end() + 30)]
        if _PERIOD_ANNUAL_RX.search(ctx) and not _PERIOD_MONTHLY_RX.search(ctx):
            val = val / 12
        if 20 <= val <= 1000:
            return int(round(val))
    return None


_CONDO_AMOUNT_FIRST_RX = _re.compile(
    r"\+\s*(?:€\s*)?(\d[\d.,]*)\s*(?:€|eur\w*)?\s*(?:per\s+|di\s+)?"
    r"(?:oneri|spese)(?:\s+condomin\w*)",
    _re.IGNORECASE,
)


# ── Parser ─────────────────────────────────────────────────────────────────────

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

    # Condominium fees (spese condominiali).
    # Step 1: try structured fields. Immobiliare's search payload does NOT
    # currently expose them, but try anyway in case the API changes.
    # Step 2: fall back to parsing the free-text description, which is where
    # the fees actually live in practice (e.g. "Spese condominiali: € 200").
    spese = None
    for field in ("expenses", "condominiumFees", "monthlyCharges"):
        raw = prop.get(field) or price_data.get(field)
        if raw is not None:
            try:
                spese = int(_re.sub(r"[^\d]", "", str(raw)))
                break
            except (ValueError, AttributeError):
                pass

    if spese is None:
        # Detail pages carry it under costs: "€ 200/mese" (or per year).
        raw = (prop.get("costs") or {}).get("condominiumExpenses")
        digits = _re.sub(r"[^\d]", "", str(raw or "").split(",")[0])
        if digits:
            spese = int(digits)
            if "anno" in str(raw).lower():
                spese = round(spese / 12)

    if spese is None:
        spese = _parse_condo_from_description(prop.get("description") or "")

    # The rest of the monthly cost, on listing pages only: heating billed
    # apart (centralised heating — "€ 2.400/anno" is €200 a month on top),
    # the deposit, and whether the rent already covers the condo fees.
    costs = prop.get("costs") or {}
    heating_mo = _cost_per_month(costs.get("heatingExpenses"))
    included = (condo_included(costs.get("condominiumExpenses"))
                or (spese is None and condo_included(prop.get("description"))))
    deposit_raw = str(((prop.get("rent") or {}).get("deposit")) or "")
    deposit_months = deposit_eur = None
    m_dep = _re.search(r"(\d+)\s*(?:mensilit|mes)", deposit_raw, _re.I)
    if m_dep:
        deposit_months = int(m_dep.group(1))
    elif _re.search(r"\d", deposit_raw):
        deposit_eur = _cost_per_month(deposit_raw.replace("/mese", ""))

    # Rooms — try numeric field first, then parse from Italian title.
    # Immobiliare returns string values like '5+' / '3+' for large
    # apartments (it caps the display at 5+). Plain int() chokes on the
    # '+' and used to silently null the field — 158 immobiliare listings
    # were losing rooms data this way, including ~all Appartamenti/Attici.
    # _parse_count_plus strips the trailing '+' so '5+' → 5 (lower bound,
    # which matches what the source page itself shows in the spec sheet).
    rooms = _parse_count_plus(
        prop.get("rooms") or prop.get("roomsNumber") or prop.get("numRooms")
    )
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

    # No typology fallback: it filled `condition` with "Appartamento" /
    # "Bilocale" (703 Milan rows) — scored as unknown anyway, shown as if
    # it were the flat's condition.
    condition_raw = prop.get("ga4Condition") or prop.get("condition") or None

    listing_id = str(re_data.get("id", ""))
    url = f"https://www.immobiliare.it/annunci/{listing_id}/" if listing_id else ""

    # Thumbnail + photo gallery — both come from properties[0].multimedia.photos
    # (realEstate.multimedia is empty on listing-search pages).
    # We capture up to 8 URLs total. Cap chosen because the average photo URL
    # is ~150 chars — 8 × ~10k listings ≈ 12 MB added to the JSON.
    photos        = prop.get("multimedia", {}).get("photos", [])
    photo_urls    = parse_photo_urls(photos)
    thumbnail     = photo_urls[0] if photo_urls else None

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

    # Search results carry features as Italian strings in `ga4features`
    # ("balcone", "arredato", "esposizione esterna", …) plus `ga4Heating` /
    # `ga4Garage`. The other keys read below ("features", "heatingType",
    # "furnished", "isExternal") are absent there — these fields were 100 %
    # null on every scan (verified on a live search payload, 2026-10-14).
    ga4_feats = {str(f).strip().lower() for f in (prop.get("ga4features") or [])}

    # is_external
    ext_raw = prop.get("isExternal") or prop.get("external") or prop.get("ga4Exposure")
    is_external = None
    if ext_raw is not None:
        if isinstance(ext_raw, bool):
            is_external = ext_raw
        elif isinstance(ext_raw, str):
            is_external = ext_raw.lower() in ("true", "yes", "esterno", "1")
    if is_external is None:
        if {"esposizione esterna", "esposizione doppia"} & ga4_feats:
            is_external = True
        elif "esposizione interna" in ga4_feats:
            is_external = False

    # energy_class
    energy_class = (
        prop.get("energyClass") or prop.get("energy_class")
        or re_data.get("energyClass")
        or ((prop.get("energy") or {}).get("class") or {}).get("name")   # detail page
        or ""
    )
    energy_class = str(energy_class).strip().upper() if energy_class else None

    # year_built
    year_built = (prop.get("buildingYear") or prop.get("yearBuilt")      # detail page: buildingYear
                  or prop.get("year_built") or re_data.get("yearBuilt"))
    if year_built is not None:
        try:
            year_built = int(year_built)
        except (TypeError, ValueError):
            year_built = None

    # bathrooms (= "bagni") — same '3+' caveat as rooms above.
    bathrooms = _parse_count_plus(prop.get("bathrooms") or prop.get("bathRooms"))

    # bedrooms (= "camere da letto" — bedrooms only, NOT total locali)
    # Italian distinction: rooms = locali (total habitable rooms incl. living)
    #                     bedRoomsNumber = camere = bedrooms only
    beds_raw = prop.get("bedRoomsNumber") or prop.get("bedrooms")
    # 0/'0' indicates "no separate bedrooms" (Monolocale / Open space) — we
    # treat that as null on purpose so the dashboard omits the bed icon
    # rather than printing a confusing "🛏 0".
    if beds_raw in (None, '', '0', 0):
        bedrooms = None
    else:
        bedrooms = _parse_count_plus(beds_raw)
    # Sanity: bedroom count cannot exceed total rooms
    if bedrooms is not None and rooms is not None and bedrooms > rooms:
        bedrooms = None

    # ── features[] array  ─────────────────────────────────────────────────────
    # Two shapes coexist depending on the page:
    #   • search results: [{"type": "balcony", "label": "Balcone"}, ...]
    #   • detail page:    ["balcony", "elevator", ...]   (bare strings)
    # Tolerate both — the detail-page backfill (detail_backfill.py)
    # exercises the bare-string path; the regular search-page scan
    # exercises the dict path. Mixing them would silently lose features.
    _feature_types: set = set()
    for feat in (prop.get("features") or re_data.get("features") or []):
        if isinstance(feat, dict):
            ft = feat.get("type") or feat.get("name") or ""
            _feature_types.add(str(ft).lower())
            lbl = str(feat.get("label") or "").lower()
        elif isinstance(feat, str):
            _feature_types.add(feat.lower())
            lbl = feat.lower()
        else:
            continue
        # Also check label/string for Italian keywords (works for both shapes).
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
    # ga4features is the complete list — detail pages' `features` is a
    # partial one (4 of 7 entries on a 2026-10-14 sample, no "balcone"),
    # so let it overrule a features-derived False.
    if ga4_feats and not has_balcony:
        has_balcony = any(f.startswith(("balcon", "terrazz")) for f in ga4_feats)

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
    if (ga4_feats or prop.get("ga4Garage")) and not has_parking:
        # e.g. "1 posto auto", "1 in box privato/box in garage"
        has_parking = bool(prop.get("ga4Garage"))

    # heating_type
    # ga4Heating ("Autonomo" / "Centralizzato") is the dedicated field — never
    # infer heating from ga4features: it lists "impianto tv centralizzato".
    heat_raw = prop.get("heatingType") or prop.get("heating") or prop.get("ga4Heating") or ""
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
    if furnished is None and ga4_feats:
        # Exact phrases: "non arredato" / "solo cucina arredata" contain the
        # word but mean unfurnished; no furnishing phrase at all = unknown.
        if {"arredato", "parzialmente arredato"} & ga4_feats:
            furnished = True
        elif {"non arredato", "solo cucina arredata"} & ga4_feats:
            furnished = False

    # photo_count — count photos in the raw multimedia list (not the URL cap)
    photo_count = len(photos) if isinstance(photos, list) else 0

    # published_date + days_on_market
    published_date = None
    days_on_market = None
    created = re_data.get("createdAt")          # detail page: epoch seconds
    if isinstance(created, (int, float)) and created > 0:
        pub = datetime.fromtimestamp(created).date()
        published_date = pub.isoformat()
        days_on_market = (date.today() - pub).days
    for date_field in ("dataModifica", "pubblicazione", "dataInserimento",
                       "publicationDate", "insertionDate", "created_at",
                       "createdAt", "insertDate", "datePublished", "firstSeenDate"):
        raw_date = re_data.get(date_field) or prop.get(date_field)
        if raw_date and published_date is None:
            try:
                pub_str = str(raw_date)[:10]
                pub = datetime.fromisoformat(pub_str).date()
                published_date = pub_str
                days_on_market = (date.today() - pub).days
                break
            except Exception:
                pass

    return {
        "id":                 listing_id,
        "source":             "immobiliare",
        # `city` is the lowercase ISTAT-style code stored in Supabase
        # (matches the cities table primary key + the listings.city
        # column from migration 007). `city_label` is the display name.
        "city":               CITY_KEY,
        "city_label":         CITY_LABEL,
        "city_key":           CITY_KEY,    # kept for back-compat with
                                           # callers that still read it
                                           # under the old name
        "title":              re_data.get("title", ""),
        "neighbourhood":      neighbourhood,
        "address":            address,
        "latitude":           latitude,
        "longitude":          longitude,
        "rent_mo":            rent,         # monthly rent €
        "sqm":                sqm,
        "ask_psqm":           ask_psqm,     # €/m²/month
        "spese_condominiali": spese,        # condominium fees €/mo (or None)
        "heating_mo":         heating_mo,   # heating billed apart, €/mo
        "condo_included":     included or None,
        "deposit_months":     deposit_months,
        "deposit_eur":        deposit_eur,
        "rooms":              rooms,
        "bedrooms":           bedrooms,        # camere da letto (bedrooms only)
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
        "published_date":     published_date,
        "condition":          condition_raw,
        "thumbnail":          thumbnail,    # first listing photo URL (or None)
        "photos":             photo_urls,   # up to 8 photo URLs for the gallery
        "url":                url,
        "omi":                omi,           # dropped before JSON export
        "fetched_at":         datetime.now().isoformat(timespec="seconds"),
        **parse_advertiser(re_data),
    }


# ── Fetch (nodriver) ───────────────────────────────────────────────────────────

async def _fetch_async(pages: int, area_slugs: list, max_rent: int,
                       min_sqm: int, max_sqm: int, min_rooms: int,
                       delay: float, browser,
                       area_delay: float = 6.0,
                       complete: dict | None = None,
                       stats: dict | None = None) -> tuple:
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

    Three rate-limit countermeasures shipped together 2026-05-22:
      • Option 1 — per-area pacing: sleep `area_delay` (default 6 s + jitter)
        BETWEEN macros so Cloudflare's anti-bot window resets between bursts.
      • Option 2 — randomized macro order: shuffle `targets` so failures
        don't always land on the same alphabetical-tail areas.
      • Option 3 — end-of-scan retry pass on JUST the skipped areas, with
        3× page delay + 2× area delay, so transient Cloudflare blocks get
        a second chance without a full city-wide re-scan.

    complete: when given, gets {area_slug: canonical_slug} for every area
    read to its last page — only those can tell a listing has gone.
    stats: when given, counts page loads ("pages") and unreadable pages
    ("parse_failed").
    """
    stats = {} if stats is None else stats
    stats.setdefault("pages", 0)
    stats.setdefault("parse_failed", 0)
    all_items     = []
    seen_ids      = set()   # global dedup across all areas
    skipped_areas = []      # {"name", "reason"} for areas that failed or returned nothing

    async def _scan_one_area(area_slug, page_delay):
        """Per-macro-area fetch. Closure over all the outer state so we can
        call it both in the main pass and the end-of-scan retry pass
        without re-threading the parameter list."""
        label = area_slug or "all Milano"
        print(f"\n    area: {label}", end="", flush=True)
        try:
            area_ids_seen    = set()
            # canonical_slug: resolved by the site on the first request (may differ
            # from area_slug when the site redirects e.g. porta-romana →
            # porta-romana-medaglie-d-oro, which drops pag= from the URL).
            canonical_slug   = area_slug
            area_got_results = False
            area_parse_failed = False
            finished = False   # read to the last page

            for page in range(1, pages + 1):
                url = build_rental_url(page, canonical_slug, max_rent, min_sqm, max_sqm, min_rooms)
                print(f"\n    [debug] GET {url}", flush=True)
                tab = await browser.get(url)
                stats["pages"] += 1
                await asyncio.sleep(page_delay)

                # Redirect detection on page 1 — Immobiliare sometimes rewrites
                # a slug to a more specific sub-area URL, stripping pag=.
                if page == 1 and area_slug:
                    try:
                        actual_href = await timed(tab.evaluate("window.location.href"),
                                                  what="read location")
                        if actual_href:
                            m = _re.search(rf'/{_re.escape(CITY_SLUG)}/([^/?#]+)', actual_href)
                            if m:
                                resolved = m.group(1)
                                if resolved != canonical_slug:
                                    print(f" [→{resolved}]", end="", flush=True)
                                    canonical_slug = resolved
                                    # The redirect drops the query string
                                    # (navigli → navigli-darsena/, no filters),
                                    # so page 1 would be unfiltered. Reload it
                                    # on the resolved slug with the filters.
                                    if "?" in url and "?" not in actual_href:
                                        url = build_rental_url(page, canonical_slug, max_rent,
                                                               min_sqm, max_sqm, min_rooms)
                                        tab = await browser.get(url)
                                        await asyncio.sleep(page_delay)
                    except PageTimeout:
                        raise
                    except Exception:
                        pass

                # __NEXT_DATA__ parse with one retry — a single failure
                # usually means we caught an anti-bot challenge mid-scan,
                # not a real empty result. Any page, not just page 1: a
                # failure on page 7 used to silently cut the area short.
                data = None
                for _attempt in (1, 2):
                    try:
                        raw  = await timed(tab.evaluate("JSON.stringify(window.__NEXT_DATA__)"),
                                           what="read __NEXT_DATA__")
                        nd   = json.loads(raw)
                        data = nd["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
                        break
                    except PageTimeout:
                        raise
                    except Exception as e:
                        if _attempt == 1:
                            print(f"\n    [retry] {label} p{page}: parse failed ({e}); "
                                  f"sleeping 4s before re-navigating", flush=True)
                            await asyncio.sleep(4.0)
                            try:
                                tab = await browser.get(url)
                                await asyncio.sleep(page_delay)
                            except PageTimeout:
                                raise
                            except Exception:
                                pass
                            continue
                        print(f"\n    [warn] __NEXT_DATA__ parse failed (area={label}, page={page}): {e}")
                        area_parse_failed = True
                        stats["parse_failed"] += 1
                        break
                if data is None:
                    break

                results = data.get("results", [])
                if not results:
                    print(f" (empty, done)", end="", flush=True)
                    finished = True
                    break

                area_got_results = True

                page_ids = {
                    str(item.get("realEstate", {}).get("id", ""))
                    for item in results
                    if item.get("realEstate", {}).get("id")
                }
                if page > 1 and page_ids and page_ids.issubset(area_ids_seen):
                    print(f" (repeat page, done)", end="", flush=True)
                    finished = True
                    break
                area_ids_seen.update(page_ids)

                new_this_page = 0
                for item in results:
                    parsed = parse_rental(item)
                    if not parsed or parsed["id"] in seen_ids:
                        continue
                    parsed["_fetched_area"] = canonical_slug
                    parsed["_via"] = "sweep"
                    if canonical_slug:
                        parsed["scan_area"] = canonical_slug   # kept on disk (de-listing)
                    seen_ids.add(parsed["id"])
                    all_items.append(parsed)
                    new_this_page += 1

                site_max = data.get("maxPages") or pages
                max_pg   = min(pages, site_max)
                print(f" p{page}/{max_pg}(+{new_this_page})", end="", flush=True)
                if page >= max_pg:
                    finished = site_max <= pages   # more pages than we read → not all of it
                    break

            if finished and area_slug and complete is not None:
                complete[area_slug] = canonical_slug
            if not area_got_results and area_slug:
                if area_parse_failed:
                    reason = ("page parse failed twice (likely Cloudflare "
                              "anti-bot rate limit, not a bad slug)")
                else:
                    reason = "no results — slug may be invalid or area not found on Immobiliare.it"
                suggested = resolve_immobiliare_slug(area_slug, CITY_KEY)
                rec = {"name": area_slug, "reason": reason}
                if suggested and suggested != area_slug:
                    rec["suggested_slug"] = suggested
                skipped_areas.append(rec)
                print(f"\n    [warn] No results for area '{label}' — {reason}", flush=True)

        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            skipped_areas.append({"name": area_slug or "all Milano", "reason": reason})
            print(f"\n    [error] Area '{label}' failed: {reason} — skipping", flush=True)
            # A hung or dead browser would fail every remaining area too
            # (2026-08-24: Edge died and the scan kept looping on it).
            if isinstance(exc, PageTimeout) or not await browser.alive():
                await browser.restart()

    # ── Pass 1 — full sweep ───────────────────────────────────────────────
    # None sentinel = no area path → fetch whole city
    targets = list(area_slugs) if area_slugs else [None]
    if area_slugs:
        # Option 2: shuffle so failures don't always land on the same
        # alphabetical-tail areas across consecutive scans.
        random.shuffle(targets)
    for i, area_slug in enumerate(targets):
        await _scan_one_area(area_slug, delay)
        # Option 1: idle between macros so Cloudflare's anti-bot
        # window resets between bursts. Skip after the last area
        # AND skip after the city-wide pass (only one entry there).
        if area_slug and i < len(targets) - 1:
            jitter = random.uniform(0.0, area_delay * 0.4)
            await asyncio.sleep(area_delay + jitter)

    # ── Pass 2 — retry skipped areas ──────────────────────────────────────
    # Option 3: give Cloudflare-blocked areas a second chance with
    # 3× page delay + 2× area delay. Drains skipped_areas first so
    # the retry pass repopulates from scratch — any area still
    # failing after the retry stays in skipped_areas as the final
    # truth, which is what the dashboard surfaces.
    if area_slugs and skipped_areas:
        retry_targets = [s["name"] for s in skipped_areas if s.get("name")]
        skipped_areas.clear()
        print(f"\n    [retry-pass] revisiting {len(retry_targets)} skipped "
              f"area(s) at 3× delay…", flush=True)
        random.shuffle(retry_targets)
        for i, area_slug in enumerate(retry_targets):
            await _scan_one_area(area_slug, delay * 3.0)
            if i < len(retry_targets) - 1:
                jitter = random.uniform(0.0, area_delay * 0.6)
                await asyncio.sleep(area_delay * 2.0 + jitter)

    # ── No city-wide fallback ──────────────────────────────────────────────
    # An earlier version of this scanner did ONE city-wide pass whenever
    # any area returned 0 results, to recover the would-be listings.
    # Removed 2026-05-22 — with the verified slug mapping in place,
    # 0-result areas now genuinely mean "no active listings here" or
    # "Cloudflare blip", not "wrong URL". The fallback was duplicating
    # already-fetched listings, attributing them to a synthetic
    # _city_wide_fallback area, and inflating scan time. Truthful empty
    # is better than synthetic recovery.

    return all_items, skipped_areas


# ── Daily incremental scan ────────────────────────────────────────────────────
# A full sweep is ~220 page loads; from ~200 loads in one session every
# remaining area's page came back blocked (8 of 10 full scans lost 6–8 of
# 17 areas), and >99 % of the loads re-read listings we already had. The
# daily scan stays well under that:
#   1. discover — city-wide search, most recently published first
#      (criterio=data&ordine=desc), until DAILY_STOP_AFTER pages in a row
#      bring nothing new. It used the default order, thought to be "most
#      recently modified" (2026-10-14); on 2026-10-17 its page 1 held the
#      same 25 old listings (ids 104M–133.4M) at 11:05 and 14:38, while
#      criterio=data showed 25 from the last hours — 21 unknown to us;
#   2. sweep    — 1/DAILY_SWEEP_DAYS of the active areas in full, so every
#      listing's last_seen_date refreshes within STALE_DAYS_THRESHOLD;
#   3. details  — detail pages (energy class, publication date, condo fees:
#      absent from search results), DAILY_DETAIL_BUDGET per scan. 0 since
#      2026-10-14: the daemon fetches them between scans instead, best
#      candidates first (listing_details.py, wait_and_drip).
DAILY_DISCOVER_MAX_PAGES = 15
DAILY_STOP_AFTER         = 2
DAILY_SWEEP_DAYS         = 4
DAILY_DETAIL_BUDGET      = 0
DETAIL_FIELDS = ("energy_class", "published_date", "spese_condominiali", "detail_fetched_at")
DETAIL_URL    = "https://www.immobiliare.it/annunci/{id}/"

# ── Spread scan (pilot from 2026-10-17; replaces the daily scan) ─────────────
# The daily scan swept each area once every DAILY_SWEEP_DAYS days, and its
# discover step read the site's default order, which isn't by date: of the
# 73 newest Immobiliare listings on 2026-10-17, ~60 sat in that day's 4
# swept areas — elsewhere new listings waited up to 4 days, dead ones
# stayed up to 11. Immobiliare blocks at ~200 page loads in one session, yet never
# blocked the detail drip (3 pages per session, all day). So the spread scan
# runs one slot every SLOT_GAP_SEC: a fresh browser session sweeps the ONE
# area swept longest ago (≤ ~40 pages), every area once a day, plus a
# discover run every DISCOVER_EVERY_SEC that stops where the previous run's
# listings begin. A listing missing from DELIST_AFTER_MISSES complete sweeps
# of its area in a row is hidden. Each slot appends a line to
# scan_slots_<city>.jsonl (tools/slot_report.py). SPREAD_SCAN = False →
# back to the daily scan.
SPREAD_SCAN           = True
SLOT_GAP_SEC          = None             # None → 24 h / (active areas + 1)
DISCOVER_EVERY_SEC    = 20 * 60          # also between slots (was 2.5 h, inside slots only)
DISCOVER_OVERLAP_STOP = 5                # of a page's 25 listings
DELIST_AFTER_MISSES   = 2
# A sweep read to its last page that still brings back under half of the
# area's visible listings is a bad page set, not a mass de-listing.
DELIST_MIN_FOUND      = 0.5

# Photos of top listings not read yet are read after every slot of either
# site and after every listing-page session (one reading at a time —
# photo_eval's lock), so they keep up with the galleries those pages bring.
# ~2k tokens each.
PHOTOS_PER_SLOT       = 32
PHOTOS_PER_BATCH      = 8                # one claude call

# Filled by fetch_rentals() for the caller: complete sweeps, page loads,
# discover stats.
LAST_FETCH: dict = {}


def sweep_slugs_for_today(area_slugs: list) -> list:
    """Every DAILY_SWEEP_DAYS-th area, rotating daily — each area in full
    once every DAILY_SWEEP_DAYS days."""
    ordered = sorted(area_slugs)
    return ordered[date.today().toordinal() % DAILY_SWEEP_DAYS::DAILY_SWEEP_DAYS]


def _macro_slug(item: dict) -> str:
    props = (item.get("realEstate") or {}).get("properties") or [{}]
    return to_url_slug(((props[0] or {}).get("location") or {}).get("macrozone") or "")


async def _discover_async(active: set, max_rent: int, min_sqm: int, max_sqm: int,
                          min_rooms: int, delay: float, browser, known_ids: set,
                          prev_ids: set | None = None) -> tuple:
    """City-wide search, most recently published first. Returns
    ({id: listing}, stats).

    prev_ids None (daily scan): stop after DAILY_STOP_AFTER pages in a row
    with nothing new. prev_ids = the ids the previous discover run read
    (spread scan): once a page holds DISCOVER_OVERLAP_STOP of them,
    everything further down was read last time — stop there."""
    found: dict = {}
    stats = {"pages": 0, "new": 0, "per_page": [], "caught_up": False, "ids": []}
    quiet_pages = 0
    print("\n    discover: city-wide, newest first", end="", flush=True)
    for page in range(1, DAILY_DISCOVER_MAX_PAGES + 1):
        url = build_rental_url(page, None, max_rent, min_sqm, max_sqm, min_rooms, newest=True)
        data = None
        for attempt in (1, 2):
            try:
                tab = await browser.get(url)
                stats["pages"] += 1
                await asyncio.sleep(delay if attempt == 1 else delay + 4.0)
                raw = await timed(tab.evaluate("JSON.stringify(window.__NEXT_DATA__)"),
                                  what="read __NEXT_DATA__")
                data = json.loads(raw)["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
                break
            except PageTimeout:
                await browser.restart()
                break
            except Exception as exc:
                # 2026-10-16: page 1 came back unreadable (TypeError) and
                # the whole discovery was lost — one more try first.
                if attempt == 2:
                    print(f"\n    [warn] discover page {page} unreadable ({type(exc).__name__}) — stopping discovery")
        if data is None:
            break
        results = data.get("results") or []
        new_here = overlap = 0
        for item in results:
            rid = str((item.get("realEstate") or {}).get("id") or "")
            if rid:
                stats["ids"].append(rid)
                overlap += rid in (prev_ids or ())
            macro = _macro_slug(item)
            if macro not in active:
                continue
            parsed = parse_rental(item)
            if not parsed:
                continue
            parsed["_fetched_area"] = macro
            parsed["_via"] = "discover"
            if parsed["id"] not in known_ids and parsed["id"] not in found:
                new_here += 1
            found[parsed["id"]] = parsed
        stats["new"] += new_here
        stats["per_page"].append(new_here)
        print(f" p{page}(+{new_here} new{f', {overlap} seen last run' if prev_ids else ''})",
              end="", flush=True)
        if not results or page >= (data.get("maxPages") or page):
            stats["caught_up"] = True
            break
        if prev_ids is not None:
            if overlap >= DISCOVER_OVERLAP_STOP:
                stats["caught_up"] = True
                break
        else:
            quiet_pages = 0 if new_here else quiet_pages + 1
            if quiet_pages >= DAILY_STOP_AFTER:
                break
    return found, stats


def _discover_state_path() -> Path:
    return BASE_DIR / f".discover_state_immobiliare_{CITY_KEY}.json"


def load_discover_state() -> dict:
    """{"at": epoch of the last discover run, "ids": ids it read}."""
    try:
        return json.loads(_discover_state_path().read_text())
    except (OSError, ValueError):
        return {}


async def _slot_async(pages: int, area_slugs: list, slot_area: str, discover: bool,
                      max_rent: int, min_sqm: int, max_sqm: int, min_rooms: int,
                      delay: float, browser) -> tuple:
    """One spread-scan slot: discover (when due), then sweep `slot_area` in
    full. Returns (listings, skipped)."""
    found: dict = {}
    if discover:
        known_ids = set(load_seen_ids())
        prev = load_discover_state()
        found, dstats = await _discover_async(
            set(area_slugs), max_rent, min_sqm, max_sqm, min_rooms, delay, browser,
            known_ids, prev_ids=set(prev.get("ids") or []))
        ids = dstats.pop("ids")
        if dstats["pages"]:
            _discover_state_path().write_text(json.dumps({"at": time.time(), "ids": ids}))
        LAST_FETCH["discover"] = dstats
    if not slot_area:                      # discover between slots
        return list(found.values()), []
    print(f"\n    sweep: {slot_area}", end="", flush=True)
    swept, skipped = await _fetch_async(pages, [slot_area], max_rent, min_sqm,
                                        max_sqm, min_rooms, delay, browser,
                                        complete=LAST_FETCH["complete"],
                                        stats=LAST_FETCH["sweep"])
    for l in swept:
        found[l["id"]] = l
    return list(found.values()), skipped


async def _daily_async(pages: int, area_slugs: list, max_rent: int, min_sqm: int,
                       max_sqm: int, min_rooms: int, delay: float, browser,
                       known_ids: set, detailed_ids: set) -> tuple:
    """Discover → sweep → details (see DAILY_* above). Returns (listings, skipped)."""
    active = set(area_slugs)

    # 1. discover
    found, _ = await _discover_async(active, max_rent, min_sqm, max_sqm, min_rooms,
                                     delay, browser, known_ids)

    # 2. sweep
    sweep = sweep_slugs_for_today(area_slugs)
    print(f"\n    sweep: {', '.join(sweep)}", end="", flush=True)
    swept, skipped = await _fetch_async(pages, sweep, max_rent, min_sqm,
                                        max_sqm, min_rooms, delay, browser,
                                        complete=LAST_FETCH.get("complete"),
                                        stats=LAST_FETCH.get("sweep"))
    for l in swept:
        found[l["id"]] = l   # the sweep's copy carries the area it was swept in

    # 3. details — new listings first, then older ones never detailed
    todo = ([i for i in found if i not in known_ids]
            + [i for i in found if i in known_ids and i not in detailed_ids])
    todo = [i for i in todo if i not in detailed_ids][:DAILY_DETAIL_BUDGET]
    print(f"\n    details: {len(todo)} listing page(s)", end="", flush=True)
    n_ok = 0
    for lid in todo:
        try:
            tab = await browser.get(DETAIL_URL.format(id=lid))
            await asyncio.sleep(delay)
            raw = await timed(tab.evaluate("JSON.stringify(window.__NEXT_DATA__)"),
                              what="read detail __NEXT_DATA__")
            re_data = json.loads(raw)["props"]["pageProps"]["detailData"]["realEstate"]
            det = parse_rental({"realEstate": re_data})
        except PageTimeout:
            await browser.restart()
            continue
        except Exception:
            continue   # removed listing / challenge page — retried another day
        if det:
            listing = found[lid]
            listing.update({k: v for k, v in det.items() if v is not None and k != "_fetched_area"})
            listing["detail_fetched_at"] = str(date.today())
            n_ok += 1
    print(f" ({n_ok} ok)", end="", flush=True)
    return list(found.values()), skipped


def fetch_rentals(pages: int = 8, area_names: list = None, max_rent: int = 0,
                  min_sqm: int = 0, max_sqm: int = 0, min_rooms: int = 0,
                  delay: float = 2.5, mode: str = "full",
                  slot_area: str = None, discover: bool = False) -> tuple:
    """
    One-shot fetch of Milano rental listings using Microsoft Edge via nodriver.
    area_names: list of display names, e.g. ["Navigli", "Brera"].
                Each is converted to a URL slug and fetched as a separate URL series.
    Returns (listings, skipped_areas) — skipped_areas is a list of
    {"name": slug, "reason": str} for any area that was skipped.
    mode "slot" (spread scan): discover when `discover`, then sweep
    `slot_area` only, in a minimized window. LAST_FETCH tells the caller
    which areas were read in full.
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
        # Fix 3: many internal areas (Cadore, Porta Romana, Montenero) share
        # one Immobiliare macro-area (porta-romana-cadore-montenero).
        # Scanning each separately would 3x the requests AND fetch the same
        # page three times. Group them and scan each macro-area once.
        verify_slug_mapping(CITY_KEY, valid_slugs)
        grouped = group_by_immobiliare_slug(valid_slugs, CITY_KEY)
        area_slugs = list(grouped.keys())
        desc = ", ".join(area_slugs)
        print(f"  [scan] {len(area_names)} area(s) requested · "
              f"{n_valid} valid · {len(area_slugs)} unique Immobiliare slugs · "
              f"{n_skipped} skipped (bad slug)")
        # Surface the dedup so the user can see what's happening — e.g.
        # which three internal areas collapsed into one Immobiliare slug.
        collapsed = {im: ins for im, ins in grouped.items() if len(ins) > 1}
        if collapsed:
            for im_slug, ins in collapsed.items():
                print(f"  [scan]   {im_slug}  ←  {', '.join(ins)}")
        if pre_skipped:
            print(f"  [scan] Skipped: {', '.join(s['name'] for s in pre_skipped)}")
    else:
        desc = "all Milano"
        area_slugs = valid_slugs

    print(f"  Fetching rentals ({desc})...", end="", flush=True)

    LAST_FETCH.clear()
    LAST_FETCH.update({"complete": {}, "sweep": {}, "discover": None})

    async def _run():
        # Slots run all day: keep their window out of sight, like the drip.
        browser = BrowserSession("immobiliare", background=(mode == "slot"))
        await browser.start()
        try:
            if mode == "slot":
                items, skipped = await _slot_async(
                    pages, area_slugs, slot_area, discover, max_rent, min_sqm,
                    max_sqm, min_rooms, delay, browser)
            elif mode == "daily" and area_slugs:
                known_ids = set(load_seen_ids())
                detailed_ids = set()
                if OUTPUT_PATH.exists():
                    try:
                        detailed_ids = {l["id"] for l in json.loads(OUTPUT_PATH.read_text())
                                        if l.get("detail_fetched_at")}
                    except Exception:
                        pass
                items, skipped = await _daily_async(
                    pages, area_slugs, max_rent, min_sqm, max_sqm, min_rooms, delay,
                    browser, known_ids, detailed_ids)
            else:
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
    INLINE_ENRICH_CAP = 200

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
            # Multi-city: use lookup_for_city(..., city=CITY_KEY) instead of
            # the legacy Milan-only ZONES dict. The legacy lookup() still
            # works for Milan but returns nothing for other cities.
            _need_omi = [l for l in items if l.get("omi_loc_mid") is None
                         and l.get("latitude") and l.get("longitude")]
            if _need_omi:
                _omi_updates = []
                for _l in _need_omi:
                    _zone, _src = _omi_lookup.lookup_for_city(
                        float(_l["latitude"]),
                        float(_l["longitude"]),
                        city=CITY_KEY,
                    )
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

def score_all(raw: list, comps_pool: list | None = None) -> list:
    """Score *raw* listings and return them sorted by total score.

    comps_pool — optional larger pool used for the comps benchmark only.
    When omitted, *raw* itself is used as the pool (normal single-source case).
    Pass ``raw + existing_immo`` when scoring an Idealista batch so every
    listing can find neighbourhood comps from the already-stored Immobiliare
    data even before geo enrichment runs.
    """
    try:
        from scoring import score_all as _score_all
        from explain import explain_all
        scored = _score_all(raw, comps_pool=comps_pool)
        explain_all(scored)
        return scored
    except ImportError:
        pass
    pool = comps_pool if comps_pool is not None else raw
    scored = []
    for l in raw:
        s = score_rental(l, pool)
        scored.append({**l, **s})
    scored.sort(key=lambda x: x.get("score_total", 0) or 0, reverse=True)
    return scored


# ── Persistence helpers ────────────────────────────────────────────────────────

def load_seen_ids() -> dict:
    # Try the per-city file first (new format from migration 007), fall
    # back to the legacy shared seen_ids.json so an existing Milan repo
    # keeps its dedup history through the rename.
    paths = [SEEN_IDS_PATH]
    if CITY_KEY == "milano":
        paths.append(BASE_DIR / "seen_ids.json")
    for p in paths:
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, list):
                today = str(date.today())
                migrated = {str(id_): {"first_seen_date": today, "last_seen_date": today} for id_ in raw}
                print(f"  [migrate] {p.name}: migrated {len(migrated)} IDs to dict format")
                return migrated
            if p != SEEN_IDS_PATH:
                print(f"  [migrate] reading legacy {p.name} — will write to {SEEN_IDS_PATH.name}")
            return raw
        except Exception:
            continue
    return {}


def save_seen_ids(ids: dict):
    SEEN_IDS_PATH.write_text(json.dumps(ids, ensure_ascii=False))


# ── Sweep coverage ────────────────────────────────────────────────────────────
# "Absent N days" only means "gone" if the listing's area was actually swept
# in that time. Daily scans sweep 1/DAILY_SWEEP_DAYS of the areas, and the
# scanners can be down for weeks (Aug 29 → Oct 13): on 2026-10-14 the first
# daily scan trimmed ~12k listings — live ones included — from the 13 areas
# it hadn't reached yet. So stale-marking / trimming only judges listings
# last seen before every active area of their source completed a sweep.

def _coverage_path() -> Path:
    return BASE_DIR / f".sweep_coverage_{CITY_KEY}.json"


def record_sweeps(source: str, active_slugs: list, completed_slugs: list,
                  tried_slugs: list = ()) -> None:
    """Remember today's fully swept areas and the source's current area list
    (plus when each was last swept / tried, which orders the spread scan)."""
    path = _coverage_path()
    try:
        cov = json.loads(path.read_text())
    except (OSError, ValueError):
        cov = {}
    entry = cov.setdefault(source, {"areas": [], "swept": {}})
    entry["areas"] = sorted(set(active_slugs))
    today, now = str(date.today()), time.time()
    for slug in completed_slugs:
        entry["swept"][slug] = today
        entry.setdefault("swept_at", {})[slug] = now
    for slug in tried_slugs:
        entry.setdefault("tried_at", {})[slug] = now
    path.write_text(json.dumps(cov, indent=1))


def next_slot_area(source: str, area_slugs: list) -> str:
    """The area swept (or, after a failed try, tried) longest ago."""
    try:
        entry = json.loads(_coverage_path().read_text()).get(source) or {}
    except (OSError, ValueError):
        entry = {}

    def last(slug: str) -> float:
        at = entry.get("swept_at", {}).get(slug)
        if at is None and entry.get("swept", {}).get(slug):
            # Swept by the daily scan: date only → midday of that date.
            at = datetime.fromisoformat(entry["swept"][slug]).timestamp() + 12 * 3600
        return max(at or 0.0, entry.get("tried_at", {}).get(slug) or 0.0)

    return min(sorted(area_slugs), key=last)


def coverage_since() -> dict:
    """{source: oldest last-complete-sweep date across its active areas},
    or None for a source with any area not yet swept."""
    try:
        cov = json.loads(_coverage_path().read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for source, entry in cov.items():
        dates = [entry["swept"].get(a) for a in entry.get("areas", [])]
        out[source] = (date.fromisoformat(min(dates))
                       if dates and all(dates) else None)
    return out


# Idealista rental rows carry last_seen_date only since its scans began
# stamping it. Once every Idealista area has been swept since then, a row
# that still has no date was not seen in any of those sweeps: it counts as
# last seen when first seen (hidden), and as last seen the day before the
# stamping began for deletion (so trimming waits REMOVE_AFTER_DAYS from there
# and a mistake stays reversible). On 2026-10-17 that was 2,243 rows first
# seen April–August, visible for ever — 49 of them labelled deals.
IDEALISTA_DATED_SINCE = date(2026, 10, 14)


def _undated_idealista_seen(l: dict, covered: dict, for_trim: bool = False) -> "date | None":
    if (l.get("source") != "idealista" or covered.get("idealista") is None
            or covered["idealista"] < IDEALISTA_DATED_SINCE):
        return None
    cutoff = IDEALISTA_DATED_SINCE - timedelta(days=1)
    if for_trim:
        return cutoff
    try:
        first = datetime.fromisoformat(str(l.get("first_seen_date"))[:10]).date()
    except ValueError:
        return cutoff
    return min(first, cutoff)


def _judgeable(l: dict, last_seen: date, covered: dict) -> bool:
    """True when every active area of the listing's source has completed a
    sweep since it was last seen — i.e. its absence is real."""
    since = covered.get(l.get("source") or "immobiliare")
    return since is not None and last_seen < since


def mark_stale_listings(all_listings: list, today: date = None) -> int:
    """
    For every listing, compare last_seen_date to today and set
    `days_since_seen` + `is_stale` accordingly. Listings without a usable
    last_seen_date, or whose areas haven't all been swept since they were
    last seen (see _judgeable), keep their current is_stale.

    Returns the number of listings flagged as stale.
    """
    if today is None:
        today = date.today()
    covered = coverage_since()
    stale_count = 0
    for l in all_listings:
        last_seen = l.get("last_seen_date")
        try:
            seen_on = datetime.fromisoformat(last_seen).date() if last_seen else None
        except ValueError:
            seen_on = None
        if seen_on is None:
            seen_on = _undated_idealista_seen(l, covered)
        if seen_on is None:
            # No usable date and no sweep since dates began: keep the flag
            # as is — forcing False revived ~9k archived Idealista rows.
            l["days_since_seen"] = None
            l["is_stale"] = bool(l.get("is_stale"))
            stale_count += l["is_stale"]
            continue
        ds = (today - seen_on).days
        l["days_since_seen"] = ds
        if ((l.get("delisted_date") and (last_seen or "") <= l["delisted_date"])
                or (l.get("gone_checked") and (last_seen or "") <= l["gone_checked"])):
            # A scan of its area came back without it (write_output), or its
            # page says it's gone (listing_details): hidden until a scan sees
            # it again; deleted by the trim like any other.
            l["is_stale"] = True
            stale_count += 1
            continue
        if ds >= STALE_DAYS_THRESHOLD and not _judgeable(l, seen_on, covered):
            # Its area hasn't been re-swept yet: can't call it gone, but
            # don't revive it either. The rentals dashboard hides listings
            # only via is_stale — forcing False here re-showed every
            # archived / user-archived listing.
            l["is_stale"] = bool(l.get("is_stale"))
            stale_count += l["is_stale"]
            continue
        is_stale = ds >= STALE_DAYS_THRESHOLD
        l["is_stale"] = is_stale
        if is_stale:
            stale_count += 1
    return stale_count


def trim_stale_listings(all_listings: list, seen: dict,
                        today: date = None) -> list:
    """
    Drop listings that haven't been seen in REMOVE_AFTER_DAYS days. Also
    purges the corresponding entries from `seen` (mutated in place) so
    seen_ids.json doesn't grow forever either.

    Listings with no last_seen_date are kept (one-cycle grace).
    """
    if today is None:
        today = date.today()
    covered = coverage_since()
    keep, dropped = [], 0
    for l in all_listings:
        last_seen = l.get("last_seen_date")
        try:
            seen_on = datetime.fromisoformat(last_seen).date() if last_seen else None
        except ValueError:
            seen_on = None
        if seen_on is None:
            seen_on = _undated_idealista_seen(l, covered, for_trim=True)
        if seen_on is None:
            keep.append(l)
            continue
        if (today - seen_on).days < REMOVE_AFTER_DAYS or not _judgeable(l, seen_on, covered):
            keep.append(l)
        else:
            dropped += 1
            seen.pop(str(l.get("id", "")), None)
    if dropped:
        print(f"  [trim] removed {dropped} listings absent for {REMOVE_AFTER_DAYS}+ days")
    return keep


def _post_scan_stale_pass(seen: dict, today_str: str) -> None:
    """
    Read rentals_latest.json back in (after write_output merged the new scan),
    backfill `last_seen_date` from seen_ids onto every listing, run the
    stale-marking + trimming sweeps, and write the result back.

    This runs once per scan and operates on the FULL merged dataset, so
    listings that were absent from this scan (kept by the merger) get their
    is_stale / days_since_seen flags updated.
    """
    if not OUTPUT_PATH.exists():
        return
    try:
        all_listings = json.loads(OUTPUT_PATH.read_text())
    except Exception as exc:
        print(f"  [stale] could not read {OUTPUT_PATH}: {exc}")
        return

    today = datetime.fromisoformat(today_str).date()

    # Backfill last_seen_date onto every listing from the seen_ids index
    for l in all_listings:
        lid = str(l.get("id", ""))
        if lid in seen and seen[lid].get("last_seen_date"):
            l["last_seen_date"] = seen[lid]["last_seen_date"]
        # else: leave last_seen_date as None — grace pass (see mark_stale_listings)

    stale_n = mark_stale_listings(all_listings, today)
    before  = len(all_listings)
    all_listings = trim_stale_listings(all_listings, seen, today)

    OUTPUT_PATH.write_text(json.dumps(all_listings, ensure_ascii=False, indent=2))
    save_seen_ids(seen)   # persist any IDs trimmed from seen
    print(f"  [stale] {stale_n}/{before} listings flagged as stale "
          f"(absent {STALE_DAYS_THRESHOLD}+ days)")


def write_output(listings: list, source: str = "immobiliare",
                 swept_areas: set = None) -> dict:
    """Merge listings into rentals_latest.json, preserving entries from other sources.

    swept_areas: areas this scan read to their last page (`scan_area`
    values). A listing of such an area that didn't come back gets one more
    `missed_sweeps`; at DELIST_AFTER_MISSES it is hidden (de-listed). Only
    complete sweeps count: the old rule took every area any listing was
    fetched from, discover hits included, and only never fired because the
    area tag (_fetched_area) was stripped on write.

    Returns {"missed", "hidden", "revived", "distrusted"} counts.

    The entire read-modify-write block is serialized via fcntl.flock on
    a sidecar lock file. Without this, two scanners running concurrently
    (fetch_rentals + fetch_idealista, both writing the same merged file)
    raced and the later writer's "old other-source" snapshot stomped the
    earlier writer's freshly-added rows. This was eating ~all new
    Immobiliare listings on 2026-05-25 — they survived write_output but
    Idealista's next scan loaded a pre-write snapshot, then wrote it
    back as "the truth", silently deleting them.

    The api.py geo-enrich worker also writes this file; for that case
    we use _merge_by_id_with_disk inside api.py (the worker holds the
    file in memory too long for flock to be effective).
    """
    DASHBOARD_DIR.mkdir(exist_ok=True)

    # Cross-process write lock — coexists with the api.py geo-enrich
    # worker's merge-by-id approach. fcntl.LOCK_EX blocks other
    # write_output callers until we're done with the read-modify-write.
    import fcntl
    lock_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".lock")
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

        existing: list = []
        if OUTPUT_PATH.exists():
            try:
                existing = json.loads(OUTPUT_PATH.read_text())
            except Exception:
                pass

        new_ids = {l["id"] for l in listings}
        out = {"missed": 0, "hidden": 0, "revived": 0, "distrusted": []}
        # Price drop on the same listing between scans: keep the old rent
        # (previous_price / price_changed_date — the Supabase columns), and
        # carry an earlier drop forward while the price stays put.
        on_disk = {l["id"]: l for l in existing if l.get("source") in (source, None)}
        today_iso = str(date.today())
        for l in listings:
            old = on_disk.get(l["id"])
            if not old:
                continue
            if old.get("missed_sweeps"):
                out["revived"] += 1   # missed by a sweep, yet still online
            if not l.get("scan_area") and old.get("scan_area"):
                l["scan_area"] = old["scan_area"]   # found by discover: keep its area
            was, now = old.get("rent_mo"), l.get("rent_mo")
            if was and now and now < was:
                l["previous_price"], l["price_changed_date"] = was, today_iso
            elif l.get("previous_price") is None and old.get("previous_price") is not None:
                l["previous_price"] = old["previous_price"]
                l["price_changed_date"] = old.get("price_changed_date")
        # Areas whose "complete" sweep still brought back too few listings.
        trusted = set(swept_areas or ())
        if trusted:
            visible: dict = {}
            back: dict = {}
            for l in existing:
                a = l.get("scan_area")
                if l.get("source") in (source, None) and a in trusted and not l.get("is_stale"):
                    visible[a] = visible.get(a, 0) + 1
                    back[a] = back.get(a, 0) + (l["id"] in new_ids)
            for a, n in visible.items():
                if n >= 20 and back.get(a, 0) < DELIST_MIN_FOUND * n:
                    trusted.discard(a)
                    out["distrusted"].append(a)
                    print(f"  [stale]  {a}: sweep brought back {back.get(a, 0)}/{n} "
                          f"visible listings — not used to de-list")
        kept = []
        for l in existing:
            if l.get("source") in (source, None):
                if l["id"] in new_ids:
                    continue   # superseded by fresh version
                if l.get("scan_area") in trusted and not l.get("is_stale"):
                    # Its area was read in full and it didn't come back.
                    # Hidden, not deleted, and only after DELIST_AFTER_MISSES
                    # sweeps — a listing can slip between pages that shift
                    # mid-sweep; the next scan that sees it revives it, the
                    # 30-day trim deletes it otherwise.
                    l["missed_sweeps"] = (l.get("missed_sweeps") or 0) + 1
                    if l["missed_sweeps"] >= DELIST_AFTER_MISSES:
                        l["is_stale"] = True
                        l["delisted_date"] = today_iso
                        out["hidden"] += 1
                    else:
                        out["missed"] += 1
            kept.append(l)

        if out["hidden"] or out["missed"]:
            print(f"  [stale]  Hid {out['hidden']} de-listed listing(s) "
                  f"({DELIST_AFTER_MISSES} sweeps without them) · {out['missed']} missed once")

        merged = kept + listings

        # Dedup by ID — new listings (appended last) win
        seen_dedup: set = set()
        deduped = []
        for l in reversed(merged):
            if l["id"] not in seen_dedup:
                seen_dedup.add(l["id"])
                deduped.append(l)
        merged = list(reversed(deduped))

        # Dedup real-world duplicates: same property listed by multiple agencies
        # Keep earliest first_seen_date within each (lat,lon@3dp, rooms, sqm, rent_mo) group
        from collections import defaultdict as _dd
        geo_groups: dict = _dd(list)
        for i, l in enumerate(merged):
            lat, lon = l.get("latitude"), l.get("longitude")
            if lat is not None and lon is not None and l.get("rooms") and l.get("sqm") and l.get("rent_mo"):
                geo_groups[(round(lat, 3), round(lon, 3), l["rooms"], l["sqm"], l["rent_mo"])].append(i)
        remove_geo: set = set()
        for idxs in geo_groups.values():
            if len(idxs) > 1:
                idxs.sort(key=lambda i: (merged[i].get("first_seen_date") or "9999", str(merged[i].get("id", ""))))
                remove_geo.update(idxs[1:])
        if remove_geo:
            print(f"  [dedup]  Removed {len(remove_geo)} real-world duplicate listing(s)")
            merged = [l for i, l in enumerate(merged) if i not in remove_geo]

        merged.sort(key=lambda x: x.get("score_total", 0), reverse=True)
        clean = [{k: v for k, v in l.items() if k != "omi"} for l in merged]
        # Compact + null-strip so the snapshot stays under the 25 MiB Cloudflare cap.
        from dashboard_io import write_snapshot
        write_snapshot(OUTPUT_PATH, clean)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()
    return out


def rescore_output() -> None:
    """Rescore the whole listings file against the whole pool (archived
    rows, both sites). A scan scores only what it fetched — against itself
    for Immobiliare (a fraction of the city) — so without this the night's
    listings kept thin-pool benchmarks until something else rescored, and
    autosync pushed them online like that."""
    import fcntl
    lock_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".lock")
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            rows = json.loads(OUTPUT_PATH.read_text())
        except Exception as exc:
            print(f"  [rescore] skipped ({exc})")
            return
        t0 = time.time()
        scored = score_all(rows)
        clean = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
        from dashboard_io import write_snapshot
        write_snapshot(OUTPUT_PATH, clean)
        print(f"  [rescore] {len(clean)} listings rescored against the full pool in {time.time() - t0:.0f}s")


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

def _carry_over_detail_fields(raw: list) -> None:
    """Search results lack the detail-page fields (DETAIL_FIELDS); keep the
    ones already on disk so a re-scan doesn't wipe them, and age
    days_on_market from the stored publication date."""
    if not OUTPUT_PATH.exists():
        return
    try:
        on_disk = {l["id"]: l for l in json.loads(OUTPUT_PATH.read_text())}
    except Exception:
        return
    for l in raw:
        old = on_disk.get(l["id"])
        if not old:
            continue
        for k in DETAIL_FIELDS:
            if l.get(k) is None and old.get(k) is not None:
                l[k] = old[k]
        if l.get("days_on_market") is None and l.get("published_date"):
            try:
                l["days_on_market"] = (date.today() - date.fromisoformat(l["published_date"])).days
            except ValueError:
                pass


def _area_names(args) -> list:
    if args.areas:
        return [a.strip() for a in args.areas.split(",") if a.strip()]
    # No --areas flag: read active areas from area_settings.json
    return _load_active_areas()


def _macro_slugs_for(area_names: list) -> list:
    """Immobiliare macro-area slugs (what a sweep fetches) for area names."""
    return list(group_by_immobiliare_slug(
        [s for s in (to_url_slug(n) for n in area_names) if s], CITY_KEY).keys())


def run_once(args) -> list:
    """Execute one fetch-score-write cycle. Returns newly seen listings."""
    area_names = _area_names(args)
    raw, skipped_areas = fetch_rentals(
        pages=args.pages,
        area_names=area_names,
        max_rent=args.max_rent   or 0,
        min_sqm=args.min_sqm     or 0,
        max_sqm=args.max_sqm     or 0,
        min_rooms=args.min_rooms or 0,
        delay=args.delay,
        mode=args.scan,
    )
    if not raw:
        print("  ✗ No rentals fetched.")
        write_status(new_count=0, total_seen=len(load_seen_ids()),
                     skipped_areas=skipped_areas)
        return []

    _carry_over_detail_fields(raw)
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

    # Only areas read to their last page can show a listing has gone.
    complete = dict(LAST_FETCH.get("complete") or {})

    seen = load_seen_ids()
    today_str = str(date.today())
    for l in scored:
        lid = l["id"]
        l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str
        # Always stamp last_seen_date — we just saw this listing in *this* scan.
        l["last_seen_date"] = today_str

    write_output(scored, swept_areas=set(complete.values()) or None)

    new_listings = [l for l in scored if l["id"] not in seen]
    # Update seen_ids with both first_seen_date (preserved) and last_seen_date
    # (refreshed to today for every listing returned by this scan).
    for l in scored:
        seen[l["id"]] = {
            "first_seen_date": l["first_seen_date"],
            "last_seen_date":  today_str,
        }
    save_seen_ids(seen)

    # ── Post-merge: mark stale + trim too-old listings across the full file ──
    # Listings that didn't appear in *this* scan keep their old last_seen_date
    # from `seen`. We need to backfill last_seen_date onto every entry of
    # rentals_latest.json (not just `scored`) and then run mark+trim sweeps.
    record_sweeps("immobiliare", _macro_slugs_for(area_names), list(complete))
    _post_scan_stale_pass(seen, today_str)

    write_status(len(new_listings), len(seen), skipped_areas=skipped_areas)
    print(f"  ✓ {len(scored)} rentals written · {len(new_listings)} new")

    # (The Netlify deploy that used to follow is gone: the dashboard moved to
    # Cloudflare, every deploy failed with 401/403, and the online data comes
    # from Supabase anyway.)

    rescore_output()
    return new_listings


# ── Spread-scan slot ──────────────────────────────────────────────────────────

def _slot_log_path() -> Path:
    return BASE_DIR / f"scan_slots_{CITY_KEY}.jsonl"


def _digest_pending_path() -> Path:
    return BASE_DIR / f".digest_pending_{CITY_KEY}.json"


def run_slot(args, area: str | None, do_discover: bool) -> dict:
    """One spread-scan slot (see SPREAD_SCAN): discover when due, sweep
    `area`, merge, stale pass, rescore. area None: discover only (between
    slots) — written only when it found new listings. Appends its numbers
    to the slot log and returns them."""
    t0 = time.time()
    area_names = _area_names(args)
    raw, skipped = fetch_rentals(
        pages=args.pages, area_names=area_names,
        max_rent=args.max_rent or 0, min_sqm=args.min_sqm or 0,
        max_sqm=args.max_sqm or 0, min_rooms=args.min_rooms or 0,
        delay=args.delay, mode="slot", slot_area=area, discover=do_discover)
    complete = dict(LAST_FETCH.get("complete") or {})
    sweep = LAST_FETCH.get("sweep") or {}
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "source": "immobiliare",
           "kind": "slot" if area else "discover",
           "area": area, "complete": area in complete, "pages": sweep.get("pages", 0),
           "parse_failed": sweep.get("parse_failed", 0),
           "skipped": [s.get("reason") for s in skipped],
           "discover": LAST_FETCH.get("discover"),
           "found": len(raw), "found_sweep": sum(l.get("_via") == "sweep" for l in raw),
           "new": 0}
    seen = load_seen_ids()
    if not area and not any(l["id"] not in seen for l in raw):
        raw = []                           # discover found nothing new: nothing to write
    if raw:
        _carry_over_detail_fields(raw)
        scored = score_all(raw)
        today_str = str(date.today())
        for l in scored:
            lid = l["id"]
            l["first_seen_date"] = seen[lid]["first_seen_date"] if lid in seen else today_str
            l["last_seen_date"] = today_str
        rec.update(write_output(scored, swept_areas=set(complete.values()) or None))
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
    if area:
        record_sweeps("immobiliare", _macro_slugs_for(area_names), list(complete),
                      tried_slugs=[area])
    if raw:
        _post_scan_stale_pass(seen, str(date.today()))
    if area or raw:      # a discover with nothing new leaves the dashboard alone
        write_status(rec["new"], len(seen), skipped_areas=skipped)
    if raw:
        rescore_output()
    rec["seconds"] = round(time.time() - t0)
    with open(_slot_log_path(), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


class _PendingLock:
    """Both daemons (Immobiliare, Idealista) queue into and send from the
    same pending file."""
    def __enter__(self):
        import fcntl
        self.fh = open(_digest_pending_path().with_suffix(".lock"), "w")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *exc):
        self.fh.close()


def read_photos_after_slot(log, limit: int = PHOTOS_PER_SLOT) -> None:
    """Read the photos of up to `limit` top listings not read yet
    (photo_eval.nightly), then rescore if anything was stored. Skipped when
    another reading runs."""
    try:
        from photo_eval import nightly as _read
        if _read(log=lambda m: print("  " + m, flush=True), limit=limit):
            rescore_output()
    except Exception as exc:
        log(f"[photos] skipped: {exc}")


def _old_id_bar(rows: list, days: int = 3) -> dict:
    """{source: the listing id we already knew `days` ago — 99th percentile
    of their ids}. Ids grow over time on both sites, so a lower id was
    published before then. Not the max: a few rows carry an older
    first_seen_date than their id. Same rule as the dashboard
    (cards.js _listedEarlier)."""
    cut = (date.today() - timedelta(days=days)).isoformat()
    ids: dict = {}
    for l in rows:
        m = _re.search(r"\d+", str(l.get("id") or ""))
        if m and (l.get("first_seen_date") or "9") <= cut:
            ids.setdefault(l.get("source") or "immobiliare", []).append(int(m.group(0)))
    return {src: sorted(a)[int(0.99 * (len(a) - 1))] for src, a in ids.items()}


def _listed_earlier(l: dict, bar: dict) -> bool:
    """First seen lately but published before `bar` (see _old_id_bar) — a
    zone read further or a scan gap, not a new listing."""
    m = _re.search(r"\d+", str(l.get("id") or ""))
    b = bar.get(l.get("source") or "immobiliare")
    return bool(m and b and int(m.group(0)) < b)


def _queue_digest(ids: list) -> None:
    if not ids:
        return
    with _PendingLock():
        try:
            pending = json.loads(_digest_pending_path().read_text())
        except (OSError, ValueError):
            pending = []
        have = set(pending)
        _digest_pending_path().write_text(json.dumps(pending + [i for i in ids if i not in have]))


def send_pending_digest(args, log) -> None:
    """Spread scan: one digest a day, from DIGEST_HOUR, of every listing
    (either site) first seen since the previous one and still visible."""
    if datetime.now().hour < DIGEST_HOUR:
        return
    with _PendingLock():
        today = str(date.today())
        if DIGEST_SENT_PATH.exists() and DIGEST_SENT_PATH.read_text().strip() == today:
            return
        from email_digest import send_digest, load_config as load_email_config
        try:
            pending = set(json.loads(_digest_pending_path().read_text()))
        except (OSError, ValueError):
            pending = set()
        email_cfg = load_email_config()
        if (email_cfg.get("enabled", False) or args.email) and pending:
            all_rows = json.loads(OUTPUT_PATH.read_text())
            bar = _old_id_bar(all_rows)
            rows = [l for l in all_rows
                    if l.get("id") in pending and not l.get("is_stale") and not _listed_earlier(l, bar)]
            if rows:
                log(f"Sending digest for {len(rows)} new listing(s)…")
                send_digest(rows, email_cfg)
        mark_digest_sent()
        _digest_pending_path().unlink(missing_ok=True)


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
SCAN_PREFS_PATH     = BASE_DIR / "scan_prefs.json"


def _load_scan_prefs() -> dict:
    """Load scan preferences from scan_prefs.json (written by the dashboard)."""
    if SCAN_PREFS_PATH.exists():
        try:
            return json.load(open(SCAN_PREFS_PATH))
        except Exception:
            pass
    return {}


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

def _between_slots(args, source: str, next_slot: float, log, run_slot_fn, discover_state) -> None:
    """Until the next slot: discover every DISCOVER_EVERY_SEC (new listings
    within ~20 min) and read listing pages meanwhile, with a photo reading
    after each page session."""
    next_discover = (discover_state().get("at") or 0) + DISCOVER_EVERY_SEC
    while time.time() < next_slot:
        if time.time() >= next_discover:
            try:
                rec = run_marked(source, run_slot_fn, args, None, True)
                d = rec.get("discover") or {}
                log(f"Discover — {d.get('new', 0)} new in {d.get('pages', 0)} page(s)")
            except Exception as exc:
                log(f"✗ Discover error: {exc}")
            next_discover = time.time() + DISCOVER_EVERY_SEC
        wait_and_drip(source, min(next_slot, next_discover), log,
                      after_batch=lambda: read_photos_after_slot(log, PHOTOS_PER_BATCH))


def _spread_loop(args, log) -> None:
    """Spread scan (see SPREAD_SCAN): a slot every gap, the detail drip in
    between."""
    import traceback

    def _gap() -> int:
        n = len(_macro_slugs_for(_area_names(args))) or 1
        return SLOT_GAP_SEC or int(24 * 3600 / (n + 1))

    log(f"Daemon started — spread scan: one area every {_gap() // 60} min, "
        f"discover every {DISCOVER_EVERY_SEC / 3600:g} h, output {OUTPUT_PATH}")
    next_slot = last_scan_at("immobiliare") + _gap()
    while True:
        if time.time() < next_slot:
            log(f"Next slot at {datetime.fromtimestamp(next_slot):%Y-%m-%d %H:%M}")
            try:
                _between_slots(args, "immobiliare", next_slot, log, run_slot,
                               load_discover_state)
            except KeyboardInterrupt:
                log("Interrupted during sleep — stopping daemon.")
                raise
        areas = _macro_slugs_for(_area_names(args))
        area = next_slot_area("immobiliare", areas)
        do_discover = time.time() - (load_discover_state().get("at") or 0) >= DISCOVER_EVERY_SEC
        log(f"Slot: {area}{' + discover' if do_discover else ''}")
        ok, blocked = False, True
        try:
            rec = run_marked("immobiliare", run_slot, args, area, do_discover)
            ok = rec["complete"]
            # Back off only when the site pushed back — skipped / unreadable
            # pages or nothing at all — not for a sweep that merely can't
            # prove it reached the end (e.g. Idealista's full last page).
            blocked = bool(rec["skipped"] or rec["parse_failed"] or not rec["found"])
            d = rec.get("discover")
            log(f"Slot done — {area}: {rec['found']} listing(s), {rec['new']} new"
                + (f" (discover: {d['new']} new in {d['pages']} page(s))" if d else "")
                + f", {rec.get('hidden', 0)} hidden, {rec.get('missed', 0)} missed once"
                + ("" if ok else " · area NOT read in full — retried later"))
            mark_scan_done("immobiliare")
            read_photos_after_slot(log)
            send_pending_digest(args, log)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                log("Interrupted — stopping daemon.")
                raise
            log(f"✗ Slot error: {e}\n{traceback.format_exc()}")
        next_slot = time.time() + (2 * _gap() if blocked else _gap())


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

    if SPREAD_SCAN and args.scan == "daily":
        try:
            _spread_loop(args, log)
        finally:
            log_fh.close()
        return

    log(f"Daemon started — one scan every {DAEMON_INTERVAL_SEC // 3600} h, output {OUTPUT_PATH}")

    next_run = last_scan_at("immobiliare") + DAEMON_INTERVAL_SEC
    while True:
        if time.time() < next_run:
            log(f"Next scan at {datetime.fromtimestamp(next_run):%Y-%m-%d %H:%M}")
            try:
                wait_and_drip("immobiliare", next_run, log)
            except KeyboardInterrupt:
                log("Interrupted during sleep — stopping daemon.")
                log_fh.close()
                raise
        log("Running scan…")
        new_listings = []
        scan_ok = False
        try:
            new_listings = run_marked("immobiliare", run_once, args)

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
            scan_ok = True

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

        if scan_ok:
            mark_scan_done("immobiliare")
            next_run = time.time() + DAEMON_INTERVAL_SEC
        else:
            next_run = time.time() + DAEMON_RETRY_SEC


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch rental listings for an Italian city and score them against OMI rent benchmarks."
    )
    p.add_argument("--city",     type=str,   default="milano",
                   choices=list(CITY_REGISTRY.keys()),
                   help="City to scan (default: milano)")
    # 3-page default used to leave ~200 listings/macro unscraped on
    # dense Milan slugs (Vincenzo Monti, San Babila…), and those
    # listings then went stale in Supabase forever. 8 pages × ~25
    # listings = up to 200 per macro, which covers every active
    # listing on the densest slug we've measured. The empty-page
    # heuristic still short-circuits sparser slugs so we don't pay
    # 8 × delay for areas with only 2 pages of real results.
    p.add_argument("--pages",    type=int,   default=8,
                   help="Pages to fetch per macro-area (25 listings/page, default 8)")
    p.add_argument("--areas",    type=str,   default="",
                   help="Comma-separated area keywords, e.g. navigli,brera,isola")
    p.add_argument("--max-rent", type=int,   default=0,    help="Max monthly rent €")
    p.add_argument("--min-sqm",  type=int,   default=0,    help="Min surface m²")
    p.add_argument("--max-sqm",  type=int,   default=0,    help="Max surface m²")
    p.add_argument("--min-rooms",type=int,   default=0,    help="Min number of rooms")
    p.add_argument("--delay",    type=float, default=2.5,  help="Seconds between page loads (default 2.5)")
    p.add_argument("--scan",     choices=("full", "daily"), default=None,
                   help="full = every page of every active area; daily = discover new "
                        "+ rotating sweep + detail pages (default with --daemon)")
    p.add_argument("--daemon",   action="store_true",
                   help="Loop forever, fetching every 60 minutes")
    p.add_argument("--email",    action="store_true",
                   help="Send daily email digest (requires --daemon; see email_digest.py)")
    p.add_argument("--netlify", action="store_true",
                   help="After each scan, deploy dashboard/ straight to Netlify via the "
                        "Deploy API (requires netlify_config.json with site_id + token)")
    args = p.parse_args()
    if args.scan is None:
        args.scan = "daily" if args.daemon else "full"
    return args


def main():
    args = parse_args()
    # First thing — rebind every per-city module global so the rest of
    # the run (paths, URL slug, listings stamp) targets the right city.
    _apply_city(args.city)

    # Apply dashboard prefs as defaults when CLI args are at their zero/default values.
    # Explicit CLI overrides (e.g. --pages 10 --max-rent 2500) always win.
    _prefs = _load_scan_prefs()
    # If the user didn't pass --pages explicitly, respect any value
    # saved in scan_prefs.json; otherwise stick with the parser default
    # (8). The "is default" check has to compare against the same
    # number set in add_argument above — keep them in lock-step.
    if args.pages == 8:
        args.pages = int(_prefs.get("pages", 8))
    if not args.max_rent:
        args.max_rent = int(_prefs.get("max_rent", 0))
    if not args.min_rooms:
        args.min_rooms = int(_prefs.get("min_rooms", 0))
    if not args.min_sqm:
        args.min_sqm = int(_prefs.get("min_sqm", 0))
    if not args.max_sqm:
        args.max_sqm = int(_prefs.get("max_sqm", 0))

    print(f"\n{'─'*52}")
    print(f"  Immobiliare Scorer — rental fetch (Milano)")
    print(f"  Pages   : {args.pages} (~{args.pages * 25} listings max)")
    if args.areas:
        print(f"  Areas   : {args.areas}")
    if args.max_rent:
        print(f"  Max rent : €{args.max_rent}/mo")
    if args.min_rooms:
        print(f"  Min rooms: {args.min_rooms}+")
    print(f"  Mode    : {'daemon (every %d h)' % (DAEMON_INTERVAL_SEC // 3600) if args.daemon else 'one-shot'}")
    if args.netlify:
        print(f"  Netlify : enabled (→ direct deploy after each scan)")
    print(f"{'─'*52}\n")

    if args.daemon:
        daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
