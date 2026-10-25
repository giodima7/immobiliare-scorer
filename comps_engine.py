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
import re
import statistics
from datetime import date
from typing import Optional

import numpy as np


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
    # Idealista's "buono stato" covers renovated flats too (listing_details)
    # — it says nothing about premium vs standard.
    if "idealista" in c:
        return 'unknown'
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

# Suspect flags set by the scrapers' detectors — the same ones
# scoring._apply_exclusion_gates uses to hide a listing. Their prices
# aren't market prices (bait rents, auctions, bare ownership), so they
# must not feed anyone else's benchmark either.
SUSPECT_FLAGS = ("is_fake", "is_misrepresented_address", "is_outside_city",
                 "is_auction", "is_nuda_proprieta")

# Duplicates: the same flat listed on Immobiliare AND Idealista, or by
# several agencies, or relisted under a new id. Measured on Milan rentals:
# ~12 % of the pool is extra copies. Left in, they over-weight one flat's
# price and — worse — a listing's own twin sits in its comps at exactly
# its price, pulling the benchmark toward its ask and hiding mispricing.
DUP_MAX_M = 300   # Idealista coords are geocoded, often a few hundred m off

# Mid-term / short-let platforms: furnished, bills-included stays priced at
# ×1.4–1.6 the zone median (Milan, 2026-10-14: ~5k rows, 18 % of the pool).
# They're still scored, but never used as comps for long-term rentals.
SHORTLET_PLATFORMS = re.compile(r"spacest|roomless|housinganywhere|uniplaces|spotahome", re.I)

# Rental benchmark (validated by backtest on Milan, 8,128 long-term listings:
# median |ask − benchmark| 12.8 % → 11.9 %, bias +0.8 % → 0.0 %, Idealista vs
# Immobiliare gap +1.4 pp → 0.0, size bias −0.113 → −0.060). Each switch can
# be overridden from scoring_settings.json.
RENT_BENCHMARK_DEFAULTS = {
    "comps_method":           "knn",   # "knn" (nearest flats) | "zone" (OMI-zone cascade)
    "knn_k":                  30,
    "knn_max_m":              1500,
    "size_elasticity":        -0.30,   # €/m² ∝ m²^β — bigger flats rent for less per m²
    "recency_half_life_days": 90,      # archived comps count half after 90 days unseen
    "omi_blend":              False,   # asks run ~41 % above OMI — blending biased it low
    "trim":                   "sym",   # "sym" = IQR only; "asym" = old 5 %/12 % trim
}


def _ll(l: dict) -> tuple:
    return (l.get("latitude") or l.get("lat"),
            l.get("longitude") or l.get("lng") or l.get("lon"))


def _dup_key(l: dict) -> tuple | None:
    """Same asking price + size + rooms + floor. Rentals carry rent_mo,
    sales carry price."""
    price = l.get("rent_mo") or l.get("price")
    if not price or not l.get("sqm") or l.get("rooms") is None:
        return None
    return (price, l.get("sqm"), l.get("rooms"), l.get("floor_n"))


def _dup_near(a: dict, b: dict) -> bool:
    la, lb = _ll(a), _ll(b)
    if all(la) and all(lb):
        return _haversine_m(*la, *lb) <= DUP_MAX_M
    return bool(a.get("omi_zona")) and a.get("omi_zona") == b.get("omi_zona")


# Cross-site copies the exact key misses (2026-10-25: one flat at €318,000
# in Corso di Porta Ticinese, 4th floor, as 43, 45 and 50 m² on the two sites,
# the Idealista pin 310 m off at the street's centre). Same price, rooms and
# floor on two sites, m² within these shares, and the same street when both
# name one (then up to DUP_LOOSE_STREET_M apart).
LOOSE_DUPS = True          # 2026-10-25: rentals 2,443 → 3,353 listings grouped, labels +29 −26
DUP_LOOSE_SQM = {"rent": 0.07, "sale": 0.15}    # a sale price is nearly a signature
DUP_LOOSE_STREET_M = 800


def _site(l: dict) -> str:
    src = l.get("source") or ""
    return "idealista" if "idealista" in src else "immobiliare" if src in ("immobiliare", "sale") else src


def _loose_dup(a: dict, b: dict) -> bool:
    if _site(a) == _site(b):
        return False
    fa, fb = a.get("floor_n"), b.get("floor_n")
    if fa is not None and fb is not None and fa != fb:
        return False
    tol = DUP_LOOSE_SQM["rent" if a.get("rent_mo") else "sale"]
    if abs(a["sqm"] - b["sqm"]) > max(3, tol * min(a["sqm"], b["sqm"])):
        return False
    st_a, st_b = _street(a), _street(b)
    if st_a and st_b:
        if st_a[0] != st_b[0] or (st_a[1] and st_b[1] and st_a[1] != st_b[1]):
            return False
        la, lb = _ll(a), _ll(b)
        return not (all(la) and all(lb)) or _haversine_m(*la, *lb) <= DUP_LOOSE_STREET_M
    return _dup_near(a, b)


def _dup_rank(l: dict) -> tuple:
    """Which copy of a duplicate group stays in the pool: one still on the
    market (not excluded, not archived), then Immobiliare (scraped coords
    beat geocoded ones), then the most recently seen. max() keeps pool
    order on ties."""
    return (not l.get("excluded"), not l.get("is_stale"),
            l.get("source") == "immobiliare", l.get("last_seen_date") or "")


# Same flat at another price — a relisting, a price change between scans,
# or two agencies asking different rents. Price-blind, so it has to be
# strict about place: same rooms, size within RELIST_SQM_TOL, same floor
# (when both state it) and the same building, and the same street when both
# addresses name one. Milan 2026-10-14: 851 live listings had an archived
# copy at another price (median −7 %); one was its own nearest comparable.
RELIST_MAX_M     = 40    # same building (Idealista coords are geocoded)
RELIST_NOFLOOR_M = 15    # floor unknown on either side → same entrance only
RELIST_SQM_TOL   = 2     # m² — the same flat gets re-measured
_STREET_WORDS = re.compile(
    r"^(via|viale|v\.le|corso|c\.so|piazza|p\.za|p\.zza|piazzale|largo|vicolo|"
    r"alzaia|ripa|strada|galleria|passaggio|bastioni|foro|vial)\b\.?\s*", re.I)


def _street(l: dict) -> tuple | None:
    """(street name key, house number or None) — only for a real street
    ("Via A. Maffei, 10" → ("maffei", "10")). Idealista's "Navigli - Porta
    Genova" style addresses name an area, not a street → None."""
    parts = [p.strip().lower() for p in (l.get("address") or "").split(",")]
    if not parts or not _STREET_WORDS.match(parts[0]):
        return None
    words = re.findall(r"[a-zà-ÿ']{3,}", _STREET_WORDS.sub("", parts[0]))
    if not words:
        return None
    num = None
    for p in parts[1:2]:
        m = re.match(r"^(\d+)", p)
        num = m.group(1) if m else None
    m = re.search(r"\b(\d+)\s*$", parts[0])
    return words[-1], num or (m.group(1) if m else None)


def _same_flat(a: dict, b: dict, streets: dict | None = None) -> bool:
    """Price-blind same-flat test. Needs a real street on both sides: an
    Idealista listing without a house number is geocoded to a street or
    area centre that many flats share, so its coordinates can't tell two
    flats apart."""
    if a.get("rooms") is None or a.get("rooms") != b.get("rooms"):
        return False
    sa, sb = a.get("sqm"), b.get("sqm")
    if not sa or not sb or abs(sa - sb) > RELIST_SQM_TOL:
        return False
    if streets is None:
        st_a, st_b = _street(a), _street(b)
    else:
        st_a = streets[id(a)] if id(a) in streets else streets.setdefault(id(a), _street(a))
        st_b = streets[id(b)] if id(b) in streets else streets.setdefault(id(b), _street(b))
    if not st_a or not st_b or st_a[0] != st_b[0]:
        return False
    if st_a[1] and st_b[1] and st_a[1] != st_b[1]:
        return False
    for l, st in ((a, st_a), (b, st_b)):
        if l.get("source") == "idealista" and not st[1]:
            return False
    la, lb = _ll(a), _ll(b)
    if not (all(la) and all(lb)):
        return False
    d = _haversine_m(float(la[0]), float(la[1]), float(lb[0]), float(lb[1]))
    fa, fb = a.get("floor_n"), b.get("floor_n")
    if fa is not None and fb is not None:
        return fa == fb and d <= RELIST_MAX_M
    return d <= RELIST_NOFLOOR_M


def _span(l: dict, cache: dict) -> tuple | None:
    """(first seen, last seen) as day ordinals, cached per listing."""
    k = id(l)
    if k not in cache:
        try:
            cache[k] = (date.fromisoformat(l.get("first_seen_date") or "").toordinal(),
                        date.fromisoformat(l.get("last_seen_date") or "").toordinal())
        except ValueError:
            cache[k] = None
    return cache[k]


def _relist_order(a: dict, b: dict, cache: dict) -> tuple | None:
    """(earlier, later, days between) when one listing ended before the
    other began (7 days' overlap allowed — agencies changing hands);
    None when they were on the market together or dates are missing."""
    sa, sb = _span(a, cache), _span(b, cache)
    if not sa or not sb:
        return None
    if sa[1] <= sb[0] + 7 and sa[0] < sb[0]:
        return a, b, sb[0] - sa[1]
    if sb[1] <= sa[0] + 7 and sb[0] < sa[0]:
        return b, a, sa[0] - sb[1]
    return None


def build_comps_index(all_listings: list[dict], relist: bool = True) -> dict:
    """
    Prepare the comps pool once per scoring run:

    • Bucket by OMI zone and fascia, preserving pool order. Every comps
      pass except the legacy radius fallback filters on omi_zona or
      omi_fascia equality, so scanning one bucket gives the same candidates
      (same order) as scanning the whole pool — without it every listing
      re-scans the pool: O(n²), ~9 min for Milan rentals.
    • Drop suspect listings (SUSPECT_FLAGS) and short-let platforms.
    • Collapse duplicate groups to one copy (see DUP_MAX_M); remember the
      groups so a listing never gets its own twins as comps.

    Pass to get_comps_benchmark(index=...). Rebuild if zone/fascia,
    suspect, price/size/floor or coordinate fields in the pool change.
    """
    skip: set[int] = set()                    # suspect + non-kept duplicates
    by_key: dict[tuple, list[dict]] = {}
    for l in all_listings:
        if any(l.get(f) for f in SUSPECT_FLAGS) or SHORTLET_PLATFORMS.search(l.get("agency_name") or ""):
            skip.add(id(l))
            continue
        k = _dup_key(l)
        if k is not None:
            by_key.setdefault(k, []).append(l)

    # Single-link groups (A~B and B~C → one group) over two relations:
    # same asking price + size + rooms + floor within DUP_MAX_M (cross-posts),
    # and — with relist — the same flat at another price (_same_flat).
    parent: dict[int, int] = {}
    obj: dict[int, dict] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: dict, b: dict) -> None:
        for m in (a, b):
            if id(m) not in parent:
                parent[id(m)] = id(m)
                obj[id(m)] = m
        ra, rb = find(id(a)), find(id(b))
        if ra != rb:
            parent[ra] = rb

    for members in by_key.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if _dup_near(a, b):
                    union(a, b)
    if LOOSE_DUPS:
        by_loose: dict[tuple, list[dict]] = {}
        for (price, _sqm, rooms, _floor), members in by_key.items():
            by_loose.setdefault((price, rooms), []).extend(members)
        for members in by_loose.values():
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    if _loose_dup(a, b):
                        union(a, b)
    # Relistings stay links (earlier → later copy), not duplicate groups:
    # merged into groups they chained identical flats of one building.
    relist_succ: dict[int, dict] = {}
    relist_prev: dict[int, list[dict]] = {}
    if relist:
        by_shape: dict[tuple, list[dict]] = {}
        for l in all_listings:
            if id(l) in skip or l.get("rooms") is None or not l.get("sqm") or not all(_ll(l)):
                continue
            by_shape.setdefault((l["rooms"], l["sqm"]), []).append(l)
        # Only one listing after the other (a relisting): two copies on the
        # market at the same time at different rents are usually two flats
        # of the same size in one building (Via Spartaco: 11 live 50–52 m²
        # flats on one floor) — they stay separate. Each earlier listing
        # links to its nearest successor only, so such buildings can't chain.
        successor: dict[int, tuple] = {}
        spans: dict[int, tuple] = {}
        streets: dict[int, tuple] = {}
        for (rooms, sqm), members in by_shape.items():
            near = [m for d in range(0, RELIST_SQM_TOL + 1)
                    for m in by_shape.get((rooms, sqm + d), ())]
            for a in members:
                for b in near:
                    if b is a or not (b["sqm"] > sqm or id(b) > id(a)):
                        continue
                    order = _relist_order(a, b, spans)
                    if order is None or not _same_flat(a, b, streets):
                        continue
                    old, new, gap = order
                    best = successor.get(id(old))
                    if best is None or (gap, abs(old["sqm"] - new["sqm"])) < best[0]:
                        successor[id(old)] = ((gap, abs(old["sqm"] - new["sqm"])), old, new)
        for _, old, new in successor.values():
            relist_succ[id(old)] = new
            relist_prev.setdefault(id(new), []).append(old)
            if old.get("is_stale"):
                skip.add(id(old))      # its successor stands for the flat now

    clusters: dict[int, list[dict]] = {}
    for l in all_listings:          # pool order inside each group
        if id(l) in parent:
            clusters.setdefault(find(id(l)), []).append(l)
    groups: list[list[int]] = []
    group_of: dict[int, int] = {}
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        keep = max(cluster, key=_dup_rank)
        gid = len(groups)
        groups.append([id(m) for m in cluster])
        for m in cluster:
            group_of[id(m)] = gid
            if m is not keep:
                skip.add(id(m))

    by_zone: dict[str, list[dict]] = {}
    by_fascia: dict[str, list[dict]] = {}
    for l in all_listings:
        if id(l) in skip:
            continue
        z = l.get("omi_zona")
        if z:
            by_zone.setdefault(z, []).append(l)
        f = l.get("omi_fascia")
        if f:
            by_fascia.setdefault(f, []).append(l)
    geo = [l for l in all_listings if id(l) not in skip and all(_ll(l))]
    return {"by_zone": by_zone, "by_fascia": by_fascia, "skip": skip,
            "by_key": by_key, "groups": groups, "group_of": group_of,
            "relist_succ": relist_succ, "relist_prev": relist_prev,
            "geo": geo,
            "geo_lat": np.array([float(_ll(l)[0]) for l in geo]),
            "geo_lon": np.array([float(_ll(l)[1]) for l in geo])}


def relist_chain(listing: dict, index: dict) -> list[dict]:
    """Earlier and later listings of the same flat (relisting links),
    nearest first in each direction."""
    out, seen = [], {id(listing)}
    todo = [listing]
    while todo:
        cur = todo.pop()
        for m in index.get("relist_prev", {}).get(id(cur), []) + \
                 ([index["relist_succ"][id(cur)]] if id(cur) in index.get("relist_succ", {}) else []):
            if id(m) not in seen:
                seen.add(id(m))
                out.append(m)
                todo.append(m)
    return out


def _twin_ids(listing: dict, index: dict) -> set[int]:
    """Object ids of the listing's own duplicates in the pool. Works for
    listings outside the pool too (matched by key + distance)."""
    twins: set[int] = set()
    g = index["group_of"].get(id(listing))
    if g is not None:
        twins.update(index["groups"][g])
    k = _dup_key(listing)
    if k is not None:
        for m in index["by_key"].get(k, ()):
            if m is listing or not _dup_near(listing, m):
                continue
            twins.add(id(m))
            mg = index["group_of"].get(id(m))
            if mg is not None:
                twins.update(index["groups"][mg])
    twins.update(id(m) for m in relist_chain(listing, index))
    twins.discard(id(listing))
    return twins


def _knn_pool(listing: dict, index: dict, is_candidate, cgroup: str,
              k: int, max_m: float, min_comps: int) -> tuple:
    """Nearest comps within max_m in the listing's OMI fascia. Same-condition
    flats first when at least 15 exist (no condition adjustment needed, like
    the zone cascade's pass 1). Returns (pool, pass_used, radius_m)."""
    lat, lon = _ll(listing)
    if not (lat and lon) or not len(index["geo"]):
        return [], "", None
    lat, lon = float(lat), float(lon)
    dy = (index["geo_lat"] - lat) * 111_195.0
    dx = (index["geo_lon"] - lon) * 111_195.0 * math.cos(math.radians(lat))
    d = np.hypot(dx, dy)
    near = np.nonzero(d <= max_m)[0]
    fascia = listing.get("omi_fascia")
    mixed, same = [], []
    # stable: Idealista rows often share a geocoded point — keep ties in pool order
    for i in near[np.argsort(d[near], kind="stable")]:
        if len(mixed) >= k and (cgroup == "unknown" or len(same) >= k):
            break
        l = index["geo"][i]
        if fascia and l.get("omi_fascia") != fascia:
            continue
        if not is_candidate(l):
            continue
        if len(mixed) < k:
            mixed.append((l, float(d[i])))
        if cgroup != "unknown" and len(same) < k and condition_group(l) == cgroup:
            same.append((l, float(d[i])))
    for pool, name, need in ((same, "knn_stratified", 15), (mixed, "knn", min_comps)):
        if len(pool) >= need:
            return [l for l, _ in pool], name, int(pool[-1][1])
    return [], "", None


def _age_days(l: dict) -> Optional[int]:
    try:
        return max(0, (date.today() - date.fromisoformat(l["last_seen_date"])).days)
    except (KeyError, TypeError, ValueError):
        return None


def _weighted_median(pairs: list) -> float:
    """Median of (value, weight) pairs sorted by value; plain median when
    all weights are equal (identical to statistics.median)."""
    if len({w for _, w in pairs}) <= 1:
        return statistics.median(v for v, _ in pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def get_comps_benchmark(
    listing: dict,
    all_listings: list[dict],
    radii: tuple[int, ...] = (500, 800, 1200),
    min_comps: int = 5,
    settings: dict | None = None,
    mode: str = 'rent',   # 'rent' or 'sale'
    index: dict | None = None,
    want_comp_ids: bool = True,
) -> dict:
    """
    Build a comps benchmark for *listing* from *all_listings*.

    Returns a dict (see module docstring for keys).
    Falls back to OMI zone if no radius yields enough comps.

    mode='rent' uses omi_loc_mid as the OMI anchor (€/m²/month).
    mode='sale' uses omi_compr_mid (€/m²) and applies a more conservative
    OMI blend when fewer than 15 sale comps are available.

    index: build_comps_index(all_listings), shared across a scoring run.
    want_comp_ids: False skips the distance sort behind `comp_ids` (the
    rental scorer doesn't store them); comp_ids is then [].
    """
    if settings:
        radii = tuple(settings.get("radii", list(radii)))
        min_comps = int(settings.get("min_comps", min_comps))
    rent = ({**RENT_BENCHMARK_DEFAULTS,
             **{k: v for k, v in (settings or {}).items() if k in RENT_BENCHMARK_DEFAULTS}}
            if mode == 'rent' else None)

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
        if id(l) in skip_ids or id(l) in twin_ids:   # set below, from the index
            return False
        if sqm_t > 0:
            comp_sqm = l.get("sqm") or 0
            if comp_sqm == 0 or not (min_sqm <= comp_sqm <= max_sqm):
                return False
        return True

    def _has_geo(l: dict) -> bool:
        return bool((l.get("latitude") or l.get("lat")) and
                    (l.get("longitude") or l.get("lng") or l.get("lon")))

    if index is None:
        index = build_comps_index(all_listings)
    skip_ids = index["skip"]
    twin_ids = _twin_ids(listing, index)
    zone_pool   = index["by_zone"].get(omi_zone, [])     if omi_zone   else []
    fascia_pool = index["by_fascia"].get(omi_fascia, []) if omi_fascia else []

    pool: list[dict] = []
    pass_used: str = ""
    radius_used = None

    # ── Rentals: nearest comparable flats (falls back to the cascade) ────────
    if rent and rent["comps_method"] == "knn":
        pool, pass_used, radius_used = _knn_pool(
            listing, index, _is_candidate, listing_cgroup,
            int(rent["knn_k"]), float(rent["knn_max_m"]), min_comps)

    # ── Pass 1: same OMI zone + same condition group (ideal pool) ─────────────
    if not pass_used and omi_zone and listing_cgroup != 'unknown':
        candidates = [l for l in zone_pool
                      if _is_candidate(l)
                      and l.get("omi_zona") == omi_zone
                      and condition_group(l) == listing_cgroup]
        if len(candidates) >= 15:
            pool = candidates
            pass_used = 'zone_stratified'

    # ── Pass 2: same OMI zone, any condition (with adjustment) ────────────────
    if not pass_used and omi_zone:
        candidates = [l for l in zone_pool
                      if _is_candidate(l) and l.get("omi_zona") == omi_zone]
        if len(candidates) >= 15:
            pool = candidates
            pass_used = 'zone_any_condition'

    # ── Pass 3: 800 m radius + same fascia ───────────────────────────────────
    if not pass_used and lat and lon and omi_fascia:
        radius_p3 = 800
        candidates = [l for l in fascia_pool
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
        candidates = [l for l in fascia_pool
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
        candidates = [l for l in zone_pool
                      if _is_candidate(l) and l.get("omi_zona") == omi_zone]
        if len(candidates) >= min_comps:
            pool = candidates
            pass_used = 'omi_zone'

    if not pool:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    # ── Price each comp like the listing ─────────────────────────────────────
    # Condition: stratified pools are same-group by definition; other pools
    # mix conditions, so each comp is multiplied by its factor. Listings with
    # 'unknown' condition keep raw prices (factor 1.0) to avoid biasing on
    # missing data.
    # Rentals also: size (€/m² falls with m²) and recency (weight halves
    # every recency_half_life_days a comp has gone unseen).
    stratified = pass_used in ('zone_stratified', 'knn_stratified')
    adjusted = not stratified and listing_cgroup != 'unknown'
    beta = float(rent["size_elasticity"] or 0) if rent else 0.0
    half_life = rent["recency_half_life_days"] if rent else None
    pairs = []
    for l in pool:
        p = _psqm(l)
        if adjusted:
            p = _adjust_price(p, condition_group(l), listing_cgroup)
        comp_sqm = l.get("sqm") or 0
        if beta and sqm_t > 0 and comp_sqm > 0:
            p *= (sqm_t / comp_sqm) ** beta
        if p <= 0:
            continue
        w = 1.0
        if half_life:
            age = _age_days(l)
            w = 0.5 ** ((age if age is not None else 120) / half_life)
        pairs.append((p, w))
    if not pairs:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    # ── IQR outlier removal (+ legacy asymmetric trim) ────────────────────────
    pairs.sort()
    n = len(pairs)
    if n >= 4:
        q1 = pairs[n // 4][0]
        q3 = pairs[(3 * n) // 4][0]
        iqr = q3 - q1
        pairs = [pv for pv in pairs if q1 - 1.5 * iqr <= pv[0] <= q3 + 1.5 * iqr] or pairs

    if not rent or rent["trim"] == "asym":
        # Cut 12 % from the top but 5 % from the bottom: put the "median" near
        # the 46th percentile — rentals use the symmetric IQR-only trim.
        n2 = len(pairs)
        if n2 >= 30:
            top_cut = max(1, int(n2 * 0.12))
            bot_cut = max(1, int(n2 * 0.05))
            pairs = pairs[bot_cut: n2 - top_cut]
        elif n2 >= 15:
            top_cut = max(1, int(n2 * 0.08))
            pairs = pairs[: n2 - top_cut]
        # < 15: no trim — every data point is precious

    if not pairs:
        return _no_comps_result(ask_psqm, omi_mid, listing_cgroup)

    s = [v for v, _ in pairs]
    median = _weighted_median(pairs)
    p40    = _percentile(s, 40)
    p60    = _percentile(s, 60)
    n_final = len(s)
    spread = (p60 - p40) / median if median > 0 else 0
    stdev_v = statistics.stdev(s) if len(s) >= 2 else 0

    # ── Confidence (per stratification spec) ──────────────────────────────────
    confidence = 0
    # A nearest-flats pool is capped at knn_k (30) by design, so "large" is
    # relative to that cap — otherwise every kNN benchmark lost 20 points
    # against zone pools and fell under the label gates (Good Value 327 →
    # 173, Idealista 134 → 1 on Milan) for no change in accuracy.
    big = max(20, int(0.8 * int(rent["knn_k"]))) if pass_used.startswith("knn") else 40
    if n_final >= big:
        confidence += 40
    elif n_final >= 20:
        confidence += 20
    if stratified:
        confidence += 20
    elif 'zone' in pass_used or pass_used == 'knn':
        confidence += 10
    confidence += 20 if stdev_v < (3.0 if mode == 'rent' else 1500) else 0
    if stratified and listing_cgroup != 'unknown':
        confidence += 20
    confidence = max(0, min(100, confidence))

    if mode == 'sale':
        blended = _blend_sale(median, omi_mid, n_final, confidence)
    elif rent and not rent["omi_blend"]:
        blended = median   # asks run ~41 % above OMI: blending pulled the benchmark low
    else:
        blended = _blend(median, omi_mid, confidence)
    delta_pct = (ask_psqm - blended) / blended if blended > 0 else 0

    # Capture the IDs of the listings that fed into the median, so the
    # detail-page comps section + mini-map can show them. Cap at 30 to keep
    # the payload reasonable (n_raw is preserved separately for the count).
    #
    # Sort priority: (1) same OMI zone first, (2) then geographic distance
    # from the listing. This matters for the cascade passes — when the
    # engine falls through to `fascia_fallback` or `omi_zone`, the pool can
    # span the whole city; sorting by €/m² (the previous behaviour) tended
    # to surface the cheapest comps which cluster in peripheral
    # neighbourhoods, producing a confusing "all blue dots are 5km away"
    # mini-map. Sorting by distance instead keeps the displayed sample
    # geographically meaningful while the median still uses the full pool.
    def _comp_sort_key(l: dict):
        same_zone = 0 if (omi_zone and l.get("omi_zona") == omi_zone) else 1
        if lat and lon:
            ll_lat = l.get("latitude") or l.get("lat")
            ll_lon = l.get("longitude") or l.get("lng") or l.get("lon")
            if ll_lat and ll_lon:
                return (same_zone, _haversine_m(lat, lon, ll_lat, ll_lon))
        # No coords on the listing or this comp → fall back to psqm
        return (same_zone, _psqm(l) or 0)

    if want_comp_ids:
        sorted_pool = sorted(pool, key=_comp_sort_key)
        comp_ids = [l.get("id") for l in sorted_pool[:30] if l.get("id") is not None]
    else:
        comp_ids = []

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
        "comp_ids":          comp_ids,
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
            "comp_ids":         [],
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
        "comp_ids":         [],
    }
