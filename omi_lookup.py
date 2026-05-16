#!/usr/bin/env python3
"""
omi_lookup.py
─────────────
Multi-city point-in-polygon OMI zone lookup.

Each city ships two JSON files in omi_data/, both produced by
update_omi.py from the raw Agenzia delle Entrate downloads:

  omi_data/{city}_polygons.json   — list of {zona, geometry} (KML → GeoJSON)
  omi_data/{city}_zones.json      — {zona: {fascia, descr, compr_*, loc_*}}

When zones.json is empty / missing values (cities whose VALORI CSV
hasn't been downloaded yet) the geometry still resolves to a zona code
but the price benchmark fields come back as None — scoring then falls
back to comps-only signal for those listings.

Pipelines call the city-aware API:

    from omi_lookup import lookup_for_city
    zone, source = lookup_for_city(lat, lng, city='milano')

The legacy Milan-only API (`lookup()`, `ZONES`) stays in place so the
existing scoring + dashboard code keeps working until each callsite is
explicitly migrated to take a `city` arg.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from shapely.geometry import MultiPolygon, Point, Polygon, shape

_log = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).parent
OMI_DATA   = BASE_DIR / "omi_data"


# ── City registry ────────────────────────────────────────────────────────────
# Mirrors the Supabase `cities` table. Keep in sync (or migrate to load this
# at startup from the DB in the future).
CITY_REGISTRY: dict[str, dict] = {
    "milano":       {"comune_code": "F205", "province": "MI"},
    "roma":         {"comune_code": "H501", "province": "RM"},
    "napoli":       {"comune_code": "F839", "province": "NA"},
    "la_maddalena": {"comune_code": "E425", "province": "SS"},
}

FASCIA_LABEL: dict[str, str] = {
    "A": "Pregio",
    "B": "Centrale",
    "C": "Semicentrale",
    "D": "Periferica",
    "E": "Periferica",
    "R": "Rurale",
}


# ── Per-city cache ───────────────────────────────────────────────────────────
# Each entry: {"zones": dict[code, meta], "polygons": [{zona, geom}]}.
_city_cache: dict[str, dict] = {}


def _zones_json_path(city: str) -> Path:
    return OMI_DATA / f"{city}_zones.json"


def _polygons_json_path(city: str) -> Path:
    return OMI_DATA / f"{city}_polygons.json"


def _load_city(city: str) -> dict:
    """
    Lazy-load a city's zone + polygon data and cache it.
    Returns {"zones": {code: meta}, "polygons": [{zona, geom}]} or
    raises FileNotFoundError if neither file exists.
    """
    if city in _city_cache:
        return _city_cache[city]

    zp = _zones_json_path(city)
    pp = _polygons_json_path(city)
    if not pp.exists():
        raise FileNotFoundError(
            f"OMI polygon data for '{city}' not found at {pp}. "
            f"Run: python3 update_omi.py --city {city}"
        )

    polygons_raw = json.loads(pp.read_text())
    polygons: list[dict] = []
    for entry in polygons_raw:
        try:
            geom = shape(entry["geometry"])
            if not geom.is_valid:
                geom = geom.buffer(0)
            polygons.append({"zona": entry["zona"], "geom": geom})
        except Exception as exc:
            _log.warning("[%s] skipping polygon %s: %s",
                         city, entry.get("zona"), exc)

    zones: dict[str, dict] = {}
    if zp.exists():
        zones = json.loads(zp.read_text())

    # Backfill stubs for any zone that has a polygon but no zones-json row
    # (happens when KML is downloaded but VALORI/ZONE CSVs aren't yet).
    for p in polygons:
        code = p["zona"]
        if code not in zones:
            zones[code] = {
                "zona":         code,
                "fascia":       code[:1] if code else "",
                "fascia_label": FASCIA_LABEL.get(code[:1] if code else "", "Periferica"),
                "descr":        "",
                "loc_min":      None, "loc_max":   None, "loc_mid":   None,
                "compr_min":    None, "compr_max": None, "compr_mid": None,
            }
        else:
            # Ensure derived fields exist on rows produced by older update_omi.
            zones[code].setdefault("zona", code)
            zones[code].setdefault(
                "fascia_label",
                FASCIA_LABEL.get(zones[code].get("fascia", ""), "Periferica"),
            )

    _city_cache[city] = {"zones": zones, "polygons": polygons}
    return _city_cache[city]


def lookup_for_city(lat: float, lng: float, city: str = "milano",
                    centroid_fallback: bool = True) -> tuple[Optional[dict], str]:
    """
    Find the OMI zone containing (lat, lng) for the given city.

    Returns (zone_dict, source). zone_dict mirrors the legacy shape:
        {zona, fascia, fascia_label, descr,
         loc_min, loc_max, loc_mid, compr_min, compr_max, compr_mid}

    Sources:
      'polygon'  exact contain
      'centroid' fallback to nearest polygon centroid
      'failed'   nothing loaded
    """
    try:
        cd = _load_city(city)
    except FileNotFoundError as exc:
        _log.error("%s", exc)
        return None, "failed"
    except Exception as exc:
        _log.error("[omi] load failed for %s: %s", city, exc)
        return None, "failed"

    pt = Point(lng, lat)  # shapely: (x=lng, y=lat)

    # Pass 1: exact containment
    for p in cd["polygons"]:
        try:
            if p["geom"].contains(pt):
                return cd["zones"].get(p["zona"]), "polygon"
        except Exception:
            continue

    if not centroid_fallback:
        return None, "failed"

    # Pass 2: nearest centroid (edge / boundary case)
    best, best_dist = None, float("inf")
    for p in cd["polygons"]:
        try:
            d = pt.distance(p["geom"].centroid)
            if d < best_dist:
                best_dist = d
                best      = p
        except Exception:
            continue

    if best is not None:
        _log.warning("[omi] centroid fallback (%.4f,%.4f) in %s → %s (d=%.4f°)",
                     lat, lng, city, best["zona"], best_dist)
        return cd["zones"].get(best["zona"]), "centroid"

    return None, "failed"


def load_city_zones(city: str) -> dict[str, dict]:
    """Public accessor used by scoring.py to compute city-relative LDI."""
    return _load_city(city)["zones"]


# ── Legacy API (Milan-only, single-import side effect) ───────────────────────
# Kept so unchanged callers keep working. New code should use
# lookup_for_city() and pass the listing's `city` field explicitly.
ZONES: dict[str, dict] = {}
_INIT_ERROR: Optional[str] = None

try:
    _t0 = time.perf_counter()
    _milano = _load_city("milano")
    # ZONES keys are the bare zone codes (e.g. "B12") so legacy callers
    # iterating ZONES.items() still get the same shape.
    ZONES = {code: dict(meta, geometry=None) for code, meta in _milano["zones"].items()}
    # Attach geometry to each entry by joining with the polygons list
    for p in _milano["polygons"]:
        if p["zona"] in ZONES:
            ZONES[p["zona"]]["geometry"] = p["geom"]
    print(f"[omi_lookup] {len(ZONES)} zones loaded for milano in "
          f"{time.perf_counter() - _t0:.2f}s", file=sys.stderr)
except Exception as _exc:
    _INIT_ERROR = str(_exc)
    print(f"[omi_lookup] INIT FAILED for milano: {_exc}", file=sys.stderr)


def lookup(lat: float, lng: float) -> tuple[Optional[dict], str]:
    """Legacy Milan-only wrapper. Equivalent to lookup_for_city(..., 'milano')."""
    return lookup_for_city(lat, lng, city="milano")


# ── Self-test ────────────────────────────────────────────────────────────────
def _run_test() -> None:
    tests = [
        ("milano", 45.4641, 9.1919, "Duomo", "B12 or B15"),
        ("milano", 45.5199, 9.1936, "Niguarda", "D33"),
        ("roma",   41.9028, 12.4964, "Centro Roma", "?"),
        ("napoli", 40.8518, 14.2681, "Centro Napoli", "?"),
        ("la_maddalena", 41.2133, 9.4063, "La Maddalena centro", "?"),
    ]
    for city, lat, lng, name, expected in tests:
        z, src = lookup_for_city(lat, lng, city)
        if z:
            rent = f"{z['loc_min']}-{z['loc_max']} €/m²/mo" if z.get("loc_min") else "no price data"
            print(f"▶ [{city}] {name} ({lat},{lng}) → {z['zona']} ({z.get('fascia','?')}) "
                  f"— {rent} — {src}")
        else:
            print(f"▶ [{city}] {name}: FAILED ({src})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        _run_test()
    else:
        ap.print_help()
