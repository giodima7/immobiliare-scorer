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

from __future__ import annotations
import json
import sys
from pathlib import Path

from comps_engine import get_comps_benchmark
import omi_lookup


# ── Location Desirability Index ──────────────────────────────────────────────

def _build_ldi() -> dict[str, int]:
    """Normalise OMI compr_mid purchase prices to a 0-100 LDI score per zone."""
    values = {
        code: z["compr_mid"]
        for code, z in omi_lookup.ZONES.items()
        if z.get("compr_mid") is not None
    }
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {code: 50 for code in values}
    return {code: round((v - lo) / (hi - lo) * 100) for code, v in values.items()}


LDI: dict[str, int] = _build_ldi()


def get_ldi(listing: dict) -> int:
    """Return LDI score (0–100) for the listing's OMI zone. Defaults to 50."""
    return LDI.get(listing.get("omi_zona"), 50)


def is_hidden_gem(listing: dict, settings: dict | None = None) -> bool:
    """
    A Hidden Gem must be excellent on ALL dimensions simultaneously.
    No single strong signal compensates for weakness elsewhere.
    Thresholds are read from settings (or _DEFAULT_SETTINGS if not provided).
    """
    if settings is None:
        settings = _load_settings()
    delta = listing.get("comps_delta_pct")
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
    "gem_total_min":      72,
    "gem_ldi_min":        65,
    "gem_delta_max":      -8.0,
    "gem_property_min":   50,
    "gem_location_min":   45,
    "gem_confidence_min": 40,
    "gem_penalty_min":    70,
    # Great Value badge thresholds
    "gv_total_min":       65,
    "gv_ldi_min":         45,
    "gv_delta_max":       -5.0,
    "gv_property_min":    40,
    "gv_location_min":    35,
    "gv_confidence_min":  30,
    "gv_penalty_min":     55,
}


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH) as f:
            overrides = json.load(f)
        return {**_DEFAULT_SETTINGS, **overrides}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


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
    Score physical quality 0–100.

    Normalised against the maximum positive score achievable from the fields
    that are actually present, so listings are not penalised for data that the
    scraper simply did not collect (e.g. energy_class, has_balcony, is_external,
    heating_type, furnished are often 100 % null for this source).

    Penalty-only fields (days_on_market, spese_condominiali) are included but
    do not inflate the positive ceiling.

    Returns 50 (neutral) when no physical fields are present.
    """
    if settings is None:
        settings = {}
    score   = 0
    n_known = 0
    max_pos = 0   # max POSITIVE points achievable from fields that are present

    # ── Floor + elevator (combined) ──────────────────────────────────────────
    floor    = listing.get("floor_n")
    has_lift = listing.get("elevator")
    lift_bonus  = int(settings.get("floor_lift_bonus",   28))
    nolift_pen  = int(settings.get("floor_nolift_penalty", -15))

    if floor is not None:
        n_known += 1
        if floor == 0:
            pass                # ground-floor penalty lives in penalty_score(); no upside here
        elif 1 <= floor <= 2:
            score   += 5
            max_pos += 5
        else:                   # floor >= 3: potential is lift_bonus regardless of actual lift
            max_pos += lift_bonus
            if has_lift is True:
                score += lift_bonus
            elif has_lift is False:
                score += nolift_pen
            else:
                score += 10     # unknown lift: mild positive

    # ── External facing ──────────────────────────────────────────────────────
    ext = listing.get("is_external")
    if ext is not None:
        n_known += 1
        max_pos += 10
        if ext is True:
            score += 10

    # ── Energy class: (numeric/10) × 20 pts max ─────────────────────────────
    ec = _energy_numeric(listing.get("energy_class"))
    if ec is not None:
        n_known += 1
        max_pos += 20
        score   += round((ec / 10) * 20)

    # ── Balcony / terrace / garden ───────────────────────────────────────────
    balc = listing.get("has_balcony")
    if balc is not None:
        n_known += 1
        max_pos += 10
        if balc is True:
            score += 10

    # ── Parking ──────────────────────────────────────────────────────────────
    park = listing.get("has_parking")
    if park is not None:
        n_known += 1
        max_pos += 5
        if park is True:
            score += 5

    # ── Autonomous heating ───────────────────────────────────────────────────
    ht = listing.get("heating_type")
    if ht is not None:
        n_known += 1
        max_pos += 5
        if str(ht).lower() == "autonomous":
            score += 5

    # ── Furnished ────────────────────────────────────────────────────────────
    furn = listing.get("furnished")
    if furn is not None:
        n_known += 1
        max_pos += 5
        if furn is True:
            score += 5

    # ── Photo count (listing quality proxy) ─────────────────────────────────
    photos = listing.get("photo_count")
    if photos is not None:
        n_known += 1
        max_pos += 5            # max upside is +5; bad photos give -5
        if photos >= 10:
            score += 5
        elif photos < 5:
            score -= 5

    # ── Days on market — penalty-only, does not raise max_pos ───────────────
    dom = listing.get("days_on_market")
    if dom is not None:
        n_known += 1
        if dom > 60:
            score -= 10
        elif dom > 30:
            score -= 5

    # ── Bathrooms ────────────────────────────────────────────────────────────
    baths = listing.get("bathrooms")
    if baths is not None:
        n_known += 1
        max_pos += 5
        if baths >= 2:
            score += 5

    # ── Condominium fees — penalty-only, does not raise max_pos ─────────────
    rent  = listing.get("rent_mo")
    spese = listing.get("spese_condominiali")
    if rent and spese and rent > 0:
        n_known += 1
        if spese > rent * 0.20:
            score -= 10

    # ── Condition ────────────────────────────────────────────────────────────
    cond = (listing.get("condition") or "").lower()
    if cond:
        n_known += 1
        max_pos += 10
        if "da " in cond and "ristruttur" in cond:
            score -= 10
        elif any(k in cond for k in ("ottim", "ristruttur", "nuovo", "costruz")):
            score += 10
        elif any(k in cond for k in ("buon", "abitabil")):
            score += 5

    # ── Year built (newer is a mild positive) ───────────────────────────────
    yr = listing.get("year_built")
    if yr and isinstance(yr, (int, float)) and yr > 1900:
        n_known += 1
        max_pos += 8
        if yr >= 2010:
            score += 8
        elif yr >= 1990:
            score += 3

    if n_known == 0:
        return 50   # no data — neutral

    if max_pos > 0:
        # Normalise raw score against achievable ceiling so absent fields
        # do not dilute the result.
        return max(0, min(100, round(score / max_pos * 100)))

    # Only penalty/neutral fields were present (ground floor, dom, spese).
    # Centre at 50 and let penalties count down from there.
    return max(0, min(50, score + 50))


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

    Returns int 0–100.
    """
    score = 100
    floor     = listing.get("floor_n")
    has_lift  = listing.get("elevator")
    sqm       = listing.get("sqm") or 0
    rooms     = listing.get("rooms") or 0
    dom       = listing.get("days_on_market") or 0
    rent_mo   = listing.get("rent_mo") or 0
    spese     = listing.get("spese_condominiali") or 0

    # Ground floor (security + light penalty)
    if floor == 0:
        score -= 20

    # High floor with no lift
    if floor is not None and floor > 4 and has_lift is False:
        score -= 30

    # Size mismatch: very small for rooms declared
    if sqm > 0 and rooms > 0 and sqm / rooms < 12:
        score -= 15

    # Very stale listing
    stale = settings.get("penalty_dom_stale", 60)
    warn  = settings.get("penalty_dom_warn",  30)
    if dom > stale:
        score -= 25
    elif dom > warn:
        score -= 10

    # Condominium fees eating > 25% of rent
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


# ── Composite score ───────────────────────────────────────────────────────────

def score_rental(listing: dict, all_listings: list, settings: dict | None = None) -> dict:
    """
    Score a single rental listing.

    Comps benchmark is built from all_listings using comps_engine.
    Returns a dict of score fields to be merged into the listing.
    """
    if settings is None:
        settings = _load_settings()

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

    # ── LDI boost (only for bargains — listings at or below comps median) ──────
    # A bargain in a prime area deserves a reward; an overpriced one does not.
    if delta_pct is not None and delta_pct <= 0:
        boosted_price_score = round(min(100.0, pvc_s * (1 + ldi_bonus)))
    else:
        boosted_price_score = round(pvc_s)   # above comps or no comps: no boost

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

    # Suggested rent from OMI (keep for display)
    suggested_rent_mo = None
    if omi_mid and omi_mid > 0 and sqm > 0:
        suggested_rent_mo = int(round(omi_mid * sqm / 25) * 25)

    # ── Hidden Gem / Great Value flags ────────────────────────────────────────
    _loc_s_r = round(loc_s)
    _hidden_gem = (
        total          >= settings.get("gem_total_min",      72)
        and ldi_score  >= settings.get("gem_ldi_min",         65)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gem_delta_max",       -8.0)
        and prop_s     >= settings.get("gem_property_min",    50)
        and _loc_s_r   >= settings.get("gem_location_min",    45)
        and confidence >= settings.get("gem_confidence_min",  40)
        and pen_s      >= settings.get("gem_penalty_min",     70)
    )
    _good_value = not _hidden_gem and (
        total          >= settings.get("gv_total_min",        65)
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
        "comps_median":        comps.get("median"),
        "comps_p40":           comps.get("p40"),
        "comps_p60":           comps.get("p60"),
        "comps_n":             comps.get("n_comps", 0),
        "comps_radius_m":      comps.get("radius_used"),
        "comps_source":        comps.get("benchmark_source"),
        "comps_confidence":    confidence,
        "comps_conf_label":    conf_label,
        "comps_delta_pct":     delta_label_pct,
        "comps_label":         comps_label,
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

    # ── LDI boost (only for at-or-below-comps listings) ─────────────────────────
    if delta_pct is not None and delta_pct <= 0:
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
    _loc_s_r = round(loc_s)
    _hidden_gem = (
        total          >= settings.get("gem_total_min",      72)
        and ldi_score  >= settings.get("gem_ldi_min",         65)
        and delta_label_pct is not None
        and delta_label_pct            <= settings.get("gem_delta_max",       -8.0)
        and prop_s     >= settings.get("gem_property_min",    50)
        and _loc_s_r   >= settings.get("gem_location_min",    45)
        and confidence >= settings.get("gem_confidence_min",  40)
        and pen_s      >= settings.get("gem_penalty_min",     70)
    )
    _good_value = not _hidden_gem and (
        total          >= settings.get("gv_total_min",        65)
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
        "comps_sale_radius_m":   comps.get("radius_used"),
        "comps_sale_source":     comps.get("benchmark_source"),
        "comps_sale_confidence": confidence,
        "comps_sale_conf_label": conf_label,
        "comps_sale_delta_pct":  delta_label_pct,
        "comps_sale_label":      comps_label,
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
        scored.append({**l, **s})

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


def score_all(listings: list, settings: dict | None = None) -> list:
    """Score all listings, log null-field coverage, return sorted list."""
    if settings is None:
        settings = _load_settings()

    scored = []
    for l in listings:
        s = score_rental(l, listings, settings)
        scored.append({**l, **s})

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

    print()
    n_capped = sum(1 for l in scored if l.get("score_was_capped"))
    print(f"Hidden Gems:    {len(gems)}  (was: {gems_before} before fix)")
    print(f"Good Value:     {len(good)}  (was: {good_before} before fix)")
    print(f"Ceiling-capped: {n_capped} listings had score reduced by price ceiling")

    if not all_ok:
        sys.exit(1)
