#!/usr/bin/env python3
from __future__ import annotations  # X | Y syntax works on Python 3.9+
"""
enrichment_cache.py
───────────────────
Persistent cache for geo enrichment results. Listings are enriched once
via the Overpass API; results are stored in enriched_cache.json. Subsequent
fetches look up the cache instead of calling Overpass again.

Key format : "{source}:{listing_id}"    e.g. "immobiliare:12345678"
Cache file : BASE_DIR/enriched_cache.json
Thread-safe: module-level lock protects all reads/writes.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).parent
CACHE_PATH = BASE_DIR / "enriched_cache.json"

_cache: dict = {}
_lock        = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> dict:
    """
    Load cache from disk into memory and return it.
    Safe to call multiple times; re-reads disk on each call.
    """
    global _cache
    if not CACHE_PATH.exists():
        with _lock:
            _cache = {}
        return _cache
    try:
        data = json.loads(CACHE_PATH.read_text())
        with _lock:
            _cache = data
        return _cache
    except Exception:
        with _lock:
            _cache = {}
        return _cache


def get(source: str, listing_id: str) -> "dict | None":
    """Return cached enrichment for (source, listing_id), or None if missing."""
    key = f"{source}:{listing_id}"
    with _lock:
        return _cache.get(key)


def save(source: str, listing_id: str, data: dict) -> None:
    """Save one enrichment result and persist the full cache atomically."""
    key   = f"{source}:{listing_id}"
    entry = {"enriched_at": datetime.now().isoformat(timespec="seconds"), **data}
    with _lock:
        _cache[key] = entry
        _flush()


def bulk_save(entries) -> None:
    """
    Batch-write multiple enrichments at once, then flush once.
    entries: iterable of (source, listing_id, data) tuples.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    with _lock:
        for source, listing_id, data in entries:
            _cache[f"{source}:{listing_id}"] = {"enriched_at": ts, **data}
        _flush()


def stats() -> dict:
    """Return summary stats about the current in-memory cache."""
    with _lock:
        total  = len(_cache)
        by_src = {}
        dates  = []
        for key, val in _cache.items():
            src = key.split(":", 1)[0]
            by_src[src] = by_src.get(src, 0) + 1
            ea = val.get("enriched_at")
            if ea:
                dates.append(ea)
    return {
        "total":     total,
        "by_source": by_src,
        "oldest":    min(dates) if dates else None,
        "newest":    max(dates) if dates else None,
        "path":      str(CACHE_PATH),
    }


def clear() -> None:
    """Delete the cache file and reset the in-memory cache."""
    global _cache
    with _lock:
        _cache = {}
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()


# ── Internal ──────────────────────────────────────────────────────────────────

def _flush() -> None:
    """Write _cache to disk atomically (write .tmp → rename). Call under _lock."""
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=2))
    tmp.replace(CACHE_PATH)
