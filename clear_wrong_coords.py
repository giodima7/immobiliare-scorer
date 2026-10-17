"""
One-shot Fix A — find rentals where coordinates landed in Duomo (the
classic Nominatim default-fallback target) but the listing's
neighbourhood clearly belongs to a peripheral area. Clear their geo
fields so the next geo-enrich pass re-geocodes them using the
fixed-2026-05-22 prefer-specific-neighbourhood logic in enrich_geo.

Detection: distance-from-Duomo < 1 km AND neighbourhood matches a
curated list of definitively-peripheral Milan zones (2-12 km from Duomo
in real life). The curated list is exact-match — substring matching
gave false positives ("Guastalla", "Borgogna" both share substrings
with peripheral names).

What gets cleared:
  latitude, longitude            — force re-geocoding
  metro_nearest_name, _dist_m,
  metro_nearest_line, walk_min   — derived from coords
  omi_zona, omi_fascia, omi_descr,
  omi_loc_*, omi_compr_*         — derived from coords
  omi_source                     — force OMI re-lookup
  geo_score                      — derived from POI distances
  metro_walk_routed              — derived

What stays untouched:
  rent_mo, sqm, rooms, bedrooms, title, address, neighbourhood
  source, id, photos, condition, etc.
  comps_*, score_* — will get recomputed by the rescore pass once
                     OMI/coords are correct.

Also drops the matching rows from enrichment_cache so the next enrich
pass doesn't immediately re-stamp the bad geo from cache.

Usage:
    python3 clear_wrong_coords.py             # dry-run, lists candidates
    python3 clear_wrong_coords.py --apply     # actually clear them
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
RENTAL_PATH   = DASHBOARD_DIR / "milano_rentals_latest.json"
LEGACY_RENTAL = DASHBOARD_DIR / "rentals_latest.json"
SALE_PATH     = DASHBOARD_DIR / "milano_sales_latest.json"

# Duomo coords — the Nominatim default for unresolved Milan queries.
DUOMO_LAT, DUOMO_LNG = 45.4641, 9.1919
DUOMO_RADIUS_KM      = 1.0          # how close to Duomo counts as "centre"

# Curated list of EXACT neighbourhood names that cannot legitimately
# fall within 1 km of Duomo. Verified by Milan geography — every entry
# below is 2-12 km from Duomo. Adding new entries is safe; the false-
# positive cost is missing some real wrong-coord listings, not
# corrupting legit ones.
PERIPHERAL_NEIGHBOURHOODS = {
    # West / north-west periphery
    "cascina merlata - musocco", "cascina merlata", "musocco", "uptown",
    "viale certosa", "uptown - cascina merlata", "uptown - viale certosa",
    "san siro", "trenno", "gallaratese", "qt8", "monte stella", "figino",
    "san carlo",
    "bisceglie", "baggio", "olmi", "quartiere olmi", "quarto cagnino",
    "quinto romano", "muggiano",
    "bande nere", "inganni", "gambara", "lorenteggio", "piazzale siena",
    "primaticcio",
    # North
    "affori", "bovisa", "bovisasca", "dergano", "comasina",
    "quarto oggiaro", "roserio", "vialba",
    "bicocca", "niguarda", "bicocca - niguarda", "bruzzano",
    "ca' granda", "ca granda", "bignami - ponale",
    "greco - segnano", "prato centenaro",
    "maggiolina", "istria", "maggiolina - istria",
    "precotto", "turro", "gorla", "villa san giovanni",
    "pasteur", "rovereto", "parco trotter",
    # East
    "lambrate", "rubattino", "ortica", "udine", "parco lambro",
    "quartiere feltre",
    "cimiano", "crescenzago", "adriano", "quartiere adriano",
    "cascina gobba", "ponte nuovo",
    "argonne - corsica", "casoretto",
    # South-east
    "forlanini", "monlue", "viale ungheria - mecenate",
    "rogoredo", "corvetto", "corvetto - rogoredo",
    "ponte lambro", "santa giulia", "ponte lambro - santa giulia",
    # South
    "lodi - brenta", "porta vittoria - lodi", "molise - cuoco",
    "bologna - sulmona", "martini - insubria",
    "ripamonti", "vigentino", "ripamonti - vigentino",
    "scalo romana", "ripamonti - fondazione prada",
    "fondazione prada - scalo romana", "fondazione prada",
    "vigentino - fatima", "quintosole - chiaravalle",
    "abbiategrasso", "chiesa rossa", "abbiategrasso - chiesa rossa",
    "gratosoglio", "pezzotti - meda", "cermenate - abbiategrasso",
    # South-west
    "barona", "famagosta", "famagosta - barona", "cantalupa - san paolo",
    "giambellino", "tre castelli - faenza", "romolo",
    "napoli - soderini", "soderini", "piazza napoli", "tripoli - soderini",
    # South-west / Corso San Gottardo cluster (~1.4 km south of Duomo;
    # the bad geocode lands at Duomo, real coords are at Vetra/Romolo).
    "corso san gottardo",
}

GEO_FIELDS_TO_CLEAR = (
    "latitude", "longitude",
    "metro_nearest_name", "metro_nearest_line",
    "metro_nearest_dist_m", "metro_walk_min", "metro_walk_routed",
    "metro_walk_grade",
    "omi_zona", "omi_fascia", "omi_descr",
    "omi_loc_mid", "omi_loc_min", "omi_loc_max",
    "omi_compr_mid", "omi_compr_min", "omi_compr_max",
    "omi_source", "omi_fallback",
    "geo_score",
    "park_nearest_dist_m", "supermarket_nearest_dist_m",
    "university_nearest_dist_m", "tram_nearest_dist_m",
)


def _normalise(name: str) -> str:
    """Lowercase + unicode-strip a neighbourhood string for matching."""
    s = unicodedata.normalize("NFKD", name or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()


def _km_from_duomo(lat, lng) -> float | None:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    R = 6371.0
    dlat = math.radians(lat - DUOMO_LAT)
    dlng = math.radians(lng - DUOMO_LNG)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(DUOMO_LAT))
         * math.cos(math.radians(lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def is_misgeocoded(listing: dict) -> bool:
    """True if coords are near Duomo but neighbourhood is peripheral."""
    dist = _km_from_duomo(listing.get("latitude"), listing.get("longitude"))
    if dist is None or dist > DUOMO_RADIUS_KM:
        return False
    nbhd = _normalise(listing.get("neighbourhood") or "")
    if not nbhd:
        return False
    return nbhd in PERIPHERAL_NEIGHBOURHOODS


def clear_geo(listing: dict) -> bool:
    """Strip geo fields. Returns True if anything actually changed."""
    changed = False
    for f in GEO_FIELDS_TO_CLEAR:
        if listing.get(f) is not None:
            listing[f] = None
            changed = True
    return changed


def process_file(path: Path, apply: bool) -> int:
    if not path.exists():
        print(f"  skip {path.name} — not on disk")
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        print(f"  skip {path.name} — could not parse: {exc}")
        return 0
    suspects = [l for l in data if is_misgeocoded(l)]
    print(f"  {path.name}: {len(suspects)} misgeocoded listing(s) of {len(data)}")
    if not suspects:
        return 0
    by_neigh: dict = {}
    for l in suspects:
        by_neigh.setdefault(l.get("neighbourhood") or "?", []).append(l.get("id"))
    for n, ids in sorted(by_neigh.items(), key=lambda kv: -len(kv[1])):
        sample = ", ".join(str(i) for i in ids[:3])
        more = f" (+{len(ids)-3} more)" if len(ids) > 3 else ""
        print(f"    {len(ids):4d}  {n:40s} e.g. {sample}{more}")
    if not apply:
        return len(suspects)
    cleared = 0
    cache_purge_ids: list[tuple[str, str]] = []
    for l in suspects:
        if clear_geo(l):
            cleared += 1
            cache_purge_ids.append((l.get("source", "immobiliare"), str(l.get("id"))))
    # Atomic write
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
    print(f"    → cleared geo on {cleared} listing(s) in {path.name}")

    # Purge from enrichment cache so the next enrich pass re-geocodes
    # instead of restoring the bad geo from cache.
    try:
        sys.path.insert(0, str(BASE_DIR))
        import enrichment_cache as _ec
        _ec.load()
        n_purged = _ec.delete(f"{src}:{lid}" for src, lid in cache_purge_ids)
        print(f"    → purged {n_purged} cache entry/ies")
    except Exception as exc:
        print(f"    [warn] could not purge enrichment_cache: {exc}")

    return cleared


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="actually clear (default = dry-run)")
    args = p.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"clear_wrong_coords.py [{mode}] — Duomo-radius {DUOMO_RADIUS_KM} km, "
          f"{len(PERIPHERAL_NEIGHBOURHOODS)} peripheral names")
    total = 0
    for path in (RENTAL_PATH, LEGACY_RENTAL, SALE_PATH):
        total += process_file(path, args.apply)
    print()
    print(f"  TOTAL flagged: {total}")
    if not args.apply:
        print("  Re-run with --apply to actually clear.")
        print("  After applying, trigger /geo-enrich-queue to re-geocode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
