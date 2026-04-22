#!/usr/bin/env python3
"""
omi_lookup.py
─────────────
Point-in-polygon OMI zone lookup for Milano rental listings.

Parses three official data files at import time (< 2 s) and builds
a ZONES dict.  Call lookup(lat, lng) once per listing.

Data files (project root):
  F205.kml                        — 43 OMI zone polygons, coord order lon,lat,alt
  QIP_1363767_1_20252_ZONE.csv    — zone metadata (fascia, description)
  QIP_1363767_1_20252_VALORI.csv  — rental + purchase price benchmarks

Usage:
    from omi_lookup import lookup, ZONES
    zone, source = lookup(45.4641, 9.1919)   # (lat, lng)

    python3 omi_lookup.py --test
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from shapely.geometry import MultiPolygon, Point, Polygon

_log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent
KML_PATH    = BASE_DIR / "F205.kml"
ZONE_PATH   = BASE_DIR / "QIP_1363767_1_20252_ZONE.csv"
VALORI_PATH = BASE_DIR / "QIP_1363767_1_20252_VALORI.csv"

KML_NS = "http://www.opengis.net/kml/2.2"

FASCIA_LABEL: dict[str, str] = {
    "B": "Centrale",
    "C": "Semicentrale",
    "D": "Periferica",
    "E": "Periferica",
    "R": "Rurale",
}

# Populated at module import; empty if _INIT_ERROR is set
ZONES: dict[str, dict] = {}
_INIT_ERROR: Optional[str] = None


# ── KML parsing ────────────────────────────────────────────────────────────────

def _parse_coords(text: str) -> list[tuple[float, float]]:
    """Parse KML <coordinates> text → [(lon, lat), ...].  KML order is lon,lat,alt."""
    pts: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return pts


def _build_polygon(poly_el) -> Optional[Polygon]:
    """Build a shapely Polygon from a KML <Polygon> element (supports inner holes)."""
    ns = KML_NS
    outer_el = poly_el.find(f".//{{{ns}}}outerBoundaryIs//{{{ns}}}coordinates")
    if outer_el is None or not outer_el.text:
        return None
    outer = _parse_coords(outer_el.text)
    if len(outer) < 3:
        return None
    holes = [
        _parse_coords(el.text)
        for el in poly_el.findall(f".//{{{ns}}}innerBoundaryIs//{{{ns}}}coordinates")
        if el.text and len(_parse_coords(el.text)) >= 3
    ]
    try:
        p = Polygon(outer, holes)
        return p if p.is_valid else p.buffer(0)   # fix self-intersections
    except Exception:
        return None


def _parse_kml() -> dict[str, object]:
    """Return {zone_code: shapely geometry} for all 43 Placemarks."""
    ns = KML_NS
    tree = ET.parse(str(KML_PATH))
    root = tree.getroot()

    geoms: dict[str, object] = {}
    for pm in root.iter(f"{{{ns}}}Placemark"):
        name_el = pm.find(f"{{{ns}}}name")
        if name_el is None or not name_el.text:
            continue
        m = re.search(r"Zona OMI (\w+)", name_el.text)
        if not m:
            continue
        code = m.group(1)

        # Collect all <Polygon> elements (direct child or nested in <MultiGeometry>)
        polys = [p for el in pm.findall(f".//{{{ns}}}Polygon")
                 if (p := _build_polygon(el)) is not None]
        if not polys:
            continue
        geoms[code] = MultiPolygon(polys) if len(polys) > 1 else polys[0]

    return geoms


# ── CSV parsing ────────────────────────────────────────────────────────────────

def _csv_reader(path: Path) -> csv.DictReader:
    """Open CSV, skip the title row (row 0), use row 1 as header."""
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    # row 0 is the title, row 1 is the header
    return csv.DictReader(io.StringIO("".join(lines[1:])), delimiter=";")


def _parse_zone_csv() -> dict[str, dict]:
    """{zone_code: {fascia, descr}} from ZONE CSV."""
    result: dict[str, dict] = {}
    for row in _csv_reader(ZONE_PATH):
        code = row.get("Zona", "").strip()
        if not code:
            continue
        result[code] = {
            "fascia": row.get("Fascia", "").strip(),
            "descr":  row.get("Zona_Descr", "").strip().strip("'"),
        }
    return result


def _parse_float(s: str) -> Optional[float]:
    """Parse Italian decimal (comma) or dot decimal string → float."""
    try:
        return float(s.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _parse_valori_csv() -> dict[str, dict]:
    """{zone_code: {loc_min, loc_max, compr_min, compr_max}} from VALORI CSV.

    Priority 1: Cod_Tip=20 (Abitazioni civili) AND Stato=NORMALE.
    Priority 2: Cod_Tip=20 AND Stato=OTTIMO (fallback for zones without NORMALE row).
    """
    all_rows = list(_csv_reader(VALORI_PATH))
    result: dict[str, dict] = {}

    def _store(row: dict) -> None:
        code = row.get("Zona", "").strip()
        if not code or code in result:
            return
        result[code] = {
            "loc_min":   _parse_float(row.get("Loc_min", "")),
            "loc_max":   _parse_float(row.get("Loc_max", "")),
            "compr_min": _parse_float(row.get("Compr_min", "")),
            "compr_max": _parse_float(row.get("Compr_max", "")),
        }

    for row in all_rows:
        if row.get("Cod_Tip", "").strip() == "20" and row.get("Stato", "").strip() == "NORMALE":
            _store(row)

    for row in all_rows:
        if row.get("Cod_Tip", "").strip() == "20" and row.get("Stato", "").strip() == "OTTIMO":
            _store(row)  # only stored if NORMALE row was absent

    return result


# ── Build ZONES ────────────────────────────────────────────────────────────────

def _build_zones() -> dict[str, dict]:
    geoms   = _parse_kml()
    zone_md = _parse_zone_csv()
    valori  = _parse_valori_csv()

    zones: dict[str, dict] = {}
    for code, geom in geoms.items():
        md  = zone_md.get(code, {})
        val = valori.get(code, {})

        loc_min  = val.get("loc_min")
        loc_max  = val.get("loc_max")
        loc_mid  = round((loc_min + loc_max) / 2, 2) if loc_min is not None and loc_max is not None else None

        compr_min = val.get("compr_min")
        compr_max = val.get("compr_max")
        compr_mid = round((compr_min + compr_max) / 2, 0) if compr_min is not None and compr_max is not None else None

        fascia = md.get("fascia", "")
        zones[code] = {
            "zona":        code,
            "fascia":      fascia,
            "fascia_label": FASCIA_LABEL.get(fascia, "Periferica"),
            "descr":       md.get("descr", ""),
            "loc_min":     loc_min,
            "loc_max":     loc_max,
            "loc_mid":     loc_mid,
            "compr_min":   compr_min,
            "compr_max":   compr_max,
            "compr_mid":   compr_mid,
            "geometry":    geom,
        }

    return zones


# ── Initialise at import ───────────────────────────────────────────────────────

try:
    _t0    = time.perf_counter()
    ZONES  = _build_zones()
    _elapsed = time.perf_counter() - _t0
    print(f"[omi_lookup] {len(ZONES)} zones loaded in {_elapsed:.2f}s", file=sys.stderr)
except Exception as _exc:
    _INIT_ERROR = str(_exc)
    print(f"[omi_lookup] INIT FAILED: {_exc}", file=sys.stderr)


# ── Public lookup ──────────────────────────────────────────────────────────────

def lookup(lat: float, lng: float) -> tuple[Optional[dict], str]:
    """
    Return (zone_dict, source) for a coordinate pair.

    source values:
      'polygon'  — point is contained within a zone polygon (exact match)
      'centroid' — no polygon contained the point; matched to nearest centroid
      'failed'   — ZONES not loaded or geometry error

    Input is (lat, lng); internally converted to Point(lng, lat) to match
    KML lon,lat coordinate order.

    The returned zone_dict is a reference into ZONES — do NOT mutate it.
    """
    if _INIT_ERROR or not ZONES:
        return None, "failed"

    try:
        pt = Point(lng, lat)   # shapely uses (x=lon, y=lat)

        # Pass 1: exact containment
        for code, z in ZONES.items():
            if z["geometry"].contains(pt):
                return z, "polygon"

        # Pass 2: nearest centroid (edge/boundary case)
        best_code: Optional[str] = None
        best_dist = float("inf")
        for code, z in ZONES.items():
            d = pt.distance(z["geometry"].centroid)
            if d < best_dist:
                best_dist, best_code = d, code

        if best_code:
            z = ZONES[best_code]
            _log.warning(
                "[omi_lookup] centroid fallback (%.4f, %.4f) → %s (dist=%.4f°)",
                lat, lng, best_code, best_dist,
            )
            return z, "centroid"

        return None, "failed"

    except Exception as exc:
        _log.error("[omi_lookup] lookup error (%.4f, %.4f): %s", lat, lng, exc)
        return None, "failed"


# ── Self-test ──────────────────────────────────────────────────────────────────

def _run_test() -> None:
    TEST_COORDS = [
        (45.4641, 9.1919, "Duomo",           "B12 or B15"),
        (45.5199, 9.1936, "Niguarda",         "D33"),
        (45.5061, 9.1385, "Quarto Oggiaro",   "E8"),
        (45.4524, 9.1734, "Navigli",          "B21 or C18"),
        (45.4856, 9.2034, "Lambrate",         "D35 or D10"),
    ]
    print(f"omi_lookup self-test — {len(ZONES)} zones loaded\n")
    for lat, lng, name, expected in TEST_COORDS:
        z, src = lookup(lat, lng)
        if z:
            print(f"▶ {name} ({lat}, {lng})  — expected {expected}")
            print(f"  zone:    {z['zona']}  (fascia {z['fascia']} — {z['fascia_label']})")
            print(f"  descr:   {z['descr']}")
            if z['loc_min'] is not None:
                print(f"  rent:    {z['loc_min']}–{z['loc_max']} €/m²/mo  (mid {z['loc_mid']})")
            else:
                print(f"  rent:    — no data")
            if z['compr_min'] is not None:
                print(f"  purchase:{z['compr_min']}–{z['compr_max']} €/m²")
            print(f"  source:  {src}")
        else:
            print(f"▶ {name}: FAILED")
        print()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Self-test on five Milan coordinates")
    args = ap.parse_args()
    if args.test:
        _run_test()
    else:
        ap.print_help()
