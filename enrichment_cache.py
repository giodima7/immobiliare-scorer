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
Thread-safe: module-level lock protects all reads/writes. Process-safe: the
API and both scanner daemons write the file; each write merges only the
keys this process added or changed into what's on disk, under a file lock.
"""

import fcntl
import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).parent
CACHE_PATH = BASE_DIR / "enriched_cache.json"
# Bookkeeping keys in an entry, not listing fields: when it was written and
# a failed placement (api.py GEO_RETRY_DAYS). Left out when an entry is
# merged into a listing.
META_KEYS = frozenset({"enriched_at", "geo_failed_at", "geo_failed_for"})

_cache: dict = {}
_dirty: set  = set()     # keys saved in this process since the last flush
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
        _dirty.add(key)
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
            _dirty.add(f"{source}:{listing_id}")
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


def delete(keys) -> int:
    """Remove "{source}:{listing_id}" keys from memory and from the file.
    Returns how many were present. (Popping _cache and flushing no longer
    removes anything: a flush only adds this process's own keys.)"""
    keys = set(keys)
    with _lock:
        n = sum(1 for k in keys if _cache.pop(k, None) is not None)
        _dirty.difference_update(keys)
        _flush(removed=keys)
    return n


def clear() -> None:
    """Delete the cache file and reset the in-memory cache. Other running
    processes still hold their copies; their own new keys come back on
    their next write, nothing else."""
    global _cache
    with _lock, _file_lock():
        _cache = {}
        _dirty.clear()
        if CACHE_PATH.exists():
            CACHE_PATH.unlink()


# ── Internal ──────────────────────────────────────────────────────────────────

@contextmanager
def _file_lock():
    """Exclusive lock shared by every process writing the cache."""
    with open(CACHE_PATH.with_suffix(".lock"), "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _flush(removed: set = frozenset()) -> None:
    """Merge this process's new / changed keys (and removals) into the file
    on disk and write it back atomically (.tmp → rename). Call under _lock.

    Each process used to write its whole in-memory copy, so the last writer
    dropped whatever the others had added since it loaded — ~7k of 65k
    entries on 2026-10-17, once the scanners started writing every 20 min.
    A key both sides changed keeps the newer enriched_at. Afterwards the
    in-memory copy is the merged file, so this process sees the others'
    entries too."""
    global _cache
    with _file_lock():
        try:
            disk = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        except (OSError, ValueError):
            disk = {}
        for k in removed:
            disk.pop(k, None)
        for k in _dirty:
            mine = _cache.get(k)
            theirs = disk.get(k)
            if mine is not None and (theirs is None or
                                     (mine.get("enriched_at") or "") >= (theirs.get("enriched_at") or "")):
                disk[k] = mine
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(disk, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(CACHE_PATH)
        _cache = disk
        _dirty.clear()
