#!/usr/bin/env python3
"""
update_omi.py
─────────────
Ingest a new OMI semester release and stage it for omi_lookup.py.

Usage:
    python3 update_omi.py omi_data/QIP_*_VALORI.csv omi_data/QIP_*_ZONE.csv

What it does:
  1. Validates the two CSVs are a matching pair (same Comune code, semester).
  2. Parses every "Abitazioni civili / NORMALE" row, falling back to OTTIMO
     when NORMALE is absent for a zone.
  3. Compares the resulting ZONES dict against what omi_lookup.py currently
     ships — printing added zones, removed zones, and value diffs.
  4. Prints the exact two-line patch you'd apply to omi_lookup.py to
     activate the new release (just bump ZONE_PATH / VALORI_PATH).

Important: this script does NOT mutate omi_lookup.py automatically. The
ZONES dict is built at import time from the CSV path, so swapping the
release is a one-line edit (the patch printed at the end). Past semesters
remain in omi_data/ for audit trail — never delete.

OMI publishes new data twice yearly. Download from:
  agenziaentrate.gov.it → OMI → Quotazioni immobiliari → Ricerca testuale
Filter: Comune = MILANO (F205), Tipologia = Abitazioni civili.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    """OMI CSVs have a title line first, then the headered table — skip line 0."""
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


def _semester_from_path(p: Path) -> str:
    """QIP_1376358_1_20252_VALORI.csv  →  '2025/2'."""
    m = re.search(r"_(\d{4})(\d)_", p.name)
    return f"{m.group(1)}/{m.group(2)}" if m else "unknown"


def _build_zones(valori_rows: list[dict], zone_rows: list[dict]) -> dict[str, dict]:
    """Mirror omi_lookup._parse_valori_csv: civili NORMALE first, then OTTIMO."""
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
            _store_once(r)   # only stored if NORMALE row was absent

    zones: dict[str, dict] = {}
    for code, v in values.items():
        md = metadata.get(code, {})
        compr_min, compr_max = v["compr_min"], v["compr_max"]
        loc_min,   loc_max   = v["loc_min"],   v["loc_max"]
        zones[code] = {
            "fascia":    md.get("fascia", code[:1]),
            "descr":     md.get("descr", ""),
            "compr_min": compr_min,
            "compr_max": compr_max,
            "compr_mid": round((compr_min + compr_max) / 2) if compr_min and compr_max else None,
            "loc_min":   loc_min,
            "loc_max":   loc_max,
            "loc_mid":   round((loc_min + loc_max) / 2, 2) if loc_min and loc_max else None,
        }
    return zones


def _diff_against_current(new_zones: dict[str, dict]) -> None:
    """Print added / removed / changed zones vs whatever omi_lookup currently loads."""
    try:
        import omi_lookup
    except Exception as exc:
        print(f"  [diff] could not import omi_lookup: {exc}", file=sys.stderr)
        return

    cur = omi_lookup.ZONES
    new_set, cur_set = set(new_zones), set(cur)

    added   = sorted(new_set - cur_set)
    removed = sorted(cur_set - new_set)
    shared  = sorted(cur_set & new_set)

    print(f"\nDiff vs currently-loaded release ({len(cur)} zones):")
    if added:   print(f"  added:    {added}")
    if removed: print(f"  removed:  {removed}")
    if not added and not removed:
        print("  (no zone added or removed)")

    changes = []
    for z in shared:
        n, o = new_zones[z], cur[z]
        for f in ("loc_min", "loc_max", "compr_min", "compr_max"):
            on, oo = n.get(f), o.get(f)
            if on != oo:
                changes.append((z, f, oo, on))
    if changes:
        print(f"  value changes in shared zones: {len(changes)}")
        for z, f, oo, on in changes[:15]:
            print(f"    {z:>4} {f:<10} {oo} → {on}")
        if len(changes) > 15:
            print(f"    … +{len(changes) - 15} more")
    else:
        print("  (no value changes in shared zones)")


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python3 update_omi.py VALORI.csv ZONE.csv")
        sys.exit(1)

    valori_path = Path(sys.argv[1]).expanduser().resolve()
    zone_path   = Path(sys.argv[2]).expanduser().resolve()
    for p in (valori_path, zone_path):
        if not p.exists():
            print(f"file not found: {p}", file=sys.stderr)
            sys.exit(1)

    # Sanity: both files should belong to the same Comune + same semester
    sem_v = _semester_from_path(valori_path)
    sem_z = _semester_from_path(zone_path)
    if sem_v != sem_z:
        print(f"warning: semester mismatch — VALORI={sem_v}, ZONE={sem_z}", file=sys.stderr)

    valori = _read_csv(valori_path)
    zones  = _read_csv(zone_path)

    # Strict filter to Comune F205 (Milano) to defend against accidentally
    # loading a national-scope file.
    valori = [r for r in valori if r.get("Comune_amm", "").strip() in ("", "F205")]

    built = _build_zones(valori, zones)
    print(f"Parsed {len(built)} zones from semester {sem_v}.")
    sample = sorted(built.keys())[:8]
    for code in sample:
        z = built[code]
        print(f"  {code:<4} {z['fascia']}  loc {z['loc_min']}-{z['loc_max']} (mid {z['loc_mid']})  "
              f"compr {z['compr_min']}-{z['compr_max']} (mid {z['compr_mid']})  {z['descr'][:40]}")

    _diff_against_current(built)

    # Patch hint
    print("\nTo activate this release, edit omi_lookup.py:")
    rel_v = valori_path.relative_to(Path.cwd()) if valori_path.is_relative_to(Path.cwd()) else valori_path
    rel_z = zone_path.relative_to(Path.cwd())   if zone_path.is_relative_to(Path.cwd())   else zone_path
    print(f"  ZONE_PATH   = OMI_DATA / \"{rel_z.name}\"")
    print(f"  VALORI_PATH = OMI_DATA / \"{rel_v.name}\"")


if __name__ == "__main__":
    main()
