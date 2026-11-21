"""
data_dir.py
───────────
Where the scanners keep the big files they rewrite all day: data.noindex/.
Spotlight skips a folder whose name ends in ".noindex", so it no longer
re-indexes them after every write — on 2026-11-21 mds_stores was at ~15%
CPU, mostly on detail_cache_milano.json (70 MB, rewritten after every batch
of listing pages), enriched_cache.json (55 MB) and the listing files
milano_rentals_latest.json (120 MB) / milano_sales_latest.json (31 MB),
rewritten on every rescore.

path(name) returns data.noindex/<name> and, the first time, moves the file
there from the project root, where earlier versions kept it.
listings_path(city, kind) does the same for dashboard/<city>_<kind>_latest.json
and its .lock. The move is a rename on the same disk, so nothing is copied
and nothing can be half-moved; when two processes start together, one moves
it and the other finds it. Every process must use the new path before any
writes again (all of them restart together), or the old location would come
back.

The legacy dashboard/rentals_latest.json and sales_latest.json (stale May
data, never pushed — supabase_sync.LEGACY_FILES) stay where they are.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data.noindex"
DASHBOARD_DIR = BASE_DIR / "dashboard"

_KINDS = {"rental": "rentals", "rentals": "rentals", "sale": "sales", "sales": "sales"}


def _moved(name: str, old_dir: Path) -> Path:
    """data.noindex/<name>, moved from old_dir/<name> if still there."""
    new = DATA_DIR / name
    old = old_dir / name
    if old.exists() and not new.exists():
        DATA_DIR.mkdir(exist_ok=True)
        try:
            os.replace(old, new)
            print(f"  [data] moved {name} → data.noindex/", flush=True)
        except FileNotFoundError:
            pass            # another process moved it first
    DATA_DIR.mkdir(exist_ok=True)
    return new


def path(name: str) -> Path:
    """data.noindex/<name>, moved from the project root if still there."""
    return _moved(name, BASE_DIR)


def listings_path(city: str, kind: str) -> Path:
    """data.noindex/<city>_rentals_latest.json or <city>_sales_latest.json —
    the city's listing file (kind "rental"/"sale"). The first call moves it
    from dashboard/, with its .lock (the lock is always the file's path +
    ".lock", next to it, so the scanners and api.py keep locking one file)."""
    if kind not in _KINDS:
        raise ValueError(f"kind must be rental or sale, not {kind!r}")
    if not re.fullmatch(r"[a-z0-9_]+", city or ""):
        raise ValueError(f"bad city {city!r}")
    name = f"{city}_{_KINDS[kind]}_latest.json"
    _moved(name + ".lock", DASHBOARD_DIR)
    return _moved(name, DASHBOARD_DIR)
