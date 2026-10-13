"""
One-shot migration after the Scan Areas UI redesign.

Rewrites every area_settings_{city}.json (and the legacy un-prefixed
area_settings.json for Milan) into the new macro-slug shape, with ALL
macro-areas active by default. Power users can deselect peripheral
macros from the Settings card afterward.

Old shape →  {"areas": [{"id","name","active"}, …]}            (display names)
New shape →  {"active_areas": ["macro-slug-1", "macro-slug-2", …]}

Idempotent — running twice is harmless.

Run once after deploying the redesign:
    python3 migrate_area_settings.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# Local import to keep this script standalone-runnable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import area_slug_mapper as _slug

BASE_DIR = Path(__file__).resolve().parent

# Cities the scanner currently supports — keep in lock-step with
# api.py's whitelist in /api/save-areas.
CITIES = ("milano", "roma", "napoli", "la_maddalena")


def _settings_path(city: str) -> Path:
    """Mirror api.py _area_settings_path: Milan reads the un-prefixed legacy
    file so the existing scan_prefs.json side-channel keeps working."""
    if city == "milano":
        return BASE_DIR / "area_settings.json"
    return BASE_DIR / f"area_settings_{city}.json"


def migrate_city(city: str) -> int:
    """Reset one city's area settings to all macro-areas active. Returns
    the count written. 0 means the slug mapping is empty for this city
    (Roma/Napoli/La Maddalena currently)."""
    macros = _slug.macro_areas_for_city(city)
    if not macros:
        print(f"  [{city}] mapping empty — skipping (add entries to "
              f"area_slug_mapping.json first)")
        return 0
    settings = {
        "active_areas": macros,
        "last_saved":   datetime.now().isoformat(timespec="seconds"),
        "city":         city,
    }
    path = _settings_path(city)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    print(f"  [{city}] reset {path.name} to {len(macros)} macro-area(s) "
          f"(all active)")

    # Milan: sync scan_prefs.json so the scan daemon picks up the new
    # selection without reading area_settings directly. fetch_rentals
    # and fetch_listings both normalise macro slugs back through
    # group_by_immobiliare_slug, so dropping them into prefs["areas"]
    # is safe.
    if city == "milano":
        prefs_path = BASE_DIR / "scan_prefs.json"
        try:
            prefs = json.loads(prefs_path.read_text())
        except Exception:
            prefs = {}
        prefs["areas"] = macros
        prefs_path.write_text(json.dumps(prefs, indent=2))
        print(f"  [{city}]   scan_prefs.json synced ({len(macros)} areas)")
    return len(macros)


def main() -> int:
    print("Migrating area settings to the macro-slug format…")
    total = 0
    for c in CITIES:
        total += migrate_city(c)
    print(f"\nDone. {total} macro-area entry(ies) written across {len(CITIES)} cities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
