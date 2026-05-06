#!/usr/bin/env python3
"""
comps_engine.py
───────────────
Builds a local comparable-listing benchmark for any single listing using
the already-fetched pool (rentals_latest.json or an in-memory list).

No external API calls.  Called at score-time inside scoring.py.

Output dict keys
────────────────
  median          float  – median ask_psqm of cleaned comps
  p40             float  – 40th-percentile ask_psqm
  p60             float  – 60th-percentile ask_psqm
  delta_pct       float  – (asking - median) / median  (+ = above comps)
  confidence      int    – 0–100
  n_comps         int    – count after cleaning
  n_raw           int    – count before cleaning
  radius_used     int    – metres used (or None for zone fallback)
  benchmark_source str   – "comps_500"|"comps_800"|"comps_1200"|"omi_zone"|"omi_only"
  blended_median  float  – confidence-weighted blend with OMI mid
"""

from __future__ import annotations
import math
import statistics
from typing import Optional


# ── Haversine ────────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── IQR + asymmetric trim ────────────────────────────────────────────────────

def _clean(values: list[float], top_trim: float = 0.12, bot_trim: float = 0.07) -> list[float]:
    """IQR outlier removal followed by asymmetric trim."""
    if len(values) < 4:
        return list(values)
    s = sorted(values)
    n = len(s)
    q1 = statistics.median(s[: n // 2])
    q3 = statistics.median(s[(n + 1) // 2 :])
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    cleaned = [v for v in s if lo <= v <= hi]
    if not cleaned:
        cleaned = s
    m = len(cleaned)
    bot_cut = max(0, round(m * bot_trim))
    top_cut = max(0, round(m * top_trim))
    return cleaned[bot_cut : m - top_cut if top_cut else m]


# ── Percentile helper ────────────────────────────────────────────────────────

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear interpolation percentile on an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    i = (pct / 100) * (len(sorted_vals) - 1)
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (i - lo) * (sorted_vals[hi] - sorted_vals[lo])


# ── Confidence score ─────────────────────────────────────────────────────────

def _confidence(n_comps: int, radius_used: Optional[int], iqr_spread: float) -> int:
    """
    0–100.
    n_comps: more = better (caps at ~30)
    radius_used: tighter = better (None = zone fallback = low conf)
    iqr_spread: p60-p40 relative spread; wide = noisy = lower conf
    """
    if n_comps == 0:
        return 0

    # Base from count
    if n_comps >= 30:
        score = 80
    elif n_comps >= 15:
        score = 65
    elif n_comps >= 8:
        score = 50
    elif n_comps >= 4:
        score = 35
    else:
        score = 20

    # Radius bonus/penalty
    if radius_used is None:
        score -= 20   # zone fallback
    elif radius_used == 500:
        score += 15
    elif radius_used == 800:
        score += 5
    # 1200m: no adjustment

    # Spread penalty
    if iqr_spread > 0.25:
        score -= 15
    elif iqr_spread > 0.15:
        score -= 8

    return max(0, min(100, score))


# ── OMI blend ────────────────────────────────────────────────────────────────

def _blend(comps_median: float, omi_mid: Optional[float], confidence: int) -> float:
    """Blend comps median with OMI mid based on confidence level."""
    if omi_mid is None or omi_mid <= 0:
        return comps_median
    if confidence >= 70:
        return comps_median                        # comps only
    elif confidence >= 40:
        return comps_median * 0.80 + omi_mid * 0.20   # 80/20
    else:
        return comps_median * 0.50 + omi_mid * 0.50   # 50/50


def _blend_sale(comps_median: float, omi_mid: Optional[float], n_comps: int,
                confidence: int) -> float:
    """
    Sale-specific blend.
    Sales comps are sparser than rentals, so blend more heavily toward OMI
    when fewer than 15 comparables are available — regardless of confidence.
    """
    if omi_mid is None or omi_mid <= 0:
        return comps_median
    if n_comps >= 15:
        return _blend(comps_median, omi_mid, confidence)
    return comps_median * 0.40 + omi_mid * 0.60   # conservative for thin comps


# ── Condition stratification ─────────────────────────────────────────────────

CONDITION_GROUPS: dict[str, str] = {
    'ristrutturato':    'premium',
    'ottimo':           'premium',
    'ottime':           'premium',
    'nuovo':            'premium',
    'in costruzione':   'premium',
    'buono':            'standard',
    'buone':            'standard',
    'abitabile':        'standard',
    'normale':          'standard',
    'da_ristrutturare': 'needs_work',
    'da ristrutturare': 'needs_work',
    'fatiscente':       'needs_work',
}

# Condition adjustment factors — Milan-specific market estimates.
# Periodically validate against real transaction data.
# Read as: FACTOR[(comp_group, listing_group)] = multiplier applied to
# the comp's raw €/m² so it approximates what it would cost in the
# target listing's condition group before computing the median.
CONDITION_FACTORS: dict[tuple[str, str], float] = {
    ('premium',    'standard'):   0.78,   # ristrutturato → buono: ~22% premium discount
    ('premium',    'needs_work'): 0.62,
    ('premium',    'unknown'):    1.00,   # do not over-correct unknowns
    ('standard',   'premium'):    1.28,
    ('standard',   'needs_work'): 0.80,
    ('standard',   'unknown'):    1.00,
    ('needs_work', 'premium'):    1.61,
    ('needs_work', 'standard'):   1.25,
    ('needs_work', 'unknown'):    1.00,
    ('unknown',    'standard'):   1.00,   # no adjustment for unknown comp condition
    ('unknown',    'premium'):    1.00,
    ('unknown',    'needs_work'): 1.00,
    ('unknown',    'unknown'):    1.00,
}


def condition_group(listing: dict) -> str:
    """
    Map a listing's raw `condition` string to one of:
      'premium' | 'standard' | 'needs_work' | 'unknown'
    Tolerates messy/spelling variants via keyword fallback.
    """
    raw = listing.get('condition')
    if not raw:
        return 'unknown'
    c = str(raw).lower().strip()
    if c in CONDITION_GROUPS:
        return CONDITION_GROUPS[c]
    # Keyword fallback for noisy data
    if 'da ' in c and 'ristruttur' in c:
        return 'needs_work'
    if 'fatiscent' in c:
        return 'needs_work'
    if any(k in c for k in ('ristruttur', 'ottim', 'nuovo', 'costruz')):
        return 'premium'
    if any(k in c for k in ('buon', 'abitabil', 'normale')):
        return 'standard'
    return 'unknown'


def _adjust_price(raw_price: float, comp_group: str, target_group: str) -> float:
    """Multiply comp price by the condition factor so it is comparable to target."""
    if comp_group == target_group:
        return raw_price
    factor = CONDITION_FACTORS.get((comp_group, target_group), 1.0)
    return raw_price * factor


# ── Size band ────────────────────────────────────────────────────────────────

def get_size_band(sqm: float) -> tuple[float, float]:
    """
    Returns (min_sqm, max_sqm) for comparable selection.
    Narrower band for small flats to avoid mixing studios/bilocali with
    larger properties that inflate the comp median in premium zones.
    """
    if sqm <= 50:
        return sqm * 0.85, sqm * 1.15   # e.g. 42 m² → 36–48 m²
    elif sqm <= 75:
        return sqm * 0.80, sqm * 1.20   # e.g. 52 m² → 42–62 m²
    else:
        return sqm * 0.75, sqm * 1.25   # tighter than legacy 0.7/1.3


# ── Public API ───────────────────────────────────────────────────────────────

def get_comps_benchmark(
    listing: dict,
    all_listings: list[dict],
    radii: tuple[int, ...] = (500, 800, 1200),
    min_comps: int = 5,
    settings: dict | None = None,
    mode: str = 'rent',   # 'rent' or 'sale'
) -> dict:
    """
    Build a comps benchmark for *listing* from *all_listings*.

    Returns a dict (see module docstring for keys).
    Falls back to OMI zone if no radius yields enough comps.

    mode='rent' uses omi_loc_mid as the OMI anchor (€/m²/month).
    mode='sale' uses omi_compr_mid (€/m²) and applies a more conservative
    OMI blend when fewer than 15 sale comps are available.
    """
    if settings:
        radii = tuple(settings.get("radii", list(radii)))
        min_comps = int(settings.get("min_comps", min_comps))

    # Use the condo-fee-inclusive €/m² when available (rent mode pre-pass
    # populates `ask_psqm_effective`). Sale listings don't carry it, so they
    # fall back to the raw ask_psqm — same behaviour as before.
    def _psqm(l: dict) -> float:
        return (l.get("ask_psqm_effective") or l.get("ask_psqm") or 0)

    lat        = listing.get("latitude")  or listing.get("lat")
    lon        = listing.get("longitude") or listing.get("lng") or listing.get("lon")
    lid        = listing.get("id")
    ask_psqm   = _psqm(listing)
    omi_mid    = listing.get("omi_compr_mid") if mode == 'sale' else listing.get("omi_loc_mid")
    omi_zone   = listing.get("omi_zona")
    omi_fascia = listing.get("omi_fascia")

    listing_cgroup = condition_group(listing)
    sqm_t = listing.get("sqm") or 0

    if not ask_psqm:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    # ── Size band ─────────────────────────────────────────────────────────────
    if sqm_t > 0:
        min_sqm, max_sqm = get_size_band(sqm_t)
    else:
        min_sqm, max_sqm = 0.0, float("inf")

    def _is_candidate(l: dict) -> bool:
        if l.get("id") == lid:
            return False
        if _psqm(l) <= 0:
            return False
        if sqm_t > 0:
            comp_sqm = l.get("sqm") or 0
            if comp_sqm == 0 or not (min_sqm <= comp_sqm <= max_sqm):
                return False
        return True

    def _has_geo(l: dict) -> bool:
        return bool((l.get("latitude") or l.get("lat")) and
                    (l.get("longitude") or l.get("lng") or l.get("lon")))

    # ── Pass 1: same OMI zone + same condition group (ideal pool) ─────────────
    pool: list[dict] = []
    pass_used: str = ""
    radius_used = None

    if omi_zone and listing_cgroup != 'unknown':
        candidates = [l for l in all_listings
                      if _is_candidate(l)
                      and l.get("omi_zona") == omi_zone
                      and condition_group(l) == listing_cgroup]
        if len(candidates) >= 15:
            pool = candidates
            pass_used = 'zone_stratified'

    # ── Pass 2: same OMI zone, any condition (with adjustment) ────────────────
    if not pass_used and omi_zone:
        candidates = [l for l in all_listings
                      if _is_candidate(l) and l.get("omi_zona") == omi_zone]
        if len(candidates) >= 15:
            pool = candidates
            pass_used = 'zone_any_condition'

    # ── Pass 3: 800 m radius + same fascia ───────────────────────────────────
    if not pass_used and lat and lon and omi_fascia:
        radius_p3 = 800
        candidates = [l for l in all_listings
                      if _is_candidate(l)
                      and l.get("omi_fascia") == omi_fascia
                      and _has_geo(l)
                      and _haversine_m(lat, lon,
                                       l.get("latitude") or l.get("lat"),
                                       l.get("longitude") or l.get("lng") or l.get("lon"))
                          <= radius_p3]
        if len(candidates) >= min_comps:
            pool = candidates
            pass_used = 'radius_800m_same_fascia'
            radius_used = radius_p3

    # ── Pass 4: full fascia fallback ─────────────────────────────────────────
    if not pass_used and omi_fascia:
        candidates = [l for l in all_listings
                      if _is_candidate(l) and l.get("omi_fascia") == omi_fascia]
        if len(candidates) >= min_comps:
            pool = candidates
            pass_used = 'fascia_fallback'

    # ── Legacy radius fallback (if no fascia info) ───────────────────────────
    if not pass_used and lat and lon:
        for radius in radii:
            candidates = [l for l in all_listings
                          if _is_candidate(l)
                          and _has_geo(l)
                          and _haversine_m(lat, lon,
                                           l.get("latitude") or l.get("lat"),
                                           l.get("longitude") or l.get("lng") or l.get("lon"))
                              <= radius]
            if len(candidates) >= min_comps:
                pool = candidates
                pass_used = f'comps_{radius}'
                radius_used = radius
                break

    # ── OMI zone fallback (any size for thin pools) ──────────────────────────
    if not pass_used and omi_zone:
        candidates = [l for l in all_listings
                      if _is_candidate(l) and l.get("omi_zona") == omi_zone]
        if len(candidates) >= min_comps:
            pool = candidates
            pass_used = 'omi_zone'

    if not pool:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    # ── Apply condition adjustment ───────────────────────────────────────────
    # Pass 1 (zone_stratified) is by definition same-group: no adjustment.
    # All other passes mix conditions: multiply each comp by its factor.
    # Listings with 'unknown' condition: keep raw price (factor 1.0) to
    # avoid biasing on missing data.
    adjusted = pass_used != 'zone_stratified' and listing_cgroup != 'unknown'
    if adjusted:
        adj_prices = [_adjust_price(_psqm(l),
                                    condition_group(l),
                                    listing_cgroup)
                      for l in pool]
    else:
        adj_prices = [_psqm(l) for l in pool]
    adj_prices = [p for p in adj_prices if p > 0]
    if not adj_prices:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    # ── IQR outlier removal + asymmetric trim (gentler when pool is small) ───
    s = sorted(adj_prices)
    n = len(s)
    if n >= 4:
        q1 = s[n // 4]
        q3 = s[(3 * n) // 4]
        iqr = q3 - q1
        s = [v for v in s if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr]
        if not s:
            s = sorted(adj_prices)

    n2 = len(s)
    if n2 >= 30:
        top_cut = max(1, int(n2 * 0.12))
        bot_cut = max(1, int(n2 * 0.05))
        s = s[bot_cut: n2 - top_cut]
    elif n2 >= 15:
        top_cut = max(1, int(n2 * 0.08))
        s = s[: n2 - top_cut]
    # < 15: no trim — every data point is precious

    if not s:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    median = statistics.median(s)
    p40    = _percentile(s, 40)
    p60    = _percentile(s, 60)
    n_final = len(s)
    spread = (p60 - p40) / median if median > 0 else 0
    stdev_v = statistics.stdev(s) if len(s) >= 2 else 0

    # ── Confidence (per stratification spec) ──────────────────────────────────
    confidence = 0
    if n_final >= 40:
        confidence += 40
    elif n_final >= 20:
        confidence += 20
    if pass_used == 'zone_stratified':
        confidence += 20
    elif 'zone' in pass_used:
        confidence += 10
    confidence += 20 if stdev_v < (3.0 if mode == 'rent' else 1500) else 0
    if pass_used == 'zone_stratified' and listing_cgroup != 'unknown':
        confidence += 20
    confidence = max(0, min(100, confidence))

    blended = (
        _blend_sale(median, omi_mid, n_final, confidence) if mode == 'sale'
        else _blend(median, omi_mid, confidence)
    )
    delta_pct = (ask_psqm - blended) / blended if blended > 0 else 0

    return {
        "median":            round(median, 2),
        "p40":               round(p40, 2),
        "p60":               round(p60, 2),
        "delta_pct":         round(delta_pct, 4),
        "confidence":        confidence,
        "n_comps":           n_final,
        "n_raw":             len(pool),
        "radius_used":       radius_used,
        "benchmark_source":  pass_used,
        "blended_median":    round(blended, 2),
        "condition_group":   listing_cgroup,
        "adjusted":          adjusted,
    }


def _no_comps_result(ask_psqm: float, omi_mid: Optional[float],
                     listing_cgroup: str = 'unknown') -> dict:
    """No usable comps — return OMI-only or empty result."""
    if omi_mid and omi_mid > 0 and ask_psqm > 0:
        delta_pct = (ask_psqm - omi_mid) / omi_mid
        return {
            "median":           round(omi_mid, 2),
            "p40":              round(omi_mid, 2),
            "p60":              round(omi_mid, 2),
            "delta_pct":        round(delta_pct, 4),
            "confidence":       15,
            "n_comps":          0,
            "n_raw":            0,
            "radius_used":      None,
            "benchmark_source": "omi_only",
            "blended_median":   round(omi_mid, 2),
            "condition_group":  listing_cgroup,
            "adjusted":         False,
        }
    return {
        "median":           None,
        "p40":              None,
        "p60":              None,
        "delta_pct":        None,
        "confidence":       0,
        "n_comps":          0,
        "n_raw":            0,
        "radius_used":      None,
        "benchmark_source": "none",
        "blended_median":   None,
        "condition_group":  listing_cgroup,
        "adjusted":         False,
    }
