"""Areas the user is searching right now (focus_areas.json): their listings
get their listing page (costs) and photos read before everything else
(listing_details.build_queue, photo_eval.cmd_select)."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

# In dashboard/ since 2026-10-31: the dashboard's "My zones" filter reads it too
# (Cloudflare publishes only that folder; the repository is public anyway).
PATH = Path(__file__).resolve().parent / "dashboard" / "focus_areas.json"
_cache: tuple | None = None   # (mtime, compiled areas)


def _areas() -> list:
    global _cache
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        return []
    if _cache is None or _cache[0] != mtime:
        try:
            raw = json.loads(PATH.read_text()).get("areas") or []
        except (OSError, ValueError):
            raw = []
        _cache = (mtime, [(re.compile(a["label"], re.I) if a.get("label") else None,
                           tuple(a["center"]) if a.get("center") else None,
                           float(a.get("radius_m") or 0),
                           [tuple(p) for p in a.get("polygon") or []]) for a in raw])
    return _cache[1]


def _in_polygon(lat: float, lon: float, poly: list) -> bool:
    """Ray casting over [lat, lon] corners — an area drawn street by street
    (2026-10-31: Cadore–Montenero up to Piazzale Libia, short of Lodi TIBB)."""
    inside = False
    for i in range(len(poly)):
        (y1, x1), (y2, x2) = poly[i], poly[i - 1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _dist_m(a: tuple, b: tuple) -> float:
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    return 6371000 * math.hypot(x, (b[0] - a[0]) * r)


def in_focus(l: dict) -> bool:
    """The listing's neighbourhood matches a focus area's label, or it lies
    within that area's radius or polygon."""
    nb = l.get("neighbourhood") or ""
    lat, lon = l.get("latitude"), l.get("longitude")
    for label, center, radius, poly in _areas():
        if label and label.search(nb):
            return True
        if poly and lat and lon:
            try:
                if _in_polygon(float(lat), float(lon), poly):
                    return True
            except (TypeError, ValueError):
                pass
        if center and radius and lat and lon:
            try:
                if _dist_m(center, (float(lat), float(lon))) <= radius:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def criteria() -> dict:
    try:
        return json.loads(PATH.read_text()).get("criteria") or {}
    except (OSError, ValueError):
        return {}


def fits_criteria(l: dict, c: dict | None = None) -> bool:
    """A live flat the user would live in (focus_areas.json "criteria"): a
    focus area, the size, the floor (unknown passes), the budget — all-in
    rent for a rental, price for a sale."""
    c = criteria() if c is None else c
    if not c or l.get("is_stale") or l.get("excluded") or l.get("_excluded") or not in_focus(l):
        return False
    if (l.get("sqm") or 0) < (c.get("min_sqm") or 0):
        return False
    if l.get("floor_n") is not None and l["floor_n"] < (c.get("min_floor") or -99):
        return False
    if l.get("rent_mo"):
        cost = l.get("effective_rent_mo") or l["rent_mo"]
        return not c.get("max_rent") or cost <= c["max_rent"]
    return bool(l.get("price")) and (not c.get("max_price") or l["price"] <= c["max_price"])


# Scan areas that hold the focus areas, per site — the zone slots sweep them
# first each time the window opens (2026-10-30, the user: "always prioritise
# Corso Genova, Porta Romana, Crocetta, Guastalla, Missori and Solari").
# Read off the listings: a site's area is a priority when enough of its
# live listings are in focus. Cached by the rentals file's mtime.
_PRIORITY_CACHE: dict = {}


def priority_slugs(source: str, min_n: int = 15, min_share: float = 0.10) -> set:
    """{scan_area} of `source` with ≥ min_n live listings in a focus area
    making up ≥ min_share of the area's live listings."""
    rentals = Path(__file__).resolve().parent / "dashboard" / "milano_rentals_latest.json"
    try:
        mtime = rentals.stat().st_mtime
    except OSError:
        return set()
    key = (mtime, PATH.stat().st_mtime if PATH.exists() else 0)
    if _PRIORITY_CACHE.get("key") != key:
        try:
            rows = json.loads(rentals.read_text())
        except (OSError, ValueError):
            rows = []
        counts: dict = {}
        for l in rows:
            if l.get("is_stale") or not l.get("scan_area"):
                continue
            c = counts.setdefault((l.get("source") or "immobiliare", l["scan_area"]), [0, 0])
            c[0] += 1
            c[1] += in_focus(l)
        _PRIORITY_CACHE.clear()
        _PRIORITY_CACHE.update(key=key, counts=counts)
    return {area for (src, area), (n, f) in _PRIORITY_CACHE["counts"].items()
            if src == source and f >= min_n and f >= min_share * n}
