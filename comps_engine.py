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

    lat = listing.get("latitude") or listing.get("lat")
    lon = listing.get("longitude") or listing.get("lng") or listing.get("lon")
    lid = listing.get("id")
    ask_psqm = listing.get("ask_psqm") or 0
    omi_mid  = listing.get("omi_compr_mid") if mode == 'sale' else listing.get("omi_loc_mid")
    omi_zone = listing.get("omi_zona")

    if not lat or not lon or not ask_psqm:
        return _no_comps_result(ask_psqm, omi_mid)

    # ── Progressive radius search ─────────────────────────────────────────────
    for radius in radii:
        candidates = [
            l["ask_psqm"]
            for l in all_listings
            if l.get("id") != lid
            and (l.get("ask_psqm") or 0) > 0
            and (l.get("latitude") or l.get("lat"))
            and (l.get("longitude") or l.get("lng") or l.get("lon"))
            and _haversine_m(
                lat, lon,
                l.get("latitude") or l.get("lat"),
                l.get("longitude") or l.get("lng") or l.get("lon"),
            ) <= radius
        ]
        if len(candidates) >= min_comps:
            return _build_result(candidates, radius, ask_psqm, omi_mid, "comps", mode)

    # ── OMI zone fallback ─────────────────────────────────────────────────────
    if omi_zone:
        zone_vals = [
            l["ask_psqm"]
            for l in all_listings
            if l.get("id") != lid
            and l.get("omi_zona") == omi_zone
            and (l.get("ask_psqm") or 0) > 0
        ]
        if len(zone_vals) >= min_comps:
            return _build_result(zone_vals, None, ask_psqm, omi_mid, "omi_zone", mode)

    # ── Pure OMI fallback ─────────────────────────────────────────────────────
    return _no_comps_result(ask_psqm, omi_mid)


def _build_result(
    raw_vals: list[float],
    radius: Optional[int],
    ask_psqm: float,
    omi_mid: Optional[float],
    source_prefix: str,
    mode: str = 'rent',
) -> dict:
    n_raw = len(raw_vals)
    cleaned = _clean(raw_vals)
    if not cleaned:
        cleaned = sorted(raw_vals)

    s = sorted(cleaned)
    median = statistics.median(s)
    p40    = _percentile(s, 40)
    p60    = _percentile(s, 60)
    spread = (p60 - p40) / median if median > 0 else 0

    confidence = _confidence(len(s), radius, spread)
    blended = (
        _blend_sale(median, omi_mid, len(s), confidence)
        if mode == 'sale'
        else _blend(median, omi_mid, confidence)
    )

    delta_pct = (ask_psqm - blended) / blended if blended > 0 else 0

    source = f"{source_prefix}_{radius}" if radius is not None else source_prefix

    return {
        "median":           round(median, 2),
        "p40":              round(p40, 2),
        "p60":              round(p60, 2),
        "delta_pct":        round(delta_pct, 4),
        "confidence":       confidence,
        "n_comps":          len(s),
        "n_raw":            n_raw,
        "radius_used":      radius,
        "benchmark_source": source,
        "blended_median":   round(blended, 2),
    }


def _no_comps_result(ask_psqm: float, omi_mid: Optional[float]) -> dict:
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
    }
