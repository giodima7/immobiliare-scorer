#!/usr/bin/env python3
"""
update_omi.py
─────────────
Build per-city OMI JSON files that omi_lookup.py consumes.

For each city, writes two files into omi_data/:

  omi_data/{city}_polygons.json   list of {zona, geometry: GeoJSON Polygon}
                                  Always built from the KML (geometry-only).

  omi_data/{city}_zones.json      {zona: {fascia, descr, compr_*, loc_*}}
                                  Built from the VALORI + ZONE CSVs when
                                  present. If the CSVs are missing the file
                                  is omitted — omi_lookup falls back to
                                  geometry-only mode for that city.

Data sources:
  - KMLs: project's OMI/ folder ({COMUNE_CODE}.kml, all of Italy, ~7,900
    files at ~404 MB total). Symlinked / referenced — NOT committed.
  - CSVs: omi_data/QIP_*_VALORI.csv + QIP_*_ZONE.csv. Downloaded from
    Agenzia delle Entrate per comune. The CSV filename embeds the
    comune's internal database ID (e.g. QIP_1376358 for Milan), so
    auto-discovery uses the Comune_amm column to match instead of the
    filename — works whether you've downloaded one comune or fifty.

Usage:
  python3 update_omi.py                 # process every city in CITY_MAP
  python3 update_omi.py --city roma     # one city
  python3 update_omi.py --list          # show what data is available
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR     = Path(__file__).parent
OMI_DATA_DIR = BASE_DIR / "omi_data"
KML_DIR      = BASE_DIR / "OMI"          # 7,887-file bulk download (gitignored)
OMI_DATA_DIR.mkdir(exist_ok=True)

# city code → ISTAT comune code. Mirrors the Supabase cities table.
CITY_MAP: dict[str, str] = {
    "milano":       "F205",
    "roma":         "H501",
    "napoli":       "F839",
    "la_maddalena": "E425",
}

KML_NS = "{http://www.opengis.net/kml/2.2}"


# ── CSV helpers ──────────────────────────────────────────────────────────────
def _read_csv(path: Path) -> list[dict]:
    """OMI CSVs lead with a title row, then the header. Skip line 0."""
    with open(path, encoding="latin-1") as fh:
        lines = fh.readlines()
    return list(csv.DictReader(io.StringIO("".join(lines[1:])), delimiter=";"))


def _parse_num(s: str) -> float | None:
    if not s or not s.strip():
        return None
    try:
        return float(s.strip().replace(",", "."))
    except ValueError:
        return None


def _find_csvs_for_comune(comune_code: str) -> tuple[Path | None, Path | None]:
    """
    Find the VALORI + ZONE CSVs that contain rows for the given Comune_amm.
    Returns (valori_path, zone_path) or (None, None) when no matching files
    exist in omi_data/.
    """
    valori_match: Path | None = None
    zone_match:   Path | None = None
    for p in OMI_DATA_DIR.glob("QIP_*_VALORI.csv"):
        try:
            for r in _read_csv(p):
                if r.get("Comune_amm", "").strip() == comune_code:
                    valori_match = p
                    break
        except Exception:
            continue
        if valori_match:
            break
    for p in OMI_DATA_DIR.glob("QIP_*_ZONE.csv"):
        try:
            for r in _read_csv(p):
                if r.get("Comune_amm", "").strip() == comune_code:
                    zone_match = p
                    break
        except Exception:
            continue
        if zone_match:
            break
    return valori_match, zone_match


# ── Build zones.json from VALORI + ZONE CSVs ─────────────────────────────────
def build_zones_json(comune_code: str, valori_path: Path, zone_path: Path,
                     city: str) -> dict:
    """
    Build {city}_zones.json. Mirrors the legacy parser:
      Priority 1 — Abitazioni civili (Cod_Tip=20) NORMALE
      Priority 2 — Cod_Tip=20 OTTIMO (only if NORMALE missing)
    """
    valori_rows = [r for r in _read_csv(valori_path)
                   if r.get("Comune_amm", "").strip() == comune_code]
    zone_rows   = [r for r in _read_csv(zone_path)
                   if r.get("Comune_amm", "").strip() == comune_code]

    metadata: dict[str, dict] = {}
    for r in zone_rows:
        code = r.get("Zona", "").strip()
        if code:
            metadata[code] = {
                "fascia": r.get("Fascia", "").strip(),
                "descr":  r.get("Zona_Descr", "").strip().strip("'"),
            }

    values: dict[str, dict] = {}
    def _store_once(row: dict) -> None:
        code = row.get("Zona", "").strip()
        if not code or code in values:
            return
        values[code] = {
            "loc_min":   _parse_num(row.get("Loc_min", "")),
            "loc_max":   _parse_num(row.get("Loc_max", "")),
            "compr_min": _parse_num(row.get("Compr_min", "")),
            "compr_max": _parse_num(row.get("Compr_max", "")),
        }

    for r in valori_rows:
        if r.get("Cod_Tip", "").strip() == "20" and r.get("Stato", "").strip() == "NORMALE":
            _store_once(r)
    for r in valori_rows:
        if r.get("Cod_Tip", "").strip() == "20" and r.get("Stato", "").strip() == "OTTIMO":
            _store_once(r)

    result: dict[str, dict] = {}
    for code, v in values.items():
        md = metadata.get(code, {})
        cmin, cmax = v["compr_min"], v["compr_max"]
        lmin, lmax = v["loc_min"],   v["loc_max"]
        result[code] = {
            "zona":       code,
            "fascia":     md.get("fascia", code[:1]),
            "descr":      md.get("descr", ""),
            "compr_min":  cmin,
            "compr_max":  cmax,
            "compr_mid":  round((cmin + cmax) / 2) if cmin and cmax else None,
            "loc_min":    lmin,
            "loc_max":    lmax,
            "loc_mid":    round((lmin + lmax) / 2, 2) if lmin and lmax else None,
        }

    out = OMI_DATA_DIR / f"{city}_zones.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"  [{city}] {len(result):>3} zones (with prices) → {out.name}")
    return result


# ── Build polygons.json from KML ─────────────────────────────────────────────
def _parse_coord_block(text: str) -> list[list[float]]:
    pts: list[list[float]] = []
    for tok in text.strip().split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            pts.append([float(parts[0]), float(parts[1])])  # [lng, lat]
        except ValueError:
            continue
    return pts


def _placemark_zona_code(pm) -> str | None:
    """
    Extract the OMI zone code from a Placemark. The pattern across all
    7,887 files is `<name>COMUNE - Zona OMI <CODE></name>`. Fall back to
    the description CDATA table (which carries Zona too) if the name
    pattern doesn't match.
    """
    name_el = pm.find(f"{KML_NS}name")
    if name_el is not None and name_el.text:
        m = re.search(r"Zona OMI\s+(\S+)", name_el.text)
        if m:
            return m.group(1).strip()
    desc_el = pm.find(f"{KML_NS}description")
    if desc_el is not None and desc_el.text:
        m = re.search(r"Zona OMI</b></td><td>\s*(\S+?)\s*<", desc_el.text)
        if m:
            return m.group(1).strip()
    return None


def build_polygons_json(comune_code: str, city: str) -> int:
    """
    Parse the comune's KML (OMI/{comune_code}.kml) and write
    omi_data/{city}_polygons.json as a list of {zona, geometry: Polygon}.

    Handles both single-Polygon and MultiGeometry Placemarks (some zones
    are split across multiple disjoint polygons in the source KML).
    """
    kml_path = KML_DIR / f"{comune_code}.kml"
    if not kml_path.exists():
        # Milan has a separate F205.kml at the project root for back-compat
        # with the legacy code path. Fall back to it when needed.
        alt = BASE_DIR / f"{comune_code}.kml"
        if alt.exists():
            kml_path = alt
        else:
            print(f"  [{city}] KML not found: {kml_path}", file=sys.stderr)
            return 0

    tree = ET.parse(str(kml_path))
    root = tree.getroot()

    out: list[dict] = []
    for pm in root.iter(f"{KML_NS}Placemark"):
        zona = _placemark_zona_code(pm)
        if not zona:
            continue

        polygons: list[list[list[list[float]]]] = []  # GeoJSON: list of rings
        for poly_el in pm.findall(f".//{KML_NS}Polygon"):
            outer_el = poly_el.find(f".//{KML_NS}outerBoundaryIs//{KML_NS}coordinates")
            if outer_el is None or not outer_el.text:
                continue
            outer = _parse_coord_block(outer_el.text)
            if len(outer) < 3:
                continue
            holes_coords: list[list[list[float]]] = []
            for inner_el in poly_el.findall(f".//{KML_NS}innerBoundaryIs//{KML_NS}coordinates"):
                if inner_el is not None and inner_el.text:
                    inner = _parse_coord_block(inner_el.text)
                    if len(inner) >= 3:
                        holes_coords.append(inner)
            polygons.append([outer] + holes_coords)

        if not polygons:
            continue
        if len(polygons) == 1:
            geom = {"type": "Polygon", "coordinates": polygons[0]}
        else:
            geom = {"type": "MultiPolygon", "coordinates": polygons}
        out.append({"zona": zona, "geometry": geom})

    out_path = OMI_DATA_DIR / f"{city}_polygons.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False))
    print(f"  [{city}] {len(out):>3} polygons → {out_path.name}")
    return len(out)


# ── Orchestration ────────────────────────────────────────────────────────────
def process_city(city: str) -> None:
    if city not in CITY_MAP:
        print(f"[update_omi] unknown city '{city}' — known: {list(CITY_MAP)}",
              file=sys.stderr)
        return
    comune_code = CITY_MAP[city]
    print(f"\n[update_omi] {city} ({comune_code}):")

    n_polys = build_polygons_json(comune_code, city)
    if n_polys == 0:
        print(f"  [{city}] no polygons → skipping zones step")
        return

    valori, zone = _find_csvs_for_comune(comune_code)
    if valori and zone:
        build_zones_json(comune_code, valori, zone, city)
    else:
        # No CSVs yet — leave zones.json absent. omi_lookup synthesises stubs
        # from the polygons so listings still get a zona code.
        zones_path = OMI_DATA_DIR / f"{city}_zones.json"
        if zones_path.exists():
            print(f"  [{city}] no CSVs found — keeping existing {zones_path.name}")
        else:
            print(f"  [{city}] no VALORI/ZONE CSV with Comune_amm={comune_code} — "
                  f"geometry-only mode")


def list_status() -> None:
    print(f"\nKML source: {KML_DIR} "
          f"({sum(1 for _ in KML_DIR.glob('*.kml'))} files)" if KML_DIR.exists()
          else f"\nKML source: {KML_DIR} (MISSING)")
    print(f"Output dir: {OMI_DATA_DIR}\n")
    print(f"{'city':<14}{'comune':<8}{'kml':<6}{'csvs':<6}{'polygons.json':<18}{'zones.json':<12}")
    print("-" * 70)
    for city, code in CITY_MAP.items():
        kml_exists = (KML_DIR / f"{code}.kml").exists() or (BASE_DIR / f"{code}.kml").exists()
        valori, zone = _find_csvs_for_comune(code)
        poly_out = OMI_DATA_DIR / f"{city}_polygons.json"
        zone_out = OMI_DATA_DIR / f"{city}_zones.json"
        print(f"{city:<14}{code:<8}"
              f"{'✓' if kml_exists else '✗':<6}"
              f"{'✓' if (valori and zone) else '✗':<6}"
              f"{'✓' if poly_out.exists() else '✗':<18}"
              f"{'✓' if zone_out.exists() else '✗':<12}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=list(CITY_MAP), help="single city")
    ap.add_argument("--list", action="store_true",
                    help="print availability matrix, don't build")
    args = ap.parse_args()

    if args.list:
        list_status()
        return

    cities = [args.city] if args.city else list(CITY_MAP)
    for city in cities:
        process_city(city)
    print("\n[update_omi] done")


if __name__ == "__main__":
    main()
