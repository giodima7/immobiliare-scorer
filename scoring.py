#!/usr/bin/env python3
"""
scoring.py
──────────
Comps-first scoring model for Milano rental listings.
Imported by fetch_rentals.py, fetch_idealista.py, and api.py.

Formula
───────
  score_total = price_vs_comps×W_PRICE + property×W_PROP
                + location×W_LOC + penalty×W_PEN

  Defaults (overridden by scoring_settings.json):
    W_PRICE = 0.40   price vs local comparable listings
    W_PROP  = 0.30   physical / property quality
    W_LOC   = 0.20   location / geo score
    W_PEN   = 0.10   deal-breaker penalty (subtracted)

  penalty_score starts at 100 and counts DOWN; subtracted with W_PEN.

  Missing sub-scores fall back to 50 (neutral) so absent data does not
  penalise a listing.
"""

# ── Professional scoring reference ───────────────────────────────────────────
# property_score() and floor_score() implement the Italian professional
# Coefficienti di Merito standard for property valuation.
#
# Primary sources (all verified May 2026):
#   - FIMAA (Federazione Italiana Mediatori Agenti d'Affari)
#   - Tecnoborsa — Codice delle Valutazioni Immobiliari
#   - OMI Agenzia delle Entrate — quotazioni semestrali + Rapporto Immobiliare 2025
#   - myprojectcasa.it/blog/id/148 — coefficienti di merito tabelle
#   - quifinanza.it — coefficienti merito valore casa
#   - realadvisor.it — coefficienti di valutazione immobili
#
# Update annually or when market conditions shift significantly.
# Current calibration: Milan residential market, May 2026.

from __future__ import annotations
import json
import re as _re
import sys
from pathlib import Path
from re import compile as re_compile

from comps_engine import get_comps_benchmark
import omi_lookup


# ── Professional Coefficienti di Merito ─────────────────────────────────────
# Source: FIMAA / Tecnoborsa / OMI professional appraisal standard.
# These are percentage adjustments (as decimal fractions) applied to a
# neutral baseline of 50. Each coefficient has a fixed, predictable impact
# regardless of which other fields are populated.

# Floor coefficients: (with_lift_pct, without_lift_pct).
# Baseline (3° piano con ascensore) = 0%.
FLOOR_COEFFICIENTS: dict[str, tuple[float, float]] = {
    'interrato':     (-0.25, -0.25),
    'seminterrato':  (-0.20, -0.20),
    'terra':         (-0.10, -0.10),   # piano terra / rialzato
    'primo':         (-0.05, -0.15),
    'secondo':       (-0.03, -0.15),
    'terzo':         ( 0.00, -0.20),   # neutral with lift
    'quarto_quinto': ( 0.05, -0.25),
    'sesto_nono':    ( 0.10, -0.30),
    'attico':        ( 0.20, -0.20),
}

CONDITION_COEFFICIENTS: dict[str, float] = {
    'nuova_costruzione':  0.10,
    'ottimo':             0.10,
    'ristrutturato':      0.05,
    'buono':              0.00,   # baseline
    'abitabile':          0.00,   # baseline
    'da_ristrutturare':  -0.10,
    'fatiscente':        -0.25,
}

ENERGY_COEFFICIENTS: dict[str, float] = {
    'A4': 0.08, 'A3': 0.08,
    'A2': 0.06, 'A1': 0.06,
    'B':  0.04,
    'C':  0.02,
    'D':  0.00,   # baseline
    'E': -0.02,
    'F': -0.04,
    'G': -0.06,
}

HEATING_COEFFICIENTS: dict[str, float] = {
    'autonomous':            0.05,
    'centralizzato_valvole': 0.02,
    'none':                 -0.03,
    'unknown':               0.00,   # baseline — absence of data is neutral
}


# ── Location Desirability Index (city-relative) ──────────────────────────────
# LDI normalises a zone's purchase price within its CITY's range, not
# Italy's. 80/100 in Naples means "top 20% of Naples", not "top 20% of
# Italy" — otherwise every Roma listing would score artificially low
# against the Milano-calibrated yardstick.

# Per-city caches — populated lazily on first lookup for that city.
_LDI_CACHE: dict[str, dict[str, int]] = {}
_LDI_BOOST_THRESHOLDS: dict[str, float] = {}


def _build_ldi(city: str = "milano") -> dict[str, int]:
    """Normalise OMI compr_mid → 0-100 LDI within a single city's range."""
    try:
        zones = omi_lookup.load_city_zones(city)
    except FileNotFoundError:
        return {}
    values = {
        code: z["compr_mid"]
        for code, z in zones.items()
        if z.get("compr_mid") is not None
    }
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {code: 50 for code in values}
    return {code: round((v - lo) / (hi - lo) * 100) for code, v in values.items()}


def get_ldi(listing: dict) -> int:
    """Return LDI score (0–100) for the listing's city + OMI zone."""
    city = listing.get("city", "milano")
    if city not in _LDI_CACHE:
        _LDI_CACHE[city] = _build_ldi(city)
    return _LDI_CACHE[city].get(listing.get("omi_zona"), 50)


def _get_ldi_boost_threshold(city: str = "milano") -> float:
    """
    City-relative ceiling for the LDI bonus: zones in the TOP 20% of
    that city's price distribution don't get the boost.

    Old Milan-only hardcode was `omi_compr_mid > 5500` — that worked
    for Milan but would either disable the boost everywhere in Roma
    (max compr_mid is similar) or wrongly enable it everywhere in
    Napoli (max compr_mid is lower). The per-city percentile keeps
    the boost firing on city-relative bargains, not Italy-relative.
    """
    if city not in _LDI_BOOST_THRESHOLDS:
        try:
            zones = omi_lookup.load_city_zones(city)
        except FileNotFoundError:
            _LDI_BOOST_THRESHOLDS[city] = float("inf")
            return _LDI_BOOST_THRESHOLDS[city]
        mids = sorted(
            z["compr_mid"] for z in zones.values() if z.get("compr_mid")
        )
        if not mids:
            _LDI_BOOST_THRESHOLDS[city] = float("inf")
        else:
            # Top 20% start at the 80th-percentile index.
            idx = min(int(len(mids) * 0.80), len(mids) - 1)
            _LDI_BOOST_THRESHOLDS[city] = float(mids[idx])
    return _LDI_BOOST_THRESHOLDS[city]


# ── Appreciation rates (per-city, per-fascia) ────────────────────────────────
# Annual nominal appreciation used by the investor view's 5-yr projection.
# Mirrors the Supabase `cities` table (migration 007); keep in sync when
# the DB values change. Last verified: May 2026.
CITY_APPRECIATION_RATES: dict[str, dict[str, float]] = {
    "milano":       {"A": 0.030, "B": 0.025, "C": 0.020, "D": 0.015, "E": 0.010, "R": 0.010},
    "roma":         {"A": 0.025, "B": 0.020, "C": 0.015, "D": 0.010, "E": 0.008, "R": 0.008},
    "napoli":       {"A": 0.020, "B": 0.015, "C": 0.012, "D": 0.010, "E": 0.008, "R": 0.008},
    "la_maddalena": {"A": 0.015, "B": 0.010, "C": 0.008, "D": 0.005, "E": 0.005, "R": 0.005},
}


def get_appreciation_rate(listing: dict) -> float:
    """OMI-sourced annual appreciation rate for this listing's city+fascia."""
    city   = listing.get("city", "milano")
    fascia = (listing.get("omi_fascia") or "C").upper()
    rates  = CITY_APPRECIATION_RATES.get(city, CITY_APPRECIATION_RATES["milano"])
    return rates.get(fascia, rates.get("C", 0.020))


# ── Investment scenario constants ─────────────────────────────────────────────
# Used by the dashboard's 3×3 financing × contract matrix and by the
# investor verdict / card metrics. Python is the authoritative copy —
# the JS in index.html mirrors these numbers and MUST be updated in
# lock-step. The JS engine (computeScenario / computeAllScenarios /
# pickBestScenario) runs client-side so users can tweak assumptions
# in the detail view without a round-trip.
#
# Source: Tecnocasa 2024, OMI Rapporto 2025, Italian tax law
# (cedolare secca, Legge n. 431/1998). Last verified: May 2026.

FINANCING_PROFILES: dict[str, dict] = {
    "cash": {
        "ltv":            0.0,
        "rate":           0.0,
        "term_years":     0,
        "label_en":       "Cash buyer",
        "label_it":       "Acquisto in contanti",
        "description_en": "Full price paid upfront. No mortgage costs but maximum capital lock-up.",
        "description_it": "Prezzo pagato integralmente. Nessun costo mutuo ma massimo capitale impegnato.",
    },
    "mortgage_invest": {
        "ltv":            0.65,
        "rate":           0.042,
        "term_years":     20,
        "label_en":       "Investment mortgage",
        "label_it":       "Mutuo investimento",
        "description_en": "65% LTV, 4.2% rate, 20yr. Standard buy-to-let financing.",
        "description_it": "LTV 65%, tasso 4,2%, 20 anni. Finanziamento standard per investimento.",
    },
    "mortgage_primary": {
        "ltv":            0.80,
        "rate":           0.038,
        "term_years":     25,
        "label_en":       "Primary residence",
        "label_it":       "Prima casa",
        "description_en": "80% LTV, 3.8% rate, 25yr. Requires you live there 18mo+, lower tax, lower yield since you live in it.",
        "description_it": "LTV 80%, tasso 3,8%, 25 anni. Richiede residenza 18+ mesi, tasse ridotte, rendimento ridotto in quanto la abiti.",
    },
}

CONTRACT_PROFILES: dict[str, dict] = {
    "libero": {
        "rent_factor":    1.00,
        "cedolare_rate":  0.21,
        "imu_discount":   0.00,
        "vacancy_months": 1.0,
        "mgmt_pct":       0.05,
        "label_en":       "Canone libero (4+4)",
        "label_it":       "Canone libero (4+4)",
        "description_en": "4-year lease, full market rent, standard 21% cedolare tax.",
        "description_it": "Contratto 4+4 anni, canone di mercato, cedolare secca 21%.",
    },
    "concordato": {
        "rent_factor":    0.72,     # ~28% below libero on average
        "cedolare_rate":  0.10,
        "imu_discount":   0.25,
        "vacancy_months": 0.5,
        "mgmt_pct":       0.03,
        "label_en":       "Canone concordato (3+2)",
        "label_it":       "Canone concordato (3+2)",
        "description_en": "3+2 year lease, ~28% lower rent, 10% cedolare + 25% IMU discount.",
        "description_it": "Contratto 3+2 anni, canone ~28% inferiore, cedolare 10% + IMU -25%.",
    },
    "transitorio": {
        "rent_factor":    1.12,     # 10-15% above libero (often furnished)
        "cedolare_rate":  0.10,
        "imu_discount":   0.25,
        "vacancy_months": 2.0,      # higher vacancy due to short terms
        "mgmt_pct":       0.08,
        "label_en":       "Transitorio (1-18mo)",
        "label_it":       "Transitorio (1-18 mesi)",
        "description_en": "Short-term lease 1-18mo, ~12% higher rent, 10% cedolare. Higher vacancy and management cost.",
        "description_it": "Contratto breve 1-18 mesi, canone ~12% superiore, cedolare 10%. Maggior vacancy e costi gestione.",
    },
}

# Cities where canone concordato is signed under an accordo territoriale
# (high-tension housing markets). ANCI list 2024.
CONCORDATO_ELIGIBLE_CITIES: set[str] = {"milano", "roma", "napoli"}

# Zones suitable for transitorio (university districts, business hubs,
# central commuter corridors). Matched as case-insensitive substrings
# against omi_descr OR neighbourhood.
TRANSITORIO_ZONE_KEYWORDS: dict[str, list[str]] = {
    "milano": [
        "duomo", "brera", "cordusio", "turati", "moscova", "porta venezia",
        "cadorna", "cinque vie", "guastalla", "porta romana", "navigli",
        "porta genova", "sant'ambrogio", "bocconi", "porta vittoria",
        "citta' studi", "città studi", "lambrate", "isola", "porta nuova",
        "garibaldi", "lima", "loreto", "piola",
    ],
    "roma": [
        "centro storico", "trastevere", "monti", "esquilino", "san giovanni",
        "prati", "flaminio", "parioli", "san lorenzo", "pigneto",
        "sapienza", "tor vergata",
    ],
    "napoli": [
        "chiaia", "centro storico", "vomero", "posillipo", "fuorigrotta",
    ],
}

# Purchase-side closing costs as fraction of price.
#   • prima casa (residence): 2% registro + ~2% notary/agency = 4%
#   • seconda casa / pure investment: 9% registro + ~2% other  = 11%
PURCHASE_COSTS: dict[str, float] = {
    "mortgage_primary": 0.04,
    "cash":             0.11,
    "mortgage_invest":  0.11,
}


# ── Legacy module-level LDI (Milan-only) ──────────────────────────────────────
# Kept so unchanged callers (`scoring.LDI["B12"]`, gem/value helpers below)
# keep working. New code should call get_ldi(listing) which resolves the
# right city automatically.
LDI: dict[str, int] = _build_ldi("milano")


def is_hidden_gem(listing: dict, settings: dict | None = None) -> bool:
    """
    A Hidden Gem must be excellent on ALL dimensions simultaneously.
    No single strong signal compensates for weakness elsewhere.
    Thresholds are read from settings (or _DEFAULT_SETTINGS if not provided).

    Beyond the standard score gates this also enforces two physical
    sanity-checks that the abstract scores can miss:
      • metro within `gem_metro_max_m` (real walkability, not just a
        "well-connected fascia" inference from the location score)
      • at least `gem_min_sqm_per_room` of floor area per room (avoids
        crammed 45 m² 2-locale flats slipping in as "gems")
    """
    if settings is None:
        settings = _load_settings()
    delta = listing.get("comps_delta_pct")

    # Metro proximity gate. None = unknown; treat as failing the gate
    # because we can't prove the listing is actually walk-to-metro.
    metro_max = settings.get("gem_metro_max_m", 800)
    metro_dist = listing.get("metro_nearest_dist_m")
    if metro_max and (metro_dist is None or metro_dist > metro_max):
        return False

    # Cramped-flat gate. A 45 m² flat marketed as a 2-locale (≈22.5 m²
    # per room) is borderline; anything below the threshold is too tight
    # to be a "gem" no matter how cheap it is per m².
    min_spr = settings.get("gem_min_sqm_per_room", 0)
    sqm     = listing.get("sqm")
    rooms   = listing.get("rooms")
    if min_spr and sqm and rooms and rooms > 0 and (sqm / rooms) < min_spr:
        return False

    return (
        (listing.get("score_total") or 0)                                      >= settings.get("gem_total_min",      72)   and
        (listing.get("ldi_score") or 0)                                        >= settings.get("gem_ldi_min",         65)   and
        delta is not None and delta                                            <= settings.get("gem_delta_max",       -8.0) and
        (listing.get("score_property") or listing.get("score_physical") or 0) >= settings.get("gem_property_min",    50)   and
        (listing.get("score_location") or 0)                                   >= settings.get("gem_location_min",    45)   and
        (listing.get("comps_confidence") or 0)                                 >= settings.get("gem_confidence_min",  40)   and
        (listing.get("score_penalty") or 0)                                    >= settings.get("gem_penalty_min",     70)
    )


def is_good_value(listing: dict, settings: dict | None = None) -> bool:
    """
    Great Value = solid listing, below market, decent area.
    Weaker than Hidden Gem on every dimension but still genuinely good.
    Thresholds are read from settings (or _DEFAULT_SETTINGS if not provided).
    """
    if settings is None:
        settings = _load_settings()
    delta = listing.get("comps_delta_pct")
    return (
        not is_hidden_gem(listing, settings)                                       and
        (listing.get("score_total") or 0)                                      >= settings.get("gv_total_min",      65)   and
        (listing.get("ldi_score") or 0)                                        >= settings.get("gv_ldi_min",         45)   and
        delta is not None and delta                                            <= settings.get("gv_delta_max",       -5.0) and
        (listing.get("score_property") or listing.get("score_physical") or 0) >= settings.get("gv_property_min",    40)   and
        (listing.get("score_location") or 0)                                   >= settings.get("gv_location_min",    35)   and
        (listing.get("comps_confidence") or 0)                                 >= settings.get("gv_confidence_min",  30)   and
        (listing.get("score_penalty") or 0)                                    >= settings.get("gv_penalty_min",     55)
    )


# ── Load scoring settings ────────────────────────────────────────────────────

_SETTINGS_PATH = Path(__file__).parent / "scoring_settings.json"

_DEFAULT_SETTINGS: dict = {
    "w_price":          0.45,   # price is the primary signal
    "w_property":       0.25,   # good property matters less if overpriced
    "w_location":       0.20,
    "w_penalty":        0.10,
    # price_vs_comps thresholds
    "price_great":     -0.15,   # ≤ −15% → score 100
    "price_neutral_lo": -0.03,  # −3% to +5% → score 50 (neutral zone)
    "price_neutral_hi":  0.05,
    "price_bad":         0.20,  # ≥ +20% → score 0
    # €-impact adjustment
    "euro_impact_small":  75,   # |monthly diff| < €75 → reduce penalty 30%
    "euro_impact_large": 200,   # |monthly diff| > €200 → full penalty
    # penalty thresholds
    "penalty_dom_stale": 60,    # days-on-market > this → stale penalty
    "penalty_dom_warn":  30,
    # floor + elevator scoring
    "floor_lift_bonus":     28,
    "floor_nolift_penalty": -15,
    # comps engine
    "radii":    [500, 800, 1200],
    "min_comps": 5,
    # Hidden Gem badge thresholds
    # Tightened 2026-05-20 — the old bar let a 45 m² 1-bed 10 min from
    # the nearest metro pass as a "gem" purely on a -18 % comps delta.
    # Hidden Gem now also requires walkable metro + non-cramped layout.
    "gem_total_min":      80,
    "gem_ldi_min":        70,
    "gem_delta_max":      -12.0,
    "gem_property_min":   62,
    "gem_location_min":   62,
    "gem_confidence_min": 60,
    "gem_penalty_min":    75,
    "gem_metro_max_m":    800,     # ≈ 10-min walk; None disables the gate
    "gem_min_sqm_per_room": 22,    # crammed-flat filter
    # Great Value badge thresholds
    "gv_total_min":       70,
    "gv_ldi_min":         50,
    "gv_delta_max":       -7.0,
    "gv_property_min":    50,
    "gv_location_min":    50,
    "gv_confidence_min":  45,
    "gv_penalty_min":     60,
}


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            overrides = json.load(f)
        return {**_DEFAULT_SETTINGS, **overrides}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


# ── Floor parsing ────────────────────────────────────────────────────────────

_FLOOR_MAP: dict[str, int] = {
    # Piano terra / ground
    'T': 0, 'PT': 0, 'TERRA': 0, 'PIANO TERRA': 0,
    'P.T.': 0, 'PT.': 0, 'RDC': 0, 'GROUND': 0, 'G': 0,
    # Piano rialzato / raised ground
    'R': 1, 'RIALZATO': 1, 'PR': 1, 'PIANO RIALZATO': 1,
    # Seminterrato (first below-ground level)
    'S': -1, 'S1': -1, 'SEMI': -1, 'SEMINTERRATO': -1,
    # Interrato / sottosuolo (deeper below ground — cap at -2)
    'I': -2, 'INTERRATO': -2, 'SOTTOSUOLO': -2,
    'S2': -2, 'S3': -2, 'S4': -2, 'S5': -2,
}
_RIALZATO_TOKENS: frozenset[str] = frozenset({'R', 'RIALZATO', 'PR', 'PIANO RIALZATO'})
_RE_FLOOR_NUM = re_compile(r'-?\d+')


def _floor_token(tok: str) -> tuple[int | None, bool]:
    """Parse one floor token. Returns (floor_n, is_rialzato)."""
    t = tok.strip().upper()
    if t in _FLOOR_MAP:
        return _FLOOR_MAP[t], t in _RIALZATO_TOKENS
    m = _RE_FLOOR_NUM.search(t)
    if m:
        return int(m.group()), False
    return None, False


def parse_floor(raw_floor) -> tuple[int | None, str | None]:
    """
    Parse an Immobiliare.it floor field into (floor_n, floor_label).

    Handles:
      - Single codes: T→0, R→1, S→-1, I→-2, digits
      - Sub-basement codes: S2/S3/S4/S5 → -2
      - Compound ranges:  'S, 3' → -1 | '4 - 5' → 4 | 'T, R' → 0
      - Dict with 'abbreviation' key (API floor object)

    Returns (None, None) when unparseable.
    The floor_n for compound strings is the minimum floor (most conservative).
    """
    if raw_floor is None:
        return None, None
    if isinstance(raw_floor, dict):
        raw_floor = raw_floor.get('abbreviation') or raw_floor.get('value') or ''
    s = str(raw_floor).strip()
    if not s or s.upper() in ('NONE', 'N/A', ''):
        return None, None

    # Split compound strings: "S, 3" → ["S","3"], "4 - 5" → ["4","5"]
    import re as _re2
    tokens = _re2.split(r',\s*|\s+[-–]\s+', s)
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        return None, None

    parsed: list[int] = []
    has_rialzato = False
    for tok in tokens:
        fn, is_r = _floor_token(tok)
        if fn is not None:
            parsed.append(fn)
        if is_r:
            has_rialzato = True

    if not parsed:
        return None, None

    floor_n = min(parsed)   # most conservative: lowest floor in compound range

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


# ── Floor scoring ─────────────────────────────────────────────────────────────

def floor_score(listing: dict) -> float:
    """
    Returns the floor coefficient as a decimal percentage adjustment
    (e.g. -0.10 for piano terra, +0.20 for attico with lift).

    Based on Italian professional Coefficienti di Merito standard
    (FIMAA / Tecnoborsa / OMI, 2026). Baseline (3° piano con ascensore) = 0.00.

    Called by property_score() only. penalty_score() now owns its own
    below-ground deal-breaker deductions (those serve a different purpose
    — binary disqualifiers rather than quality gradations).
    """
    floor_n  = listing.get('floor_n')
    elevator = listing.get('elevator')   # True / False / None
    has_lift = elevator is True

    if floor_n is None:
        return 0.00   # unknown floor — neutral, no assumption

    # ── Floor bucket ─────────────────────────────────────────────────────────
    # piano terra (0) and rialzato/1st (1) are treated identically per the
    # professional standard (both -10% regardless of lift).
    if floor_n <= -2:
        bucket = 'interrato'
    elif floor_n == -1:
        bucket = 'seminterrato'
    elif floor_n <= 1:
        bucket = 'terra'
    elif floor_n == 2:
        bucket = 'secondo'
    elif floor_n == 3:
        bucket = 'terzo'
    elif floor_n <= 5:
        bucket = 'quarto_quinto'
    elif floor_n <= 9:
        bucket = 'sesto_nono'
    else:
        # 10° piano and above treated as attico/ultimo piano
        bucket = 'attico'

    coeff_with, coeff_without = FLOOR_COEFFICIENTS[bucket]

    if elevator is None:
        # Lift unknown — blend the two coefficients, biased toward
        # "no lift" for high floors (more conservative).
        if floor_n > 3:
            return coeff_without * 0.6 + coeff_with * 0.4
        return (coeff_with + coeff_without) / 2.0

    return coeff_with if has_lift else coeff_without


# ── Energy class → numeric ───────────────────────────────────────────────────

ENERGY_SCORE = {
    "A4": 10, "A3": 9, "A2": 8, "A1": 7,
    "B":   6, "C":  5, "D":  4, "E":  3, "F": 2, "G": 1,
}


def _energy_numeric(energy_class) -> int | None:
    if not energy_class:
        return None
    return ENERGY_SCORE.get(str(energy_class).strip().upper())


# ── Physical / property score (0–100) ───────────────────────────────────────

def property_score(listing: dict, settings: dict | None = None) -> int:
    """
    Score physical quality 0–100 using Italian Coefficienti di Merito.

    Architecture: start from neutral baseline 50, apply percentage
    adjustments for each known attribute. Each coefficient has a fixed
    impact regardless of data coverage — no max_pos normalisation, so a
    +10% condition bonus moves the score by the same amount whether the
    listing has 3 known fields or 13.

    Source: FIMAA / Tecnoborsa / OMI professional appraisal standard.

    Returns 50 (neutral) when no physical fields are present. Clamped 0–100.
    """
    if settings is None:
        settings = {}

    # Scale factor: 1 percentage point of adjustment → SCALE score points.
    # Max positive sweep (attico+lift +20, ottimo +10, balcony +4, box +4,
    # external +5, A3 +8, autonomous heat +5, furnished +8, bathrooms +3) ≈ +67%
    # Max negative sweep (interrato -25, fatiscente -25, G -6, no-heat -3) ≈ -59%
    # SCALE 1.5 → +67×1.5 = +100.5 (clamped to 100), -59×1.5 = -88.5 (clamped to 0)
    SCALE = 1.5

    n_known = 0
    total_adjustment = 0.0
    cond_key: str | None = None   # captured for the age × condition rule below

    # ── Floor coefficient ────────────────────────────────────────────────────
    if listing.get('floor_n') is not None:
        n_known += 1
        total_adjustment += floor_score(listing)

    # ── Condition ────────────────────────────────────────────────────────────
    cond_raw = (listing.get('condition') or '').lower().strip()
    if cond_raw:
        n_known += 1
        # Order matters: check 'da_ristrutturare' BEFORE 'ristrutturat' since
        # the substring 'ristruttur' is contained in both.
        if 'fatiscente' in cond_raw:
            cond_key = 'fatiscente'
        elif ('da_ristrutturare' in cond_raw
              or ('da ' in cond_raw and 'ristruttur' in cond_raw)):
            cond_key = 'da_ristrutturare'
        elif 'ottim' in cond_raw:
            cond_key = 'ottimo'
        elif 'nuov' in cond_raw or 'costruz' in cond_raw:
            cond_key = 'nuova_costruzione'
        elif 'ristruttur' in cond_raw:
            cond_key = 'ristrutturato'
        elif 'buon' in cond_raw:
            cond_key = 'buono'
        elif 'abitabil' in cond_raw:
            cond_key = 'abitabile'
        else:
            cond_key = 'buono'   # unknown phrasing → neutral
        total_adjustment += CONDITION_COEFFICIENTS.get(cond_key, 0.0)

    # ── Building age × condition interaction ─────────────────────────────────
    yr = listing.get('year_built')
    if yr and isinstance(yr, (int, float)) and yr > 1900:
        n_known += 1
        age = 2026 - int(yr)
        is_good_cond = cond_key in ('ottimo', 'nuova_costruzione', 'ristrutturato')
        is_poor_cond = cond_key in ('da_ristrutturare', 'fatiscente')
        if   age > 40 and is_good_cond:    total_adjustment += 0.05   # well-maintained historical
        elif 20 < age <= 40 and is_good_cond: total_adjustment += 0.02
        elif age > 40 and is_poor_cond:    total_adjustment -= 0.05   # old AND needs work
        # otherwise no extra age adjustment (condition already captures)

    # ── Exposure / luminosity ────────────────────────────────────────────────
    ext = listing.get('is_external')
    if ext is not None:
        n_known += 1
        if ext is True:
            total_adjustment += 0.05   # external facing (proxy for sud/est/ovest)

    # ── Energy class ─────────────────────────────────────────────────────────
    ec_raw   = (listing.get('energy_class') or '').strip().upper()
    ec_coeff = ENERGY_COEFFICIENTS.get(ec_raw)
    if ec_coeff is not None:
        n_known += 1
        total_adjustment += ec_coeff

    # ── Balcony / terrazza ────────────────────────────────────────────────────
    balc = listing.get('has_balcony')
    if balc is not None:
        n_known += 1
        if balc is True:
            total_adjustment += 0.04

    # ── Box / garage / posto auto (averaged at +4%) ──────────────────────────
    park = listing.get('has_parking')
    if park is not None:
        n_known += 1
        if park is True:
            total_adjustment += 0.04

    # ── Heating ──────────────────────────────────────────────────────────────
    ht_raw = (listing.get('heating_type') or '').lower().strip()
    if ht_raw:
        n_known += 1
        if 'autonomous' in ht_raw or 'autonomo' in ht_raw:
            total_adjustment += HEATING_COEFFICIENTS['autonomous']
        elif 'centraliz' in ht_raw and ('valvol' in ht_raw or 'termost' in ht_raw):
            total_adjustment += HEATING_COEFFICIENTS['centralizzato_valvole']
        elif 'none' in ht_raw or ht_raw in ('no', 'assente'):
            total_adjustment += HEATING_COEFFICIENTS['none']
        # other centralised → 0 (baseline)

    # ── Furnished (rental market signal — not a formal coefficient) ──────────
    furn = listing.get('furnished')
    if furn is not None:
        n_known += 1
        if furn is True:
            total_adjustment += 0.08

    # ── Bathrooms (quality signal — not a formal coefficient) ────────────────
    baths = listing.get('bathrooms')
    if baths is not None:
        n_known += 1
        if baths >= 2:
            total_adjustment += 0.03

    # ── Photo count (listing-quality proxy — penalty only) ───────────────────
    photos = listing.get('photo_count')
    if photos is not None:
        n_known += 1
        if photos < 5:
            total_adjustment -= 0.05

    # ── Days on market (staleness penalty — not a property coefficient) ──────
    dom = listing.get('days_on_market')
    if dom is not None:
        n_known += 1
        if   dom > 60:  total_adjustment -= 0.07
        elif dom > 30:  total_adjustment -= 0.03

    # ── Condominium fees (cost penalty — not a property coefficient) ─────────
    rent_mo = listing.get('rent_mo') or listing.get('price') or 0
    spese   = listing.get('spese_condominiali') or listing.get('condominium_fees') or 0
    if rent_mo and spese and rent_mo > 0:
        n_known += 1
        if   spese > rent_mo * 0.20:  total_adjustment -= 0.10
        elif spese > rent_mo * 0.12:  total_adjustment -= 0.04

    # ── Room efficiency / monolocale size (layout-quality penalty) ───────────
    _sqm   = listing.get('sqm')   or 0
    _rooms = listing.get('rooms') or 0
    if _sqm > 0:
        n_known += 1
        if _rooms >= 2:
            _eff = _sqm / _rooms
            if   _eff < 10:
                total_adjustment -= 0.20
                listing['_room_efficiency_flag'] = 'severe'
            elif _eff < 14:
                total_adjustment -= 0.10
                listing['_room_efficiency_flag'] = 'tight'
            else:
                listing['_room_efficiency_flag'] = None
        else:
            # Monolocale / studio — penalise on absolute sqm
            if   _sqm < 30:
                total_adjustment -= 0.25
                listing['_room_efficiency_flag'] = 'micro_studio'
            elif _sqm < 38:
                total_adjustment -= 0.15
                listing['_room_efficiency_flag'] = 'small_studio'
            elif _sqm < 45:
                total_adjustment -= 0.08
                listing['_room_efficiency_flag'] = 'compact_studio'
            else:
                listing['_room_efficiency_flag'] = None
    else:
        listing['_room_efficiency_flag'] = None

    # ── No data — return neutral ─────────────────────────────────────────────
    if n_known == 0:
        return 50

    # ── Apply to baseline and clamp ──────────────────────────────────────────
    raw = 50.0 + (total_adjustment * 100 * SCALE)
    return max(0, min(100, round(raw)))


# ── Price-vs-comps score (0–100) ─────────────────────────────────────────────

def price_vs_comps_score(
    delta_pct: float,
    rent_mo: float,
    blended_median: float,
    sqm: float,
    settings: dict,
) -> float:
    """
    Asymmetric piecewise score.

      delta ≤ GREAT  → 100
      GREAT < delta < NEUTRAL_LO → linear 100→50
      NEUTRAL_LO ≤ delta ≤ NEUTRAL_HI → 50
      NEUTRAL_HI < delta < BAD → linear 50→0
      delta ≥ BAD → 0

    Then apply €-impact adjustment:
      if |monthly_diff| < euro_impact_small → soften penalty by 30%
      if |monthly_diff| > euro_impact_large → full penalty (no change)
      between → linear interpolation
    """
    g   = settings["price_great"]
    nlo = settings["price_neutral_lo"]
    nhi = settings["price_neutral_hi"]
    bad = settings["price_bad"]

    if delta_pct <= g:
        raw = 100.0
    elif delta_pct <= nlo:
        raw = 100.0 - (delta_pct - g) / (nlo - g) * 50.0
    elif delta_pct <= nhi:
        raw = 50.0
    elif delta_pct <= bad:
        raw = 50.0 - (delta_pct - nhi) / (bad - nhi) * 50.0
    else:
        raw = 0.0

    # €-impact adjustment (only softens penalty, not bonus)
    if raw < 50.0 and sqm > 0 and blended_median > 0 and rent_mo > 0:
        monthly_diff = abs(rent_mo - blended_median * sqm)
        small = settings["euro_impact_small"]
        large = settings["euro_impact_large"]
        if monthly_diff < small:
            factor = 0.70
        elif monthly_diff > large:
            factor = 1.00
        else:
            factor = 0.70 + 0.30 * (monthly_diff - small) / (large - small)
        deficit = 50.0 - raw
        raw = 50.0 - deficit * factor

    return max(0.0, min(100.0, raw))


# ── Penalty score (starts at 100, counts DOWN) ───────────────────────────────

def penalty_score(listing: dict, settings: dict) -> int:
    """
    Deal-breaker combinations.  Starts at 100 (clean); deductions applied.
    The result is MULTIPLIED by w_penalty in the composite formula, so a
    lower number hurts more.

    NOTE: floor quality is now handled entirely by property_score() via
    FLOOR_COEFFICIENTS. This function only handles floor-based DEAL-BREAKERS
    (interrato/seminterrato hard caps, high-floor-no-lift accessibility)
    that are binary disqualifiers rather than quality gradations.

    Returns int 0–100.
    """
    score   = 100
    sqm     = listing.get("sqm") or 0
    rooms   = listing.get("rooms") or 0
    dom     = listing.get("days_on_market") or 0
    rent_mo = listing.get("rent_mo") or 0
    spese   = listing.get("spese_condominiali") or 0
    floor_n  = listing.get("floor_n")
    elevator = listing.get("elevator")

    # ── Below-ground / ground-floor deal-breaker penalties ──────────────────
    # Separate from the property_score quality coefficient. These represent a
    # deal-breaker signal: most tenants/buyers won't tolerate
    # interrato/seminterrato regardless of price discount.
    if floor_n is not None:
        if floor_n <= -2:
            score -= 55   # interrato — near-disqualifying
        elif floor_n == -1:
            score -= 40   # seminterrato — severe but not total disqualifier
        elif floor_n == 0:
            score -= 20   # ground floor — meaningful drawback

    # ── High-floor-no-lift accessibility deal-breakers ──────────────────────
    # property_score already applied the -20% / -30% quality coefficient.
    # This adds a FURTHER deal-breaker on extreme cases — physical
    # accessibility, not just quality.
    if floor_n is not None and elevator is False:
        if floor_n >= 5:
            score -= 25   # 5th+ floor, confirmed no lift — serious deal-breaker
        elif floor_n >= 4:
            score -= 15   # 4th floor, confirmed no lift — significant

    # ── Size mismatch: very small for rooms declared ────────────────────────
    if sqm > 0 and rooms > 0 and sqm / rooms < 12:
        score -= 15

    # ── Very stale listing ──────────────────────────────────────────────────
    stale = settings.get("penalty_dom_stale", 60)
    warn  = settings.get("penalty_dom_warn",  30)
    if dom > stale:
        score -= 25
    elif dom > warn:
        score -= 10

    # ── Condominium fees eating > 25% of rent ───────────────────────────────
    if rent_mo > 0 and spese > rent_mo * 0.25:
        score -= 15

    return max(0, score)


# ── Price ceiling ─────────────────────────────────────────────────────────────

def apply_price_ceiling(score_total: float, comps_delta_pct: float) -> int:
    """
    Caps score_total for listings priced above the comps median.
    The more overpriced, the lower the ceiling.
    comps_delta_pct is in percent (e.g. 8.3 for +8.3% above comps).
    """
    if comps_delta_pct <= 0:
        return round(score_total)          # no ceiling for at or below comps
    elif comps_delta_pct <= 5:
        ceiling = 72                       # mildly above — still can be decent
    elif comps_delta_pct <= 10:
        ceiling = 62                       # clearly above — capped at below-average
    elif comps_delta_pct <= 20:
        ceiling = 50                       # significantly above — mediocre at best
    else:
        ceiling = 38                       # very overpriced — poor score regardless
    return min(round(score_total), ceiling)


# Minimum legitimate sale €/m² by city. Anything below this is almost
# certainly an auction (asta giudiziaria), a data error, or a mislabelled
# rental price. Calibrated from the OMI compr_min across each city's
# zones with safety margin so floor sales don't false-positive.
#   milano       — OMI min in D/E fascia is ~1000; 800 = safe margin
#   roma         — OMI min ~750 in periphery; 600 = safe margin
#   napoli       — OMI min ~500 in deep periphery; 400 = safe margin
#   la_maddalena — tourist market, no floor sales below ~€1000/m²
# Default 400 catches anything unmistakably wrong without rejecting
# legitimately cheap stock in cities we haven't tuned yet.
MIN_SALE_PSQM: dict[str, int] = {
    "milano":       800,
    "roma":         600,
    "napoli":       400,
    "la_maddalena": 800,
    "default":      400,
}


def apply_price_floor_gate(listing: dict) -> bool:
    """
    Returns True if `listing` should be excluded for impossibly low
    €/m². Catches three flavours of rubbish:
      • judicial auctions that slipped past the auction-flag detector
      • monthly-rental prices scraped into the sales feed (e.g. €30/m²)
      • plain data errors (price stored as the down payment, etc.)

    Mutates the listing dict in place with diagnostic fields so the
    Settings → Data Quality panel can surface why a row got dropped.
    """
    ask_psqm = listing.get("ask_psqm") or 0
    sqm      = listing.get("sqm")      or 0
    price    = listing.get("price")    or 0
    if not ask_psqm or not sqm or not price:
        return False

    city     = (listing.get("city") or "milano").lower()
    min_psqm = MIN_SALE_PSQM.get(city, MIN_SALE_PSQM["default"])
    if ask_psqm >= min_psqm:
        return False

    listing["_price_floor_gate_applied"] = True
    listing["_price_floor_reason"] = (
        f"€{ask_psqm:.0f}/m² below minimum €{min_psqm}/m² for {city}"
    )
    listing["_excluded"] = True
    return True


# ── Extreme-underpricing suspicion flag ──────────────────────────────────
# A legitimate listing is rarely more than ~40% below comps. Beyond -60%
# the probability of fraud / data error / undetected fake spikes. We
# don't auto-exclude — genuine distressed sales exist — but cap the
# score at 55 and strip gem badges so the row never lands at the top
# of the user's grid. Dashboard surfaces a "verify this listing"
# banner via the _extreme_underpricing flag.
EXTREME_UNDERPRICING_THRESHOLD_PCT: float = -60.0
EXTREME_UNDERPRICING_CAP:           int   = 55


def apply_extreme_underpricing_flag(listing: dict, comps_delta_pct: float | None) -> None:
    """
    Mutate `listing` if comps_delta_pct is below the extreme threshold.
    Caps score_total at EXTREME_UNDERPRICING_CAP, clears hidden_gem /
    good_value, sets _extreme_underpricing + _extreme_underpricing_delta
    so the dashboard can render a warning banner.

    Idempotent: re-running on an already-flagged listing is a no-op.
    """
    if comps_delta_pct is None:
        return
    try:
        dp = float(comps_delta_pct)
    except (TypeError, ValueError):
        return
    if dp >= EXTREME_UNDERPRICING_THRESHOLD_PCT:
        return
    listing["_extreme_underpricing"]       = True
    listing["_extreme_underpricing_delta"] = dp
    if (listing.get("score_total") or 0) > EXTREME_UNDERPRICING_CAP:
        listing["score_total"] = EXTREME_UNDERPRICING_CAP
    # Strip gem badges — suspicious listings should never read as gems.
    listing["hidden_gem"] = False
    listing["good_value"] = False


def apply_absolute_value_gate(score_total: float, ask_psqm: float, sqm: float) -> tuple[int, bool]:
    """
    Hard score ceiling for small, very expensive listings.

    Prevents overpriced micro-flats from scoring highly even when they look
    attractive vs. local comps — comps in premium zones are themselves expensive.
    Only activates for listings < 60 m².

    Tiers (first match wins):
      ask_psqm ≥ 7 000                  → ceiling 55  (any small flat at this price)
      ask_psqm ≥ 5 800 AND sqm ≤ 58     → ceiling 65  (e.g. €6 154/m² at 52 m²)
      ask_psqm ≥ 5 000 AND sqm ≤ 45     → ceiling 70  (tiny flat, still expensive)

    Returns (final_score, gate_applied).
    """
    if ask_psqm <= 0 or sqm <= 0 or sqm >= 60:
        return round(score_total), False

    if ask_psqm >= 7000:
        ceiling = 55
    elif ask_psqm >= 5800 and sqm <= 58:
        ceiling = 65
    elif ask_psqm >= 5000 and sqm <= 45:
        ceiling = 70
    else:
        return round(score_total), False

    capped = min(round(score_total), ceiling)
    applied = capped < round(score_total)
    return capped, applied


# ── Corporate / short-term rental detection ─────────────────────────────────
#
# Some agencies in Milan systematically charge a premium for furnished /
# short-term / serviced apartments. The price is fair *for that product*
# (flexible contracts, all-inclusive, business travelers) but it's NOT a
# bargain for a standard long-term renter. We flag these so they don't sneak
# into Hidden Gem / Good Value badges and we cap their composite score.

CORPORATE_RENTAL_AGENCIES: set[str] = {
    'spacest', 'spacest.com', 'roomless',
    'milano monolocali',
    'dovevivo',
    'homy', 'homy.it',
    'serviced apartment',
    'corporate housing',
    'short let',
    'short stay',
    'temporary milano',
    'affitti brevi',
    'milano short stay',
    'urban campus',
    'the social hub',
    'aparto',
    'camplus',
    'collegium',
    'milano studio rent',
    'milano transfer',
    'milano luxury',
    'tempoaffitti',
    'safestays',
    'housinganywhere',
    'the best rent',
}


_CORPORATE_OVERRIDE_IDS: set[str] | None = None

def _load_corporate_overrides() -> set[str]:
    """
    Load `corporate_overrides.json` once. The file lets the user manually
    flag listings as corporate when the scrapers fail to capture agency_name
    (Idealista currently has 0 % agency-name coverage). Format:
        { "ids": ["id_35604324", "id_xxxxxxxx", ...] }
    Lookups are case-sensitive and match the listing's `id` field exactly.
    """
    global _CORPORATE_OVERRIDE_IDS
    if _CORPORATE_OVERRIDE_IDS is not None:
        return _CORPORATE_OVERRIDE_IDS
    try:
        from pathlib import Path
        import json as _json
        p = Path(__file__).parent / "corporate_overrides.json"
        if p.exists():
            data = _json.loads(p.read_text())
            ids = data.get("ids") if isinstance(data, dict) else data
            _CORPORATE_OVERRIDE_IDS = set(map(str, ids or []))
        else:
            _CORPORATE_OVERRIDE_IDS = set()
    except Exception:
        _CORPORATE_OVERRIDE_IDS = set()
    return _CORPORATE_OVERRIDE_IDS


def is_corporate_rental(listing: dict) -> bool:
    """
    True when:
      - the listing's agency name matches a known corporate operator, OR
      - the listing's id appears in `corporate_overrides.json` (manual list
        for cases where the scraper missed the agency name).
    """
    lid = str(listing.get("id", ""))
    if lid and lid in _load_corporate_overrides():
        return True
    agency = (listing.get('agency_name') or '').lower().strip()
    if not agency:
        return False
    return any(corp in agency for corp in CORPORATE_RENTAL_AGENCIES)


def has_corporate_rental_signals(listing: dict) -> bool:
    """
    Detect corporate rental from listing description / title text patterns.
    Requires 2+ signals to fire (one alone is too noisy).
    Operates on `description` and `title` if either is present — both fields
    are optional in our scrape, so this is a no-op when text is missing.
    """
    desc  = (listing.get('description') or '').lower()
    title = (listing.get('title')       or '').lower()
    text  = desc + ' ' + title
    if not text.strip():
        return False
    signals = (
        'minimo mesi', 'massimo mesi',
        'medium term', 'medium-term',
        'short term',  'short-term',
        'corporate',   'serviced',
        'all inclusive', 'all-inclusive',
        'utenze incluse', 'wifi incluso',
        'mensile', 'monthly stay',
        'business travelers', 'digital nomad',
        'minimo 1 mese', 'da 1 mese',
        'temporary',
    )
    hits = sum(1 for s in signals if s in text)
    return hits >= 2


def compute_effective_rent(listing: dict) -> dict:
    """
    Real monthly cost: base rent + condo fees + bundled utilities.
    Field-name shims:
      base_rent  ← `rent_mo` (preferred) or `price`
      condo      ← `spese_condominiali` (Italian) or `condominium_fees`
      utilities  ← `utilities_mo` (rare in our scrape)
    Caps absurd condo values at 15 % of base rent (anything higher is
    almost always a data-entry error in the scrape).
    """
    try:
        base_rent = float(listing.get('rent_mo') or listing.get('price') or 0) or 0
    except (TypeError, ValueError):
        base_rent = 0
    try:
        condo = float(listing.get('spese_condominiali')
                      or listing.get('condominium_fees') or 0) or 0
    except (TypeError, ValueError):
        condo = 0
    try:
        utilities = float(listing.get('utilities_mo') or 0) or 0
    except (TypeError, ValueError):
        utilities = 0

    if base_rent > 0 and condo > base_rent * 0.30:
        condo = base_rent * 0.15

    effective = base_rent + condo + utilities
    sqm = listing.get('sqm') or 0
    try:
        sqm = float(sqm)
    except (TypeError, ValueError):
        sqm = 0
    return {
        'effective_rent_mo':   round(effective, 2) if effective else None,
        'effective_psqm_rent': round(effective / sqm, 2) if (effective and sqm) else None,
        'condo_pct_of_rent':   round(condo / base_rent * 100, 1) if base_rent else 0,
    }


CORPORATE_CEILING = 75


def compute_effective_psqm(listing: dict) -> float:
    """
    Real €/m²/month including mandatory condo fees. Caps condo at 20 % of
    rent so a stray data error doesn't blow up the comps comparison.
    Falls back to the listing's existing `ask_psqm` when rent or sqm are
    unavailable.
    """
    try:
        rent = float(listing.get("rent_mo") or listing.get("price") or 0) or 0
    except (TypeError, ValueError):
        rent = 0
    try:
        sqm = float(listing.get("sqm") or 0) or 0
    except (TypeError, ValueError):
        sqm = 0
    if not rent or not sqm:
        try:
            return float(listing.get("ask_psqm") or 0) or 0
        except (TypeError, ValueError):
            return 0
    try:
        condo = float(listing.get("spese_condominiali")
                      or listing.get("condominium_fees") or 0) or 0
    except (TypeError, ValueError):
        condo = 0
    condo_capped = min(condo, rent * 0.20)
    return round((rent + condo_capped) / sqm, 2)


def get_condo_fee_flag(listing: dict) -> str | None:
    """
    Flag when condo fees significantly inflate the effective rent.
      ≥ 20 % of rent  → 'high_condo_fees'
      ≥ 12 % of rent  → 'elevated_condo_fees'
      otherwise       → None
    """
    try:
        rent = float(listing.get("rent_mo") or listing.get("price") or 0) or 0
    except (TypeError, ValueError):
        rent = 0
    try:
        condo = float(listing.get("spese_condominiali")
                      or listing.get("condominium_fees") or 0) or 0
    except (TypeError, ValueError):
        condo = 0
    if rent <= 0 or condo <= 0:
        return None
    pct = condo / rent
    if pct >= 0.20:
        return "high_condo_fees"
    if pct >= 0.12:
        return "elevated_condo_fees"
    return None


# ── Composite score ───────────────────────────────────────────────────────────

def score_rental(listing: dict, all_listings: list, settings: dict | None = None) -> dict:
    """
    Score a single rental listing.

    Comps benchmark is built from all_listings using comps_engine.
    Returns a dict of score fields to be merged into the listing.
    """
    if settings is None:
        settings = _load_settings()

    # Fake/foreign-property bait — same short-circuit as score_sale_listing.
    # Mutate in place + return an empty dict; score_all merges {**l, **s}
    # so the mutation survives.
    if listing.get("is_fake"):
        listing["score_total"]      = 0
        listing["hidden_gem"]       = False
        listing["good_value"]       = False
        listing["_excluded"]        = True
        listing["_excluded_reason"] = "Fake / foreign-property bait listing"
        return {}

    w_price = settings.get("w_price", 0.40)
    w_prop  = settings.get("w_property", 0.30)
    w_loc   = settings.get("w_location", 0.20)
    w_pen   = settings.get("w_penalty", 0.10)

    ask_psqm = listing.get("ask_psqm") or 0
    rent_mo  = listing.get("rent_mo")  or 0
    sqm      = listing.get("sqm")      or 0

    # ── Sub-scores ─────────────────────────────────────────────────────────────
    prop_s = property_score(listing, settings)
    geo_s  = listing.get("geo_score")
    loc_s  = geo_s if geo_s is not None else 50
    pen_s  = penalty_score(listing, settings)

    # ── LDI (computed once — used for boost and labelling) ─────────────────────
    ldi_score = get_ldi(listing)
    # Multiplicative bonus: 0 % at LDI≤50, up to +10 % at LDI=100
    ldi_bonus = max(0.0, (ldi_score - 50) / 50 * 0.10)

    # ── Comps benchmark ────────────────────────────────────────────────────────
    comps = get_comps_benchmark(
        listing, all_listings,
        radii=tuple(settings.get("radii", [500, 800, 1200])),
        min_comps=int(settings.get("min_comps", 5)),
    )

    delta_pct      = comps.get("delta_pct")
    blended_median = comps.get("blended_median")
    confidence     = comps.get("confidence", 0)

    if delta_pct is not None and blended_median is not None and blended_median > 0:
        pvc_s = price_vs_comps_score(delta_pct, rent_mo, blended_median, sqm, settings)
        delta_label_pct = round(delta_pct * 100, 1)
        # Log extremes for debugging
        if pvc_s in (0.0, 100.0):
            print(
                "[score-debug] "
                f"id={listing.get('id', '?')} "
                f"ask_psqm={ask_psqm} "
                f"blended_median={blended_median} "
                f"delta_pct={delta_pct:.3f} "
                f"pvc_score={pvc_s:.1f} "
                f"n_comps={comps['n_comps']} "
                f"source={comps['benchmark_source']}",
                file=sys.stderr,
            )
    else:
        pvc_s = 50.0          # neutral when no benchmark
        delta_label_pct = None

    # ── LDI boost (only for bargains in non-premium zones) ─────────────────────
    # A bargain in a prime area deserves a reward; an overpriced one does not.
    # The "premium" cutoff is the 80th-percentile compr_mid IN THE LISTING'S
    # CITY — was hardcoded 5500 for Milan, now city-relative so Naples /
    # La Maddalena get a sensible threshold too.
    _omi_compr_r = listing.get("omi_compr_mid") or 0
    _boost_max_r = _get_ldi_boost_threshold(listing.get("city", "milano"))
    if delta_pct is not None and delta_pct <= 0 and _omi_compr_r <= _boost_max_r:
        boosted_price_score = round(min(100.0, pvc_s * (1 + ldi_bonus)))
    else:
        boosted_price_score = round(pvc_s)   # above comps, no comps, or premium zone: no boost

    # ── Composite ──────────────────────────────────────────────────────────────
    total_raw = round(
        boosted_price_score * w_price
        + prop_s  * w_prop
        + loc_s   * w_loc
        + pen_s   * w_pen
    )
    total_raw = max(0, min(100, total_raw))

    # Property quality gate: sparse/poor property data caps the total at 75
    if prop_s < 40:
        total_raw = min(total_raw, 75)

    # Price ceiling: overpriced listings cannot score above a ceiling regardless
    # of how good the property or location is.
    if delta_label_pct is not None:
        total = apply_price_ceiling(total_raw, delta_label_pct)
    else:
        total = total_raw

    # Below-ground hard cap: seminterrato/interrato cannot score above 55
    # regardless of price or location — the habitat is fundamentally compromised.
    _fn = listing.get("floor_n")
    if _fn is not None and _fn < 0:
        total = min(total, 55)

    # Absolute value gate: overpriced small flats in premium zones
    total, _gate_applied = apply_absolute_value_gate(total, ask_psqm, sqm)
    listing["_absolute_value_gate_applied"] = bool(_gate_applied)

    # Corporate / short-term rental detection — cap at 75 and disqualify badges.
    _is_corp = is_corporate_rental(listing) or has_corporate_rental_signals(listing)
    listing["_is_corporate_rental"] = bool(_is_corp)
    listing["_corporate_ceiling_applied"] = False
    if _is_corp and total > CORPORATE_CEILING:
        listing["_corporate_ceiling_applied"] = True
        total = CORPORATE_CEILING

    # Effective rent (base + condo + bundled utilities) — what the renter
    # actually pays each month, useful when condo fees are non-trivial.
    _eff = compute_effective_rent(listing)
    listing["effective_rent_mo"]   = _eff["effective_rent_mo"]
    listing["effective_psqm_rent"] = _eff["effective_psqm_rent"]
    listing["condo_pct_of_rent"]   = _eff["condo_pct_of_rent"]

    score_was_capped = total < total_raw

    # ── Labels ─────────────────────────────────────────────────────────────────
    if delta_pct is not None:
        # delta_label_pct already computed above
        if abs(delta_label_pct) < 0.5:
            comps_label = "at comps median"
        elif delta_label_pct > 0:
            comps_label = f"+{delta_label_pct:.1f}% above comps"
        else:
            comps_label = f"{delta_label_pct:.1f}% below comps"
    else:
        comps_label = None

    # ── Confidence label ───────────────────────────────────────────────────────
    if confidence >= 70:
        conf_label = "High"
    elif confidence >= 40:
        conf_label = "Medium"
    else:
        conf_label = "Low"

    # Keep backward-compat vs_omi fields for listings that have them
    omi_mid   = listing.get("omi_loc_mid")
    omi_fascia = listing.get("omi_fascia") or "B"
    if omi_mid and omi_mid > 0 and ask_psqm > 0:
        vs_omi_pct = round((ask_psqm - omi_mid) / omi_mid * 100, 1)
        if abs(vs_omi_pct) < 0.5:
            vs_omi_label = "at OMI"
        elif vs_omi_pct > 0:
            vs_omi_label = f"+{vs_omi_pct:.1f}% above OMI"
        else:
            vs_omi_label = f"{vs_omi_pct:.1f}% below OMI"
    else:
        vs_omi_pct   = None
        vs_omi_label = None

    # Suggested rent from OMI, adjusted for size + condition.
    # Without these coefficients a 30 m² studio in da_ristrutturare and a
    # 90 m² ottimo flat in the same zone would suggest the same €/m²/mo —
    # which is wrong. _surface_coeff and _condition_coeff are the same
    # multipliers score_sale_listing uses for estimated_yield_pct.
    suggested_rent_mo   = None
    suggested_rent_psqm = None
    if omi_mid and omi_mid > 0 and sqm and sqm > 0:
        surf_c = _surface_coeff(sqm)
        cond_c = _condition_coeff(listing.get("condition", ""))
        adjusted_psqm = omi_mid * surf_c * cond_c
        suggested_rent_mo   = int(round(adjusted_psqm * sqm / 25) * 25)
        suggested_rent_psqm = round(adjusted_psqm, 1)

    # ── Hidden Gem / Great Value flags ────────────────────────────────────────
    _gate_fired = listing.get("_absolute_value_gate_applied", False)
    _is_corp_l  = listing.get("_is_corporate_rental", False)
    _loc_s_r = round(loc_s)
    _hidden_gem = (
        not _gate_fired                                                        # gate = not a gem
        and not _is_corp_l                                                     # corporate = not a gem
        and total          >= settings.get("gem_total_min",      72)
        and ldi_score  >= settings.get("gem_ldi_min",         65)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gem_delta_max",       -8.0)
        and prop_s     >= settings.get("gem_property_min",    50)
        and _loc_s_r   >= settings.get("gem_location_min",    45)
        and confidence >= settings.get("gem_confidence_min",  40)
        and pen_s      >= settings.get("gem_penalty_min",     70)
    )
    _good_value = not _hidden_gem and (
        not _gate_fired                                                        # gate = not good value
        and not _is_corp_l                                                     # corporate = not good value
        and total          >  settings.get("gv_total_min",        65)         # strictly > ceiling
        and ldi_score  >= settings.get("gv_ldi_min",          45)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gv_delta_max",        -5.0)
        and prop_s     >= settings.get("gv_property_min",     40)
        and _loc_s_r   >= settings.get("gv_location_min",     35)
        and confidence >= settings.get("gv_confidence_min",   30)
        and pen_s      >= settings.get("gv_penalty_min",      55)
    )

    return {
        # Comps fields
        "comps_median":          comps.get("median"),
        "comps_p40":             comps.get("p40"),
        "comps_p60":             comps.get("p60"),
        "comps_n":               comps.get("n_comps", 0),
        "comps_radius_m":        comps.get("radius_used"),
        "comps_source":          comps.get("benchmark_source"),
        "comps_confidence":      confidence,
        "comps_conf_label":      conf_label,
        "comps_delta_pct":       delta_label_pct,
        "comps_label":           comps_label,
        "comps_condition_group": comps.get("condition_group"),
        "comps_adjusted":        bool(comps.get("adjusted")),
        # Sub-scores
        "score_price":         round(pvc_s),
        "score_property":      prop_s,
        "score_location":      round(loc_s),
        "score_penalty":       pen_s,
        "score_geo":           geo_s,
        "score_total":         total,
        # LDI + boost + ceiling
        "ldi_score":           ldi_score,
        "ldi_bonus":           round(ldi_bonus, 3),   # fraction 0–0.10
        "boosted_price_score": boosted_price_score,
        "score_was_capped":    score_was_capped,
        # Gem flags
        "hidden_gem":          _hidden_gem,
        "good_value":          _good_value,
        # Back-compat fields (kept for dashboard display / OMI context)
        "vs_omi_pct":          vs_omi_pct,
        "vs_omi_rent_pct":     vs_omi_pct,
        "vs_omi_label":        vs_omi_label,
        "suggested_rent_mo":   suggested_rent_mo,
        "suggested_rent_psqm": suggested_rent_psqm,
        "omi_fallback":        comps.get("benchmark_source") in ("omi_only", "none"),
        # Legacy names (score_physical / score_rent kept for dashboard compat)
        "score_physical":      prop_s,
        "score_rent":          round(pvc_s),
        "score_value":         None,
        "score_fascia":        None,
    }


# ── Surface / condition coefficients (needed for estimated yield) ────────────

def _surface_coeff(sqm: int) -> float:
    if sqm < 50:    return 1.20
    if sqm <= 85:   return 1.00
    if sqm <= 115:  return 0.90
    if sqm <= 145:  return 0.82
    return 0.75


def _condition_coeff(condition: str) -> float:
    c = (condition or "").lower()
    if any(k in c for k in ("ristrutturato", "ottimo", "nuovo", "eccellente")):
        return 1.00
    if any(k in c for k in ("da ristrutturare", "fatiscente")):
        return 0.70
    return 0.85


# ── Sale penalty score ───────────────────────────────────────────────────────

def penalty_score_sale(listing: dict, settings: dict) -> int:
    """
    Sale-specific penalty score (starts at 100, counts down).
    Identical to the rental version plus one sale-specific deduction:
      ask_psqm > omi_compr_max × 1.3  → −20 pts (significantly above OMI ceiling)
    """
    score = penalty_score(listing, settings)

    # Sale-specific: significantly above OMI purchase ceiling
    ask_psqm    = listing.get("ask_psqm") or 0
    omi_compr_max = listing.get("omi_compr_max")
    if ask_psqm > 0 and omi_compr_max and ask_psqm > omi_compr_max * 1.3:
        score -= 20

    return max(0, score)


# ── Hidden Gem / Good Value — sale versions ───────────────────────────────────

def is_sale_hidden_gem(listing: dict, settings: dict | None = None) -> bool:
    if settings is None:
        settings = _load_settings()
    delta = listing.get("comps_sale_delta_pct")
    return (
        (listing.get("score_total") or 0)                                      >= settings.get("gem_total_min",      72)   and
        (listing.get("ldi_score") or 0)                                        >= settings.get("gem_ldi_min",         65)   and
        delta is not None and delta                                            <= settings.get("gem_delta_max",       -8.0) and
        (listing.get("score_property") or listing.get("score_physical") or 0) >= settings.get("gem_property_min",    50)   and
        (listing.get("score_location") or 0)                                   >= settings.get("gem_location_min",    45)   and
        (listing.get("comps_sale_confidence") or 0)                            >= settings.get("gem_confidence_min",  40)   and
        (listing.get("score_penalty") or 0)                                    >= settings.get("gem_penalty_min",     70)
    )


def is_sale_good_value(listing: dict, settings: dict | None = None) -> bool:
    if settings is None:
        settings = _load_settings()
    delta = listing.get("comps_sale_delta_pct")
    return (
        not is_sale_hidden_gem(listing, settings)                                  and
        (listing.get("score_total") or 0)                                      >= settings.get("gv_total_min",      65)   and
        (listing.get("ldi_score") or 0)                                        >= settings.get("gv_ldi_min",         45)   and
        delta is not None and delta                                            <= settings.get("gv_delta_max",       -5.0) and
        (listing.get("score_property") or listing.get("score_physical") or 0) >= settings.get("gv_property_min",    40)   and
        (listing.get("score_location") or 0)                                   >= settings.get("gv_location_min",    35)   and
        (listing.get("comps_sale_confidence") or 0)                            >= settings.get("gv_confidence_min",  30)   and
        (listing.get("score_penalty") or 0)                                    >= settings.get("gv_penalty_min",     55)
    )


# ── Composite sale score ──────────────────────────────────────────────────────

def score_sale_listing(listing: dict, all_listings: list, settings: dict | None = None) -> dict:
    """
    Score a single sale listing.

    Mirrors score_rental() but uses:
      • ask_psqm as purchase €/m² (not rent €/m²/month)
      • omi_compr_mid as OMI anchor (not omi_loc_mid)
      • comps_sale_* field names in output
      • sale-specific penalty (OMI ceiling breach)
      • estimated_yield_pct as informational field
    """
    if settings is None:
        settings = _load_settings()

    # Short-circuit gates — three flavours of listings that should never
    # carry a score: judicial auctions, nuda-proprietà sales (buyer can't
    # actually use the property), and sub-floor prices (mislabelled
    # rentals / data errors). All three set `_excluded=True` so the
    # dashboard's applySaleFilters hides them via one consolidated check.
    # The row still gets emitted (rather than returning None) so the
    # Data-Quality panel can surface what got rejected.
    def _excluded_zero(reason: str) -> dict:
        listing["score_total"]    = 0
        listing["score_price"]    = 0
        listing["score_property"] = 0
        listing["score_location"] = 0
        listing["score_penalty"]  = 0
        listing["hidden_gem"]     = False
        listing["good_value"]     = False
        listing["_excluded"]      = True
        listing["_excluded_reason"] = reason
        return listing

    if listing.get("is_fake"):
        return _excluded_zero("Fake / foreign-property bait listing")
    if listing.get("is_nuda_proprieta"):
        return _excluded_zero("Nuda proprietà — usufruct retained by seller")
    if listing.get("is_auction"):
        return _excluded_zero("Auction listing (asta giudiziaria)")
    if apply_price_floor_gate(listing):
        # apply_price_floor_gate already sets _excluded + _price_floor_reason
        return _excluded_zero(listing.get("_price_floor_reason")
                              or "Below per-city €/m² floor")

    w_price = settings.get("w_price",    0.45)
    w_prop  = settings.get("w_property", 0.25)
    w_loc   = settings.get("w_location", 0.20)
    w_pen   = settings.get("w_penalty",  0.10)

    ask_psqm = listing.get("ask_psqm") or 0
    price    = listing.get("price")    or 0
    sqm      = listing.get("sqm")      or 0

    # ── Sub-scores ──────────────────────────────────────────────────────────────
    prop_s = property_score(listing, settings)
    geo_s  = listing.get("geo_score")
    loc_s  = geo_s if geo_s is not None else 50
    pen_s  = penalty_score_sale(listing, settings)

    # ── LDI ─────────────────────────────────────────────────────────────────────
    ldi_score = get_ldi(listing)
    ldi_bonus = max(0.0, (ldi_score - 50) / 50 * 0.10)

    # ── Comps benchmark (sale mode) ─────────────────────────────────────────────
    comps = get_comps_benchmark(
        listing, all_listings,
        radii=tuple(settings.get("radii", [500, 800, 1200])),
        min_comps=int(settings.get("min_comps", 5)),
        mode='sale',
    )

    delta_pct      = comps.get("delta_pct")
    blended_median = comps.get("blended_median")
    confidence     = comps.get("confidence", 0)

    if delta_pct is not None and blended_median is not None and blended_median > 0:
        # For sales the "monthly amount" concept doesn't apply — we use the
        # total purchase price difference for the €-impact adjustment.
        # Pass price as rent_mo and blended_median*sqm as the blended total
        # so the existing asymmetric formula works correctly.
        pvc_s = price_vs_comps_score(delta_pct, price, blended_median, sqm, settings)
        delta_label_pct = round(delta_pct * 100, 1)
        if pvc_s in (0.0, 100.0):
            print(
                "[score-debug-sale] "
                f"id={listing.get('id', '?')} ask_psqm={ask_psqm} "
                f"blended_median={blended_median} delta_pct={delta_pct:.3f} "
                f"pvc_score={pvc_s:.1f} n={comps['n_comps']} src={comps['benchmark_source']}",
                file=sys.stderr,
            )
    else:
        pvc_s = 50.0
        delta_label_pct = None

    # ── LDI boost (only for at-or-below-comps listings in non-premium zones) ────
    # Same per-city percentile cutoff as the rental scorer above.
    _omi_compr_s = listing.get("omi_compr_mid") or 0
    _boost_max_s = _get_ldi_boost_threshold(listing.get("city", "milano"))
    if delta_pct is not None and delta_pct <= 0 and _omi_compr_s <= _boost_max_s:
        boosted_price_score = round(min(100.0, pvc_s * (1 + ldi_bonus)))
    else:
        boosted_price_score = round(pvc_s)

    # ── Composite ───────────────────────────────────────────────────────────────
    total_raw = round(
        boosted_price_score * w_price
        + prop_s  * w_prop
        + loc_s   * w_loc
        + pen_s   * w_pen
    )
    total_raw = max(0, min(100, total_raw))

    if prop_s < 40:
        total_raw = min(total_raw, 75)

    if delta_label_pct is not None:
        total = apply_price_ceiling(total_raw, delta_label_pct)
    else:
        total = total_raw

    # Below-ground hard cap (sale)
    _fn = listing.get("floor_n")
    if _fn is not None and _fn < 0:
        total = min(total, 55)

    # Absolute value gate: overpriced small flats in premium zones
    total, _gate_applied = apply_absolute_value_gate(total, ask_psqm, sqm)
    listing["_absolute_value_gate_applied"] = bool(_gate_applied)

    score_was_capped = total < total_raw

    # ── Labels ──────────────────────────────────────────────────────────────────
    if delta_pct is not None:
        if abs(delta_label_pct) < 0.5:
            comps_label = "at comps median"
        elif delta_label_pct > 0:
            comps_label = f"+{delta_label_pct:.1f}% above nearby sales"
        else:
            comps_label = f"{delta_label_pct:.1f}% below nearby sales"
    else:
        comps_label = None

    if confidence >= 70:
        conf_label = "High"
    elif confidence >= 40:
        conf_label = "Medium"
    else:
        conf_label = "Low"

    # ── vs OMI purchase benchmark ────────────────────────────────────────────────
    omi_compr_mid = listing.get("omi_compr_mid")
    if omi_compr_mid and omi_compr_mid > 0 and ask_psqm > 0:
        vs_omi_pct = round((ask_psqm - omi_compr_mid) / omi_compr_mid * 100, 1)
        if abs(vs_omi_pct) < 0.5:
            vs_omi_label = "at OMI"
        elif vs_omi_pct > 0:
            vs_omi_label = f"+{vs_omi_pct:.1f}% above OMI"
        else:
            vs_omi_label = f"{vs_omi_pct:.1f}% below OMI"
    else:
        vs_omi_pct   = None
        vs_omi_label = None

    # ── Estimated gross yield (informational only) ───────────────────────────────
    estimated_yield_pct = None
    omi_loc_mid = listing.get("omi_loc_mid")
    if omi_loc_mid and omi_loc_mid > 0 and sqm > 0 and price > 0:
        surf_c = _surface_coeff(sqm)
        cond_c = _condition_coeff(listing.get("condition", ""))
        est_rent_mo = omi_loc_mid * sqm * surf_c * cond_c
        estimated_yield_pct = round((est_rent_mo * 12 / price) * 100, 2)

    # ── Gem flags ────────────────────────────────────────────────────────────────
    _gate_fired = listing.get("_absolute_value_gate_applied", False)
    _loc_s_r = round(loc_s)
    _hidden_gem = (
        not _gate_fired                                                        # gate = not a gem
        and total          >= settings.get("gem_total_min",      72)
        and ldi_score  >= settings.get("gem_ldi_min",         65)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gem_delta_max",       -8.0)
        and prop_s     >= settings.get("gem_property_min",    50)
        and _loc_s_r   >= settings.get("gem_location_min",    45)
        and confidence >= settings.get("gem_confidence_min",  40)
        and pen_s      >= settings.get("gem_penalty_min",     70)
    )
    _good_value = not _hidden_gem and (
        not _gate_fired                                                        # gate = not good value
        and total          >  settings.get("gv_total_min",        65)         # strictly > ceiling
        and ldi_score  >= settings.get("gv_ldi_min",          45)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gv_delta_max",        -5.0)
        and prop_s     >= settings.get("gv_property_min",     40)
        and _loc_s_r   >= settings.get("gv_location_min",     35)
        and confidence >= settings.get("gv_confidence_min",   30)
        and pen_s      >= settings.get("gv_penalty_min",      55)
    )

    return {
        # Comps fields (sale-prefixed)
        "comps_sale_median":     comps.get("median"),
        "comps_sale_p40":        comps.get("p40"),
        "comps_sale_p60":        comps.get("p60"),
        "comps_sale_n":          comps.get("n_comps", 0),
        "comps_sale_radius_m":         comps.get("radius_used"),
        "comps_sale_source":           comps.get("benchmark_source"),
        "comps_sale_confidence":       confidence,
        "comps_sale_conf_label":       conf_label,
        "comps_sale_delta_pct":        delta_label_pct,
        "comps_sale_label":            comps_label,
        "comps_sale_condition_group":  comps.get("condition_group"),
        "comps_sale_adjusted":         bool(comps.get("adjusted")),
        # IDs of the matched comp listings (≤30, sorted by €/m² asc) — used
        # by the detail page to show the actual comps that fed the median.
        "comps_sale_comp_ids":         comps.get("comp_ids", []),
        # Sub-scores
        "score_price":           round(pvc_s),
        "score_property":        prop_s,
        "score_location":        round(loc_s),
        "score_penalty":         pen_s,
        "score_geo":             geo_s,
        "score_total":           total,
        # LDI + boost + ceiling
        "ldi_score":             ldi_score,
        "ldi_bonus":             round(ldi_bonus, 3),
        "boosted_price_score":   boosted_price_score,
        "score_was_capped":      score_was_capped,
        # Gem flags
        "hidden_gem":            _hidden_gem,
        "good_value":            _good_value,
        # OMI purchase context
        "vs_omi_pct":            vs_omi_pct,
        "vs_omi_label":          vs_omi_label,
        # Estimated gross yield (informational)
        "estimated_yield_pct":   estimated_yield_pct,
        "omi_fallback":          comps.get("benchmark_source") in ("omi_only", "none"),
        # Legacy aliases for dashboard compat
        "score_physical":        prop_s,
    }


def score_all_sales(listings: list, settings: dict | None = None) -> list:
    """Score all sale listings, return sorted list."""
    if settings is None:
        settings = _load_settings()

    scored = []
    for l in listings:
        s = score_sale_listing(l, listings, settings)
        merged = {**l, **s}
        # Apply the extreme-underpricing flag AFTER the merge so we see
        # the final score_total + comps_sale_delta_pct in one place.
        # Fake / auction / nuda / price-floor early-return above means
        # we never apply the flag to already-excluded rows (they have
        # score_total=0 and no comps delta — the function is a no-op).
        apply_extreme_underpricing_flag(merged, merged.get("comps_sale_delta_pct"))
        scored.append(merged)

    scored.sort(key=lambda x: x.get("score_total", 0) or 0, reverse=True)

    # Log null-field coverage
    if scored:
        fields_to_check = [
            "omi_compr_mid", "omi_zona", "omi_fascia",
            "floor_n", "elevator", "is_external", "energy_class",
            "has_balcony", "has_parking", "heating_type",
            "photo_count", "days_on_market", "bathrooms",
            "metro_nearest_dist_m", "geo_score",
            "comps_sale_median", "comps_sale_confidence",
        ]
        n = len(scored)
        nulls = {f: sum(1 for l in scored if l.get(f) is None) for f in fields_to_check}
        missing = [(f, c) for f, c in sorted(nulls.items(), key=lambda x: -x[1]) if c > 0]
        if missing:
            print(f"  [scoring-sale] null-field coverage ({n} listings):", file=sys.stderr)
            for f, c in missing:
                pct = round(c / n * 100)
                print(f"    {f:<35s} {c:4d}/{n} ({pct:3d}% null)", file=sys.stderr)

    return scored


def score_all(listings: list, settings: dict | None = None,
              comps_pool: list | None = None) -> list:
    """Score all listings, log null-field coverage, return sorted list.

    Parameters
    ----------
    listings    : the listings to score (written into returned dicts)
    settings    : scoring weights / thresholds; loaded from disk when None
    comps_pool  : pool used for the comps benchmark.  Defaults to *listings*
                  when not supplied, which is the normal single-source case.
                  Pass a larger merged pool (e.g. Idealista + Immobiliare) so
                  every listing has neighbourhood comps available regardless
                  of which source was fetched most recently.
    """
    if settings is None:
        settings = _load_settings()

    pool = comps_pool if comps_pool is not None else listings

    # Pre-pass: stamp ask_psqm_effective + condo_fee_flag on every listing in
    # the comps pool BEFORE scoring runs, so the comps engine reads a
    # consistent "effective" €/m² across the whole pool (including condo
    # fees where present). Cheap — pure arithmetic, no I/O.
    for l in pool:
        l["ask_psqm_effective"] = compute_effective_psqm(l)
        l["condo_fee_flag"]     = get_condo_fee_flag(l)

    scored = []
    for l in listings:
        s = score_rental(l, pool, settings)
        merged = {**l, **s}
        # Same extreme-underpricing flag as sales (rentals at -60 % vs
        # comps are equally suspicious — fake listings exist on both
        # sides of the market).
        apply_extreme_underpricing_flag(merged, merged.get("comps_delta_pct"))
        scored.append(merged)

    scored.sort(key=lambda x: x.get("score_total", 0) or 0, reverse=True)

    # Log null-field coverage
    if scored:
        fields_to_check = [
            "omi_loc_mid", "omi_zona", "omi_fascia",
            "floor_n", "elevator", "is_external", "energy_class",
            "has_balcony", "has_parking", "heating_type", "furnished",
            "photo_count", "days_on_market", "bathrooms",
            "metro_nearest_dist_m", "geo_score",
            "comps_median", "comps_confidence",
        ]
        n = len(scored)
        nulls = {
            f: sum(1 for l in scored if l.get(f) is None)
            for f in fields_to_check
        }
        missing = [(f, c) for f, c in sorted(nulls.items(), key=lambda x: -x[1]) if c > 0]
        if missing:
            print(f"  [scoring] null-field coverage ({n} listings):", file=sys.stderr)
            for f, c in missing:
                pct = round(c / n * 100)
                print(f"    {f:<35s} {c:4d}/{n} ({pct:3d}% null)", file=sys.stderr)

    return scored


# ── Convenience re-exports ───────────────────────────────────────────────────

# physical_score kept for backward compatibility
physical_score = property_score


if __name__ == "__main__":
    import json as _json

    # ── LDI table summary ─────────────────────────────────────────────────────
    print(f"LDI table: {len(LDI)} zones loaded")
    above_65 = [(code, v) for code, v in sorted(LDI.items(), key=lambda x: -x[1]) if v >= 65]
    print(f"Hidden-gem eligible zones (LDI≥65): {len(above_65)}")
    for code, v in above_65:
        zone = omi_lookup.ZONES.get(code, {})
        print(f"  {code:5s}  LDI={v:3d}  compr_mid={zone.get('compr_mid')}  {zone.get('descr','')[:50]}")
    e_zones = [c for c in LDI if c.startswith("E")]
    print(f"Suburbana (E-) zones: {e_zones} → LDI {[LDI[c] for c in e_zones]}")
    print(f"Min LDI={min(LDI.values())}  Max LDI={max(LDI.values())}")
    print()

    # ── Badge validation against rentals_latest.json ──────────────────────────
    rent_path = Path(__file__).parent / "dashboard" / "rentals_latest.json"
    if not rent_path.exists():
        print("rentals_latest.json not found — skipping badge validation")
        sys.exit(0)

    raw = _json.loads(rent_path.read_text())
    gems_before = sum(1 for l in raw if l.get("hidden_gem"))
    good_before = sum(1 for l in raw if l.get("good_value"))

    # Rescore with updated criteria (stderr suppressed for cleaner output)
    import io, contextlib
    _buf = io.StringIO()
    with contextlib.redirect_stderr(_buf):
        scored = score_all(raw)

    gems = [l for l in scored if l.get("hidden_gem")]
    good = [l for l in scored if l.get("good_value")]

    def _prop(l):
        return l.get("score_property") or l.get("score_physical") or 0

    # ── Before/after for first 20 listings ───────────────────────────────────
    old_by_id = {l.get("id"): l for l in raw}
    print("Before/after for first 20 listings (sorted by new score desc):")
    print(f"  {'ID':>12}  {'old':>5}  {'new':>5}  {'delta_pct':>9}  {'capped':>6}  "
          f"{'bst_price':>9}  note")
    for l in scored[:20]:
        lid    = l.get("id", "?")
        old_s  = (old_by_id.get(lid) or {}).get("score_total", "–")
        new_s  = l.get("score_total", "–")
        dpct   = l.get("comps_delta_pct")
        capped = "✓" if l.get("score_was_capped") else ""
        bst    = l.get("boosted_price_score", "–")
        note   = ""
        if dpct is not None and dpct > 10:
            note = f"⚠ {dpct:+.1f}%"
        print(f"  {str(lid):>12}  {str(old_s):>5}  {str(new_s):>5}  "
              f"{(f'{dpct:+.1f}%' if dpct is not None else '–'):>9}  "
              f"{capped:>6}  {str(bst):>9}  {note}")
    print()

    # ── Ceiling constraint checks ──────────────────────────────────────────────
    over10  = [l for l in scored
               if (l.get("comps_delta_pct") or 0) > 10
               and (l.get("score_total") or 0) > 62]
    over20  = [l for l in scored
               if (l.get("comps_delta_pct") or 0) > 20
               and (l.get("score_total") or 0) > 50]

    checks = [
        ("No Hidden Gem has score_total < 72",
         [l for l in gems if (l.get("score_total") or 0) < 72]),
        ("No Hidden Gem has property_score < 50",
         [l for l in gems if _prop(l) < 50]),
        ("No Hidden Gem has location_score < 45",
         [l for l in gems if (l.get("score_location") or 0) < 45]),
        ("No Hidden Gem has penalty_score < 70",
         [l for l in gems if (l.get("score_penalty") or 0) < 70]),
        ("No Good Value has score_total < 65",
         [l for l in good if (l.get("score_total") or 0) < 65]),
        ("No Good Value has property_score < 40",
         [l for l in good if _prop(l) < 40]),
        ("No badge on any listing with score_total < 65",
         [l for l in scored if (l.get("hidden_gem") or l.get("good_value"))
          and (l.get("score_total") or 0) < 65]),
        (">10% above comps never scores above 62",  over10),
        (">20% above comps never scores above 50",  over20),
    ]

    print("Validation checks:")
    all_ok = True
    for desc, violations in checks:
        ok = not violations
        print(f"  {'✓' if ok else '✗'} {desc:<52s} (found: {len(violations)} violations)")
        if not ok:
            all_ok = False
            for l in violations[:5]:
                print(f"      id={l.get('id')}  total={l.get('score_total')}  "
                      f"prop={_prop(l)}  loc={l.get('score_location')}  "
                      f"pen={l.get('score_penalty')}  ldi={l.get('ldi_score')}  "
                      f"delta={l.get('comps_delta_pct')}")

    # ── Floor coefficient validation table ───────────────────────────────────
    # floor_score() now returns a single float (the percentage adjustment),
    # not a tuple. Below-ground hard caps live in penalty_score().
    print("Floor coefficient validation:")
    _floor_cases = [
        # (label,              floor_n, elevator, expected_coeff)
        ('Interrato',             -2,   None,    -0.25),
        ('Seminterrato',          -1,   None,    -0.20),
        ('Piano terra',            0,   None,    -0.10),
        ('Piano rialzato',         1,   None,    -0.10),
        ('2° piano, lift',         2,   True,    -0.03),
        ('2° piano, no lift',      2,   False,   -0.15),
        ('3° piano, lift',         3,   True,     0.00),
        ('3° piano, no lift',      3,   False,   -0.20),
        ('5° piano, lift',         5,   True,     0.05),
        ('5° piano, no lift',      5,   False,   -0.25),
        ('8° piano, lift',         8,   True,     0.10),
        ('8° piano, no lift',      8,   False,   -0.30),
        ('Attico (12°), lift',    12,   True,     0.20),
        ('Attico (12°), no lift', 12,   False,   -0.20),
    ]
    _floor_ok = True
    for lbl, fn, elev, exp in _floor_cases:
        _dummy = {'floor_n': fn}
        if elev is not None:
            _dummy['elevator'] = elev
        got = floor_score(_dummy)
        ok = abs(got - exp) < 0.001
        mark = '✓' if ok else '✗'
        if not ok:
            _floor_ok = False
        print(f"  {mark} {lbl:<28} got={got:+.2f}  exp={exp:+.2f}")
    # Below-ground hard-cap assertion (cap lives in score_rental/score_sale_listing)
    _bg_listings = [l for l in scored if (l.get("floor_n") or 0) < 0]
    _bg_above_55 = [l for l in _bg_listings if (l.get("score_total") or 0) > 55]
    if _bg_above_55:
        print(f"  ✗ {len(_bg_above_55)} below-ground listings scored above 55 — cap broken!")
        _floor_ok = False
    else:
        print(f"  ✓ All {len(_bg_listings)} below-ground listings capped at ≤55")
    if not _floor_ok:
        all_ok = False
    print()

    n_capped = sum(1 for l in scored if l.get("score_was_capped"))
    print(f"Hidden Gems:    {len(gems)}  (was: {gems_before} before fix)")
    print(f"Good Value:     {len(good)}  (was: {good_before} before fix)")
    print(f"Ceiling-capped: {n_capped} listings had score reduced by price ceiling")

    if not all_ok:
        sys.exit(1)
