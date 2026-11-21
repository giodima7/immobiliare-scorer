"""
data_dir.py
───────────
Where the scanners keep the big files they rewrite all day: data.noindex/.
Spotlight skips a folder whose name ends in ".noindex", so it no longer
re-indexes them after every write — on 2026-11-21 mds_stores was at ~15%
CPU, mostly on detail_cache_milano.json (70 MB, rewritten after every batch
of listing pages) and enriched_cache.json (55 MB).

path(name) returns data.noindex/<name> and, the first time, moves the file
there from the project root, where earlier versions kept it. The move is a
rename on the same disk, so nothing is copied and nothing can be half-moved;
when two processes start together, one moves it and the other finds it.
Every process must use the new path before any writes again (all of them
restart together), or the old location would come back.

Not here yet: dashboard/<city>_{rentals,sales}_latest.json, read and
written from ~15 modules (a follow-up).
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data.noindex"


def path(name: str) -> Path:
    """data.noindex/<name>, moved from the project root if still there."""
    new = DATA_DIR / name
    old = BASE_DIR / name
    if old.exists() and not new.exists():
        DATA_DIR.mkdir(exist_ok=True)
        try:
            os.replace(old, new)
            print(f"  [data] moved {name} → data.noindex/", flush=True)
        except FileNotFoundError:
            pass            # another process moved it first
    DATA_DIR.mkdir(exist_ok=True)
    return new
