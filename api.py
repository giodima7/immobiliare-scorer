#!/usr/bin/env python3
"""
api.py — Flask backend for the Immobiliare Scorer dashboard.
Replaces serve.py.

Usage:
    python3 api.py
    → http://localhost:8000/

Endpoints:
    GET  /           — dashboard HTML
    GET  /listings   — latest.json
    GET  /status     — {"running": bool}
    POST /fetch      — start a fetch; streams stdout as Server-Sent Events
"""
from __future__ import annotations   # makes X | None annotations lazy — works on Python 3.9

import json as _json
import subprocess
import sys
import threading
import time as _time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE_DIR      = Path(__file__).parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
SCRIPT        = BASE_DIR / "fetch_listings.py"

app = Flask(__name__)

# Return JSON for all HTTP errors so the browser never sees an HTML error page
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found", "path": request.path}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": str(e)}), 500

_fetch_lock    = threading.Lock()
_fetch_running = False

_scanner_lock = threading.Lock()
_scanner_proc = None   # subprocess.Popen for fetch_rentals.py --daemon
_idealista_proc = None  # subprocess.Popen for fetch_idealista.py --daemon

# ── Background geo enrichment ─────────────────────────────────────────────────
# Legacy single-status dicts: kept so the existing dashboard polling code that
# reads st.geo_enrich / st.geo_sale_enrich keeps working unchanged. Mirrors
# the milano slot of _geo_jobs.
_geo_status: dict = {"running": False, "done": 0, "total": 0, "error": None}
_geo_stop   = threading.Event()

# ── Background geo enrichment — sales ────────────────────────────────────────
_geo_sale_status: dict = {"running": False, "done": 0, "total": 0, "error": None}
_geo_sale_stop   = threading.Event()

# ── Multi-city enrichment queue ──────────────────────────────────────────────
# Per-(city, type) progress, keyed "{city}:{rental|sale}". The orchestrator
# below drains _geo_queue one entry at a time, mutating the matching slot.
# The dashboard polls scanner-status and renders each row.
_geo_jobs: dict[str, dict] = {}
_geo_queue: list[tuple[str, str]] = []
_geo_queue_lock = threading.Lock()
_geo_queue_stop = threading.Event()       # global cancel for the orchestrator
_geo_orchestrator_thread: threading.Thread | None = None

def _geo_job_key(city: str, kind: str) -> str:
    return f"{city}:{kind}"

def _geo_job_init(city: str, kind: str, state: str = "queued") -> dict:
    """Create/replace a progress slot for one (city, kind) job."""
    slot = {
        "city": city, "kind": kind, "state": state,
        "running": False, "done": 0, "total": 0, "error": None,
        "phase": None,       # human-readable: "polygon"/"geocoding"/"rescoring"/"pushing"
        "phase_label": None, # localised label for the dashboard
        "started_at": None, "finished_at": None,
    }
    _geo_jobs[_geo_job_key(city, kind)] = slot
    return slot


def _geo_set_phase(slot: dict, phase: str, label: str | None = None) -> None:
    """Mark which sub-step of enrichment the worker is in. Picked up by
    the dashboard so the user can see WHAT is happening, not just IF."""
    slot["phase"]       = phase
    slot["phase_label"] = label or {
        "polygon":   "Applying OMI zones",
        "geocoding": "Geocoding + enriching listings",
        "rescoring": "Rescoring city listings",
        "pushing":   "Syncing to Supabase",
    }.get(phase, phase)

def _geo_job(city: str, kind: str) -> dict:
    """Fetch an existing slot, creating an empty one if missing."""
    key = _geo_job_key(city, kind)
    if key not in _geo_jobs:
        _geo_job_init(city, kind, state="idle")
    return _geo_jobs[key]

_RE_POSIZIONE_APPROSSIMATIVA = None  # lazy-compiled

def _has_geocodable_data(l: dict) -> bool:
    """
    Single source of truth for "could enrich_geo.enrich() resolve coordinates
    for this listing?". Used by both per-city workers AND _count_pending_for
    so the preview number on the dashboard matches the actual job size.

    Four routes (checked in priority order):
      1. Stored coordinates                    → fastest path, always works
      2. Non-placeholder address string        → Photon / Nominatim geocoding
      3. Title embeds "in [Location]" or
         "a [UppercaseLocation]" (Idealista)   → enrich() extracts & geocodes
      4. Non-empty neighbourhood name          → neighbourhood-centroid fallback
    """
    global _RE_POSIZIONE_APPROSSIMATIVA
    import re as _re
    if _RE_POSIZIONE_APPROSSIMATIVA is None:
        _RE_POSIZIONE_APPROSSIMATIVA = _re.compile(r'posizione approssimativa', _re.I)

    if l.get("latitude") and l.get("longitude"):
        return True
    addr = (l.get("address") or "").strip()
    if addr and not _RE_POSIZIONE_APPROSSIMATIVA.search(addr):
        return True
    title = (l.get("title") or "").strip()
    if title and (_re.search(r'\s+in\s+', title, _re.I)
                  or _re.search(r'\s+a\s+(?=[A-ZÀ-ɏ])', title)):
        return True
    nbhd = (l.get("neighbourhood") or "").strip()
    if (nbhd
            and nbhd.lower() not in {"—", "-", ""}
            and not _RE_POSIZIONE_APPROSSIMATIVA.search(nbhd)):
        return True
    return False


def _resolve_city_path(city: str, kind: str) -> Path:
    """
    Resolve the JSON path for a (city, kind) pair, falling back to the
    legacy un-prefixed file for Milan when the new per-city file hasn't
    been written yet. Returns the per-city path either way — callers
    that need to create the file use this as the write target.
    """
    suffix = "rentals_latest.json" if kind == "rental" else "sales_latest.json"
    p = DASHBOARD_DIR / f"{city}_{suffix}"
    if p.exists() or city != "milano":
        return p
    legacy = DASHBOARD_DIR / suffix
    return legacy if legacy.exists() else p


# ── Pending-count preview ────────────────────────────────────────────────────
# Used by the Settings → Data Enrichment panel so the user knows how much
# work each (city, kind) job will do BEFORE clicking Run-all. Computing it
# means opening every file + walking the enrichment cache, which is too
# heavy to do per poll — cache the result with a short TTL and only
# recompute when the file mtime changes or the TTL expires.
_GEO_PENDING_CACHE: dict[str, dict] = {}   # key: "{city}:{kind}" → {count, total, mtime, computed_at}
_GEO_PENDING_TTL = 60.0  # seconds

def _count_pending_for(city: str, kind: str) -> dict:
    """
    Return {"count": N pending, "total": N listings, "stale": bool}.
    `count` mirrors what the worker would actually process; `stale`
    flags whether the file is missing entirely.
    """
    key  = _geo_job_key(city, kind)
    path = _resolve_city_path(city, kind)
    if not path.exists():
        return {"count": 0, "total": 0, "stale": True}

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"count": 0, "total": 0, "stale": True}

    cached = _GEO_PENDING_CACHE.get(key)
    now    = _time.time()
    if (cached
            and cached.get("mtime") == mtime
            and (now - cached.get("computed_at", 0)) < _GEO_PENDING_TTL):
        return {"count": cached["count"], "total": cached["total"], "stale": False}

    try:
        import enrichment_cache as _ecache
        _ecache.load()
        data = _json.loads(path.read_text())
    except Exception:
        return {"count": 0, "total": 0, "stale": True}

    # Cache key source. Sales always use "sale"; rentals derive per listing
    # (immobiliare vs idealista) so they hit the right cache entry.
    src_key = "sale" if kind == "sale" else None
    pending = 0
    for l in data:
        # Listings already carrying enrichment in the JSON itself don't need work.
        if l.get("geo_score") is not None:
            continue
        src = src_key or l.get("source") or "immobiliare"
        lid = l.get("id")
        if not lid:
            continue
        cached_entry = _ecache.get(src, lid)
        if cached_entry and cached_entry.get("geo_score") is not None:
            continue
        # Single source of truth shared with both workers — guarantees the
        # preview number matches the actual job size.
        if _has_geocodable_data(l):
            pending += 1

    _GEO_PENDING_CACHE[key] = {
        "count": pending, "total": len(data),
        "mtime": mtime, "computed_at": now,
    }
    return {"count": pending, "total": len(data), "stale": False}


def _all_pending() -> dict:
    """Per-(city, kind) pending counts for the four active cities."""
    out = {}
    for city in ("milano", "roma", "napoli", "la_maddalena"):
        for kind in ("rental", "sale"):
            out[_geo_job_key(city, kind)] = _count_pending_for(city, kind)
    return out


def _invalidate_pending_cache(city: str | None = None, kind: str | None = None) -> None:
    """Drop pending-count cache entries so the next poll recomputes."""
    if city is None and kind is None:
        _GEO_PENDING_CACHE.clear()
        return
    if city and kind:
        _GEO_PENDING_CACHE.pop(_geo_job_key(city, kind), None)
        return
    if city:
        for k in list(_GEO_PENDING_CACHE):
            if k.startswith(f"{city}:"):
                _GEO_PENDING_CACHE.pop(k, None)

# ── Sales fetch (fetch_listings.py) ──────────────────────────────────────────
_sale_fetch_lock   = threading.Lock()
_sale_proc: subprocess.Popen | None = None
_sale_fetch_status: dict = {"running": False, "last_run": None, "count": 0, "error": None}

# ── Idealista sale fetch (fetch_idealista.py --mode sale) ─────────────────────
_ideal_sale_proc: subprocess.Popen | None = None
_ideal_sale_lock   = threading.Lock()
_ideal_sale_status: dict = {"running": False, "last_run": None, "count": 0, "error": None}

# ── Data-quality cache ────────────────────────────────────────────────────────
_dq_cache: dict | None = None
_dq_cache_ts: float = 0.0
_DQ_SALE_CACHE: dict | None = None
_dq_sale_cache_ts: float = 0.0
_DQ_CACHE_TTL = 60.0  # seconds


def _apply_omi_polygon(data: list, ecache=None) -> int:
    """
    Apply omi_lookup.lookup() to every listing that has coordinates but is
    missing omi_loc_mid (polygon-matched OMI data).

    Runs in-memory (no network calls) — typically completes in < 1 s for 1 000+
    listings.  Updates the listing dicts in-place and, if an ecache module is
    supplied, also updates the cached enrichment entry so the data persists.

    Returns the number of listings updated.
    """
    try:
        import omi_lookup as _omi
        if not _omi.ZONES:
            return 0
    except Exception as exc:
        print(f"  [omi] omi_lookup unavailable: {exc}", file=sys.stderr)
        return 0

    updated = 0
    cache_updates: list = []
    for l in data:
        if l.get("omi_loc_mid") is not None:
            continue   # already polygon-matched
        lat = l.get("latitude") or l.get("lat")
        lng = l.get("longitude") or l.get("lng")
        if not lat or not lng:
            continue
        try:
            zone, src = _omi.lookup(float(lat), float(lng))
        except Exception:
            continue
        if not zone:
            continue

        omi_fields = {
            "omi_zona":      zone["zona"],
            "omi_fascia":    zone["fascia"],
            "omi_descr":     zone["descr"],
            "omi_loc_min":   zone["loc_min"],
            "omi_loc_max":   zone["loc_max"],
            "omi_loc_mid":   zone["loc_mid"],
            "omi_compr_min": zone["compr_min"],
            "omi_compr_max": zone["compr_max"],
            "omi_compr_mid": zone["compr_mid"],
            "omi_source":    src,
        }
        l.update(omi_fields)
        updated += 1
        cache_updates.append((l.get("source", "immobiliare"), l["id"], omi_fields))

    # Persist all cache updates in a single bulk write (avoids 1 000+ individual flushes)
    if ecache is not None and cache_updates:
        try:
            merged_updates = []
            for src_key, lid, fields in cache_updates:
                existing = ecache.get(src_key, lid) or {}
                merged_updates.append((src_key, lid, {**existing, **fields}))
            ecache.bulk_save(merged_updates)
        except Exception as exc:
            print(f"  [omi] cache bulk_save failed: {exc}", file=sys.stderr)

    if updated:
        print(f"  [omi] polygon fields applied to {updated} listings", file=sys.stderr)
    return updated


def _geo_enrich_worker(city: str = "milano"):
    """
    Background thread: enrich all rental listings for `city` that aren't
    yet in the enrichment cache.

    Strategy:
      • OMI polygon lookup + haversine POI distances: in-memory, parallel (fast)
      • OSRM walk times: one table API call per metro station per 500-listing chunk
      • Flush to disk after every chunk so the dashboard updates in real time

    Progress is written to BOTH the per-city slot (`_geo_jobs[city:rental]`)
    AND, for Milan, the legacy `_geo_status` dict so the existing
    stats-bar progress UI keeps working without code changes.
    """
    global _geo_status
    _geo_stop.clear()
    slot = _geo_job_init(city, "rental", state="running")
    slot.update({"running": True, "started_at": _time.time()})
    if city == "milano":
        _geo_status = {"running": True, "done": 0, "total": 0, "error": None}

    def _mark_progress(done: int, total: int) -> None:
        """Single point of truth so per-city slot + legacy dict stay in sync."""
        slot["done"]  = done
        slot["total"] = total
        if city == "milano":
            _geo_status["done"]  = done
            _geo_status["total"] = total
    try:
        import enrichment_cache as _ecache
        import scoring as _scoring

        rent_path = _resolve_city_path(city, "rental")
        if not rent_path.exists():
            # Scanner hasn't run on this machine yet — pull the current
            # rows from Supabase so the user can still re-enrich without
            # waiting for the next scan.
            _load_env()
            from supabase_sync import hydrate_local_json
            if not hydrate_local_json(rent_path, "rental"):
                err = (
                    f"{rent_path.name} not found — set SUPABASE_URL / "
                    f"SUPABASE_SERVICE_KEY in .env, or run a scan first"
                )
                slot["error"] = err
                if city == "milano":
                    _geo_status["error"] = err
                return

        data = _json.loads(rent_path.read_text())
        _ecache.load()   # warm up in-memory cache

        # ── Pass 1: fast in-memory OMI polygon application ───────────────────
        # Fixes all listings that have coordinates but were cached before
        # omi_lookup existed (no Overpass call needed).
        _geo_set_phase(slot, "polygon")
        n_omi = _apply_omi_polygon(data, _ecache)
        if n_omi > 0:
            _geo_set_phase(slot, "rescoring", "Rescoring after OMI pass")
            _flush_geo(data, rent_path, _scoring)

        # ── Pass 2: Overpass POI enrichment for uncached listings ─────────────
        # Skip only if the cache entry already contains Overpass data (geo_score).
        # A cache entry with only OMI polygon fields (no geo_score) still needs
        # Overpass enrichment — this happens when the scan's inline enrich_batch
        # timed out but the OMI polygon pass ran and partially populated the cache.
        def _needs_overpass(l):
            # Fast path: if the listing JSON already carries geo_score the listing
            # was enriched in a previous run.  The cache may have been cleared since
            # then (e.g. file deleted) but there's nothing to gain by re-enriching.
            if l.get("geo_score") is not None:
                return False

            cached = _ecache.get(l.get("source", "immobiliare"), l["id"])
            if cached is None:
                return True

            # Re-enrich only if geo_score is missing entirely.
            # NOTE: do NOT add a terminal-state check for omi_source="no_coordinates"
            # here.  Whether a listing CAN be enriched is determined by
            # _has_geocodable_data() below.  A past "no_coordinates" result may have
            # been caused by an older code version that lacked title-based address
            # extraction — _has_geocodable_data() will correctly re-admit those
            # listings for another attempt with the improved enrich_geo.enrich().
            # metro_walk_routed is no longer used as a trigger: the table API
            # (/table/v1/foot/) is unavailable on the public OSRM server, so
            # walk times are always haversine-derived in batch mode.
            return cached.get("geo_score") is None

        # _has_geocodable_data is the module-level helper — same predicate
        # the dashboard's preview count uses, so the worker's processed
        # total matches what the UI promised.
        need_idx = [
            i for i, l in enumerate(data)
            if _needs_overpass(l) and _has_geocodable_data(l)
        ]
        n_needs_geocoding = sum(
            1 for i in need_idx
            if not (data[i].get("latitude") and data[i].get("longitude"))
        )
        _mark_progress(n_omi, len(need_idx) + n_omi)   # polygon pass already counted
        if need_idx:
            _geo_set_phase(slot, "geocoding")
        print(f"  [geo][{city}] background enrichment: {len(need_idx)} listings to process "
              f"({n_needs_geocoding} need Nominatim geocoding)")

        from enrich_geo import enrich_batch as _enrich_batch

        WORKERS    = 8
        # Chunk size trades off progress-bar granularity vs. I/O overhead.
        # When geocoding is needed (Photon: ~3 req/sec) a 500-listing chunk
        # takes ~2-3 minutes before the counter updates — looks frozen.
        # Use small chunks (24) so the counter ticks every ~8 seconds.
        # When all listings already have coordinates, use large chunks (500)
        # since the work is pure in-memory and each chunk completes in < 1 s.
        CHUNK_SIZE      = 24 if n_needs_geocoding > 50 else 500
        MAX_NULL_STREAK = 10   # stop if a whole chunk returns no geo data
        done_count      = 0
        null_streak     = 0

        # Chunks: all OMI polygon lookups and haversine POI distances run
        # in parallel (pure in-memory).  No OSRM table calls are made —
        # the public server only exposes foot-profile routing,
        # not the table service, so walk times are haversine-derived.
        for chunk_start in range(0, len(need_idx), CHUNK_SIZE):
            if _geo_stop.is_set():
                print("  [geo] stopped by request")
                break
            if null_streak >= MAX_NULL_STREAK:
                remaining = len(need_idx) - chunk_start
                print(f"  [geo] {null_streak} consecutive nulls — skipping remaining {remaining}")
                break

            chunk_idxs     = need_idx[chunk_start:chunk_start + CHUNK_SIZE]
            chunk_listings = [data[i] for i in chunk_idxs]

            geo_results = _enrich_batch(chunk_listings, max_workers=WORKERS)

            cache_entries = []
            chunk_nulls   = 0
            for idx, geo in zip(chunk_idxs, geo_results):
                data[idx].update(geo)
                src = data[idx].get("source", "immobiliare")
                cache_entries.append((src, data[idx]["id"], geo))
                if geo.get("geo_score") is None:
                    chunk_nulls += 1

            _ecache.bulk_save(cache_entries)
            done_count  += len(chunk_idxs)
            null_streak  = null_streak + chunk_nulls if chunk_nulls == len(chunk_idxs) else 0
            _mark_progress(n_omi + done_count, slot["total"])

            # Flush after every chunk so the dashboard reflects progress in real time
            clean = [{k: v for k, v in l.items() if k != "omi"} for l in data]
            tmp = rent_path.with_suffix(".tmp")
            from dashboard_io import write_snapshot
            write_snapshot(tmp, clean)
            tmp.replace(rent_path)
            print(f"  [geo][{city}] {done_count}/{len(need_idx)} done")

        # Final flush — mark total complete so UI shows 100 %.
        _mark_progress(slot["total"], slot["total"])
        # Skip the rescore + supabase push when nothing actually changed
        # (n_omi was 0 AND no overpass batches ran). Otherwise we spend
        # 10–30 s rescoring a city whose data is byte-identical to disk.
        if n_omi == 0 and done_count == 0:
            print(f"  [geo][{city}] nothing to enrich — skipping rescore")
            return
        _geo_set_phase(slot, "rescoring")
        _flush_geo(data, rent_path, _scoring)
        # Push the enriched rows back to Supabase so the deployed
        # dashboard sees the new geo fields without waiting for the
        # next scan. Quiet failure if creds aren't set — the local
        # JSON is already updated.
        _geo_set_phase(slot, "pushing")
        try:
            from supabase_sync import push_local_json
            push_local_json(rent_path, "rental")
        except Exception as exc:
            print(f"  [geo][{city}] supabase push after enrich failed: {exc}")
        print(f"  [geo][{city}] enrichment complete: {n_omi + done_count} done")

    except Exception as exc:
        slot["error"] = str(exc)
        if city == "milano":
            _geo_status["error"] = str(exc)
        print(f"  [geo][{city}] worker crashed: {exc}")
    finally:
        slot["running"] = False
        # A user-triggered stop wins over "done" so the row reads "Stopped"
        # in the dashboard even though we still exit cleanly. The legacy
        # _geo_status uses error for the same signal (kept for back-compat).
        if slot.get("error"):
            slot["state"] = "error"
        elif _geo_stop.is_set() or _geo_queue_stop.is_set():
            slot["state"] = "stopped"
        else:
            slot["state"] = "done"
        slot["phase"]       = None
        slot["phase_label"] = None
        slot["finished_at"] = _time.time()
        if city == "milano":
            _geo_status["running"] = False
        # File on disk just changed (rescored + flushed) → pending count
        # is now stale. Drop the cache so the next poll recomputes from
        # the fresh file.
        _invalidate_pending_cache(city, "rental")


def _flush_geo(data: list, path: Path, scoring_mod):
    """Rescore and write JSON atomically."""
    global _dq_cache
    _dq_cache = None  # invalidate data-quality cache after new data is written
    scored = scoring_mod.score_all(list(data))
    # Refresh score_reasons too — without this the bullets stay anchored
    # to the previous scan's scores and can contradict the new ones (e.g.
    # "Quieter / less-connected pocket" on a listing whose location score
    # just got recomputed to 80+ after geo enrichment landed).
    from explain import explain_all
    explain_all(scored)
    clean  = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
    tmp = path.with_suffix(".tmp")
    from dashboard_io import write_snapshot
    write_snapshot(tmp, clean)
    tmp.replace(path)


def _flush_sale_geo(data: list, path: Path):
    """Rescore sales and write JSON atomically."""
    global _DQ_SALE_CACHE
    _DQ_SALE_CACHE = None  # invalidate sales DQ cache
    from scoring import score_all_sales
    from explain import explain_all_sales
    scored = score_all_sales(list(data))
    explain_all_sales(scored)
    clean  = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
    tmp = path.with_suffix(".tmp")
    from dashboard_io import write_snapshot
    write_snapshot(tmp, clean)
    tmp.replace(path)


def _geo_enrich_sales_worker(city: str = "milano"):
    """
    Background thread: enrich sale listings for `city` that aren't yet in
    the enrichment cache. Cache keys use the `sale` source prefix.

    Progress is written to BOTH `_geo_jobs[city:sale]` AND the legacy
    `_geo_sale_status` dict (Milan-only mirror) so the stats-bar progress
    UI keeps working without changes.
    """
    global _geo_sale_status
    _geo_sale_stop.clear()
    slot = _geo_job_init(city, "sale", state="running")
    slot.update({"running": True, "started_at": _time.time()})
    if city == "milano":
        _geo_sale_status = {"running": True, "done": 0, "total": 0, "error": None}

    def _mark_progress(done: int, total: int) -> None:
        slot["done"]  = done
        slot["total"] = total
        if city == "milano":
            _geo_sale_status["done"]  = done
            _geo_sale_status["total"] = total
    try:
        import enrichment_cache as _ecache

        sale_path = _resolve_city_path(city, "sale")
        if not sale_path.exists():
            _load_env()
            from supabase_sync import hydrate_local_json
            if not hydrate_local_json(sale_path, "sale"):
                err = (
                    f"{sale_path.name} not found — set SUPABASE_URL / "
                    f"SUPABASE_SERVICE_KEY in .env, or run a scan first"
                )
                slot["error"] = err
                if city == "milano":
                    _geo_sale_status["error"] = err
                return

        data = _json.loads(sale_path.read_text())
        _ecache.load()

        # Fast OMI polygon pass
        n_omi = _apply_omi_polygon(data, _ecache)

        # Backfill from cache: apply any cached geo fields that aren't yet in the
        # file (e.g. from a previous enrichment run whose flush was incomplete).
        GEO_FIELDS = ("geo_score", "metro_nearest_dist_m", "metro_nearest_name",
                      "pois", "walk_score", "park_nearest_dist_m",
                      "supermarket_nearest_dist_m")
        n_backfill = 0
        for l in data:
            if l.get("geo_score") is not None:
                continue
            cached = _ecache.get(l.get("source", "sale"), l["id"])
            if cached and cached.get("geo_score") is not None:
                for f in GEO_FIELDS:
                    if f in cached:
                        l[f] = cached[f]
                n_backfill += 1

        if n_omi > 0 or n_backfill > 0:
            print(f"  [geo-sale][{city}] backfill: {n_backfill} from cache, {n_omi} OMI polygon")
            _geo_set_phase(slot, "rescoring", "Rescoring after OMI pass")
            _flush_sale_geo(data, sale_path)

        def _needs_overpass(l):
            cached = _ecache.get(l.get("source", "sale"), l["id"])
            if cached is None:
                return True
            return cached.get("geo_score") is None

        # Match the dashboard preview exactly — same _has_geocodable_data the
        # rental worker (and _count_pending_for) use. The old loose check
        # ("coords OR any address string") swept up Idealista listings whose
        # only address was "Posizione approssimativa.", inflating the
        # processed count to 8× the preview.
        need_idx = [
            i for i, l in enumerate(data)
            if _needs_overpass(l) and _has_geocodable_data(l)
        ]
        _mark_progress(n_omi, len(need_idx) + n_omi)
        if need_idx:
            _geo_set_phase(slot, "geocoding")
        print(f"  [geo-sale][{city}] background enrichment: {len(need_idx)} listings to process")

        from enrich_geo import enrich_batch as _enrich_batch

        WORKERS    = 8
        n_needs_geocoding_sale = sum(
            1 for i in need_idx
            if not (data[i].get("latitude") and data[i].get("longitude"))
        )
        CHUNK_SIZE = 24 if n_needs_geocoding_sale > 50 else 500
        done_count = 0

        for chunk_start in range(0, len(need_idx), CHUNK_SIZE):
            if _geo_sale_stop.is_set():
                print("  [geo-sale] stopped by request")
                break

            chunk_idxs     = need_idx[chunk_start:chunk_start + CHUNK_SIZE]
            chunk_listings = [data[i] for i in chunk_idxs]

            geo_results = _enrich_batch(chunk_listings, max_workers=WORKERS)

            cache_entries = []
            for idx, geo in zip(chunk_idxs, geo_results):
                data[idx].update(geo)
                cache_entries.append(("sale", data[idx]["id"], geo))

            _ecache.bulk_save(cache_entries)
            done_count += len(chunk_idxs)
            _mark_progress(n_omi + done_count, slot["total"])

            clean = [{k: v for k, v in l.items() if k != "omi"} for l in data]
            tmp = sale_path.with_suffix(".tmp")
            from dashboard_io import write_snapshot
            write_snapshot(tmp, clean)
            tmp.replace(sale_path)
            print(f"  [geo-sale][{city}] {done_count}/{len(need_idx)} done")

        _mark_progress(slot["total"], slot["total"])
        if n_omi == 0 and n_backfill == 0 and done_count == 0:
            print(f"  [geo-sale][{city}] nothing to enrich — skipping rescore")
            return
        _geo_set_phase(slot, "rescoring")
        _flush_sale_geo(data, sale_path)
        _geo_set_phase(slot, "pushing")
        try:
            from supabase_sync import push_local_json
            push_local_json(sale_path, "sale")
        except Exception as exc:
            print(f"  [geo-sale][{city}] supabase push after enrich failed: {exc}")
        print(f"  [geo-sale][{city}] enrichment complete: {n_omi + done_count} done")

    except Exception as exc:
        slot["error"] = str(exc)
        if city == "milano":
            _geo_sale_status["error"] = str(exc)
        print(f"  [geo-sale][{city}] worker crashed: {exc}")
    finally:
        slot["running"] = False
        if slot.get("error"):
            slot["state"] = "error"
        elif _geo_sale_stop.is_set() or _geo_queue_stop.is_set():
            slot["state"] = "stopped"
        else:
            slot["state"] = "done"
        slot["phase"]       = None
        slot["phase_label"] = None
        slot["finished_at"] = _time.time()
        if city == "milano":
            _geo_sale_status["running"] = False
        _invalidate_pending_cache(city, "sale")

def _load_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            import os as _os
            _os.environ.setdefault(key.strip(), val.strip())

def _has_idealista_creds() -> bool:
    _load_env()
    import os as _os
    return bool(_os.environ.get("IDEALISTA_KEY") and _os.environ.get("IDEALISTA_SECRET"))


@app.route("/api-ping")
def api_ping():
    """Lightweight liveness probe — returns 200 only on the local Flask server.
    Netlify is configured to return 404 for this path.
    The dashboard JS uses this to switch between live-API and static-read-only modes."""
    return jsonify({"ok": True, "mode": "api"})


@app.route("/")
def index():
    return send_file(DASHBOARD_DIR / "index.html")


@app.route("/listings")
def listings():
    path = DASHBOARD_DIR / "latest.json"
    if not path.exists():
        return jsonify([])
    return send_file(path, mimetype="application/json")


@app.route("/status")
def status():
    return jsonify({"running": _fetch_running})


@app.route("/favourites")
def favourites():
    return send_file(DASHBOARD_DIR / "favourites.html")


@app.route("/rentals")
def rentals():
    path = DASHBOARD_DIR / "rentals_latest.json"
    if not path.exists():
        # Dashboard's loadRentals will fall through to Supabase on []
        # (see SupabaseClient.fetchAll). We could hydrate here too, but
        # an empty response is faster and the JS fallback is well-tested.
        return jsonify([])
    return send_file(path, mimetype="application/json")


@app.route("/sales")
def sales():
    path = DASHBOARD_DIR / "sales_latest.json"
    if not path.exists():
        return jsonify([])
    return send_file(path, mimetype="application/json")


SCAN_PREFS_PATH      = BASE_DIR / "scan_prefs.json"
SALE_PREFS_PATH      = BASE_DIR / "sale_fetch_prefs.json"

def _build_scanner_cmd(body: dict, city: str = "milano") -> list:
    """Build the fetch_rentals.py --daemon command from a prefs dict.
    Areas are NOT passed here — fetch_rentals.py reads active areas from
    area_settings_{city}.json (falling back to area_settings.json for
    Milan) via _load_active_areas()."""
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_rentals.py"),
           "--daemon", "--city", city]
    if body.get("max_rent"):
        cmd += ["--max-rent",  str(int(body["max_rent"]))]
    if body.get("min_rooms"):
        cmd += ["--min-rooms", str(int(body["min_rooms"]))]
    if body.get("pages"):
        cmd += ["--pages",     str(int(body["pages"]))]
    if body.get("email"):
        cmd.append("--email")
    return cmd


def _start_scanner_proc(prefs: dict, city: str = "milano") -> subprocess.Popen:
    cmd    = _build_scanner_cmd(prefs, city)
    log_fh = open(BASE_DIR / "scanner.log", "a")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_fh, stderr=log_fh)


def _start_idealista_proc(prefs: dict, city: str = "milano") -> subprocess.Popen:
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_idealista.py"),
           "--daemon", "--city", city]
    # Areas are driven by area_settings_{city}.json — not passed here.
    if prefs.get("max_rent"):
        cmd += ["--max-rent",  str(int(prefs["max_rent"]))]
    if prefs.get("min_rooms"):
        cmd += ["--min-rooms", str(int(prefs["min_rooms"]))]
    if prefs.get("pages"):
        cmd += ["--pages",     str(int(prefs["pages"]))]
    log_fh = open(BASE_DIR / "scanner_idealista.log", "a")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_fh, stderr=log_fh)


@app.route("/scan-prefs", methods=["GET"])
def get_scan_prefs():
    if SCAN_PREFS_PATH.exists():
        return send_file(SCAN_PREFS_PATH, mimetype="application/json")
    return jsonify({})


@app.route("/scan-prefs", methods=["POST"])
def save_scan_prefs():
    body = request.get_json(silent=True) or {}
    SCAN_PREFS_PATH.write_text(_json.dumps(body, indent=2))
    return jsonify({"saved": True})


@app.route("/sale-fetch-prefs", methods=["GET"])
def get_sale_fetch_prefs():
    if SALE_PREFS_PATH.exists():
        return send_file(SALE_PREFS_PATH, mimetype="application/json")
    return jsonify({})


@app.route("/sale-fetch-prefs", methods=["POST"])
def save_sale_fetch_prefs():
    body = request.get_json(silent=True) or {}
    SALE_PREFS_PATH.write_text(_json.dumps(body, indent=2))
    return jsonify({"saved": True})


@app.route("/start-scanner", methods=["POST"])
def start_scanner():
    """
    Start the rentals scanner. Accepts:
      pages, max_rent, min_rooms, email  → forwarded to fetch_rentals.py
      cities: [list]                     → multi-city; defaults to ["milano"]
                                           for back-compat with old POST bodies
    The first city in `cities` is the one the persistent --daemon process
    targets. Additional cities are spawned as separate processes that exit
    once the scan completes — they don't loop on a 60-minute cadence.
    """
    global _scanner_proc, _idealista_proc
    with _scanner_lock:
        if _scanner_proc and _scanner_proc.poll() is None:
            return jsonify({"error": "already running", "pid": _scanner_proc.pid}), 409
        body   = request.get_json(silent=True) or {}
        cities = body.get("cities") or ["milano"]
        if not isinstance(cities, list) or not cities:
            cities = ["milano"]
        primary = cities[0]
        # Persist prefs (without areas — areas are owned by area_settings.json)
        prefs_to_save = {k: v for k, v in body.items() if k != "areas"}
        SCAN_PREFS_PATH.write_text(_json.dumps(prefs_to_save, indent=2))
        _scanner_proc = _start_scanner_proc(body, primary)
        result = {"started": True, "pid": _scanner_proc.pid, "primary_city": primary}
        # Always start the Idealista daemon alongside Immobiliare.
        # (fetch_idealista.py uses browser automation — no API credentials needed)
        _idealista_proc = _start_idealista_proc(body, primary)
        result["idealista_pid"] = _idealista_proc.pid
        # Extra cities: one-shot subprocess each (no --daemon so they exit
        # once their pass is done). These show up in scanner.log alongside
        # the daemon. They run in parallel — fetch_rentals.py is mostly
        # network-bound on Immobiliare, so 4 concurrent processes still
        # fit comfortably under the site's rate limits.
        extras = []
        for c in cities[1:]:
            try:
                extras.append(_start_scanner_proc(body, c).pid)
            except Exception as exc:
                print(f"[start-scanner] {c} failed: {exc}")
        if extras:
            result["extra_pids"] = extras
        return jsonify(result)


@app.route("/stop-scanner", methods=["POST"])
def stop_scanner():
    global _scanner_proc, _idealista_proc
    with _scanner_lock:
        stopped = []
        if _scanner_proc and _scanner_proc.poll() is None:
            _scanner_proc.terminate()
            stopped.append(_scanner_proc.pid)
        if _idealista_proc and _idealista_proc.poll() is None:
            _idealista_proc.terminate()
            stopped.append(_idealista_proc.pid)
        if stopped:
            return jsonify({"stopped": True, "pids": stopped})
    return jsonify({"stopped": False, "reason": "not running"})


@app.route("/scanner-status")
def scanner_status():
    running_immo      = _scanner_proc   is not None and _scanner_proc.poll()   is None
    running_idealista = _idealista_proc is not None and _idealista_proc.poll() is None

    immo_st  = {"running": running_immo}
    ideal_st = {"running": running_idealista}

    for st, path in [(immo_st,  BASE_DIR / "scanner_status.json"),
                     (ideal_st, BASE_DIR / "scanner_status_idealista.json")]:
        if path.exists():
            try:
                st.update(_json.loads(path.read_text()))
            except Exception:
                pass

    immo_st["running"]  = running_immo
    ideal_st["running"] = running_idealista

    # Top-level fields from immobiliare (for backward compat with existing JS)
    result = {
        "running":     running_immo or running_idealista,
        "last_run":    immo_st.get("last_run"),
        "new_count":   immo_st.get("new_count", 0),
        "total_seen":  immo_st.get("total_seen", 0),
        "immobiliare": immo_st,
        "idealista":   ideal_st,
        "geo_enrich":       dict(_geo_status),       # live rentals enrichment progress
        "geo_sale_enrich":  dict(_geo_sale_status),  # live sales enrichment progress
        # Multi-city queue + per-(city, kind) state. The Settings panel
        # renders one row per slot; `queue` is the not-yet-started list.
        "geo_jobs": {k: dict(v) for k, v in _geo_jobs.items()},
        "geo_queue": [{"city": c, "kind": k} for c, k in list(_geo_queue)],
        "geo_queue_running": bool(_geo_orchestrator_thread
                                  and _geo_orchestrator_thread.is_alive()),
        # Per-(city, kind) preview: how many listings the worker WOULD
        # process if the user clicked Run-all right now. Cached for
        # _GEO_PENDING_TTL seconds and auto-invalidated whenever a worker
        # finishes (so post-enrichment the row drops to 0).
        "geo_pending": _all_pending(),
    }
    return jsonify(result)


# ── Sales fetch endpoints ─────────────────────────────────────────────────────

def _start_sale_fetch_proc(prefs: dict) -> subprocess.Popen:
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_listings.py")]
    cities = prefs.get("cities") or ["milano"]
    if len(cities) == 1:
        cmd += ["--city", cities[0]]
    else:
        cmd += ["--cities"] + cities
    if prefs.get("pages"):
        cmd += ["--pages",     str(int(prefs["pages"]))]
    if prefs.get("max_price"):
        cmd += ["--max-price", str(int(prefs["max_price"]))]
    if prefs.get("min_price"):
        cmd += ["--min-price", str(int(prefs["min_price"]))]
    if prefs.get("min_sqm"):
        cmd += ["--min-sqm",   str(int(prefs["min_sqm"]))]
    if prefs.get("max_sqm"):
        cmd += ["--max-sqm",   str(int(prefs["max_sqm"]))]
    if prefs.get("min_rooms"):
        cmd += ["--min-rooms", str(int(prefs["min_rooms"]))]
    if prefs.get("areas"):
        cmd += ["--areas", ",".join(prefs["areas"])]
    log_fh = open(BASE_DIR / "sale_fetch.log", "a")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_fh, stderr=log_fh)


def _sale_fetch_monitor(proc: subprocess.Popen) -> None:
    """Wait for fetch_listings.py to finish, then auto-rescore."""
    proc.wait()
    with _sale_fetch_lock:
        _sale_fetch_status["running"] = False
        if proc.returncode == 0:
            _sale_fetch_status["last_run"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
            _sale_fetch_status["error"] = None
            try:
                _rescore_existing_sales_json()
                sale_path = DASHBOARD_DIR / "sales_latest.json"
                if sale_path.exists():
                    data = _json.loads(sale_path.read_text())
                    _sale_fetch_status["count"] = len(data)
            except Exception as exc:
                _sale_fetch_status["error"] = str(exc)
        else:
            _sale_fetch_status["error"] = f"exited with code {proc.returncode} — check sale_fetch.log"


def _start_ideal_sale_proc(prefs: dict) -> subprocess.Popen:
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_idealista.py"), "--mode", "sale"]
    if prefs.get("pages"):      cmd += ["--pages",     str(int(prefs["pages"]))]
    if prefs.get("max_price"):  cmd += ["--max-rent",  str(int(prefs["max_price"]))]
    if prefs.get("min_sqm"):    cmd += ["--min-sqm",   str(int(prefs["min_sqm"]))]
    if prefs.get("min_rooms"):  cmd += ["--min-rooms", str(int(prefs["min_rooms"]))]
    log_fh = open(BASE_DIR / "idealista_sale_fetch.log", "a")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_fh, stderr=log_fh)


def _ideal_sale_monitor(proc: subprocess.Popen) -> None:
    proc.wait()
    with _ideal_sale_lock:
        _ideal_sale_status["running"] = False
        if proc.returncode == 0:
            _ideal_sale_status["last_run"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
            _ideal_sale_status["error"] = None
            try:
                _rescore_existing_sales_json()
                sale_path = DASHBOARD_DIR / "sales_latest.json"
                if sale_path.exists():
                    data = _json.loads(sale_path.read_text())
                    _ideal_sale_status["count"] = len(
                        [l for l in data if l.get("source") == "idealista_sale"]
                    )
            except Exception as exc:
                _ideal_sale_status["error"] = str(exc)
        else:
            _ideal_sale_status["error"] = (
                f"exited with code {proc.returncode} — check idealista_sale_fetch.log"
            )


@app.route("/start-ideal-sale-fetch", methods=["POST"])
def start_ideal_sale_fetch():
    global _ideal_sale_proc
    with _ideal_sale_lock:
        if _ideal_sale_proc and _ideal_sale_proc.poll() is None:
            return jsonify({"ok": False, "error": "already running"}), 409
        body = request.get_json(silent=True) or {}
        prefs = _json.loads(SALE_PREFS_PATH.read_text()) if SALE_PREFS_PATH.exists() else {}
        prefs.update({k: v for k, v in body.items() if v})
        _ideal_sale_proc = _start_ideal_sale_proc(prefs)
        _ideal_sale_status.update({"running": True, "error": None})
        threading.Thread(target=_ideal_sale_monitor, args=(_ideal_sale_proc,),
                         daemon=True).start()
    return jsonify({"ok": True})


@app.route("/stop-ideal-sale-fetch", methods=["POST"])
def stop_ideal_sale_fetch():
    with _ideal_sale_lock:
        if _ideal_sale_proc and _ideal_sale_proc.poll() is None:
            _ideal_sale_proc.terminate()
            _ideal_sale_status["running"] = False
    return jsonify({"ok": True})


@app.route("/ideal-sale-fetch-status")
def get_ideal_sale_fetch_status():
    running = bool(_ideal_sale_proc and _ideal_sale_proc.poll() is None)
    return jsonify({**_ideal_sale_status, "running": running})


@app.route("/start-sale-fetch", methods=["POST"])
def start_sale_fetch():
    global _sale_proc
    with _sale_fetch_lock:
        if _sale_proc and _sale_proc.poll() is None:
            return jsonify({"error": "already running", "pid": _sale_proc.pid}), 409
        body = request.get_json(silent=True) or {}
        cities_raw = body.get("cities", "milano")
        if isinstance(cities_raw, str):
            cities = [c.strip() for c in cities_raw.replace(",", " ").split() if c.strip()]
        else:
            cities = list(cities_raw)
        prefs: dict = {"cities": cities or ["milano"], "pages": body.get("pages", 3)}
        for key in ("max_price", "min_price", "min_sqm", "max_sqm", "min_rooms"):
            if body.get(key):
                prefs[key] = body[key]
        if body.get("areas"):
            areas_raw = body["areas"]
            prefs["areas"] = list(areas_raw) if not isinstance(areas_raw, str) else \
                             [a.strip() for a in areas_raw.split(",") if a.strip()]
        _sale_proc = _start_sale_fetch_proc(prefs)
        _sale_fetch_status.update({"running": True, "error": None})
        threading.Thread(target=_sale_fetch_monitor, args=(_sale_proc,), daemon=True).start()
        return jsonify({"started": True, "pid": _sale_proc.pid})


@app.route("/stop-sale-fetch", methods=["POST"])
def stop_sale_fetch():
    global _sale_proc
    with _sale_fetch_lock:
        if _sale_proc and _sale_proc.poll() is None:
            _sale_proc.terminate()
            _sale_fetch_status["running"] = False
            return jsonify({"stopped": True, "pid": _sale_proc.pid})
    return jsonify({"stopped": False, "reason": "not running"})


@app.route("/sale-fetch-status")
def get_sale_fetch_status():
    running = _sale_proc is not None and _sale_proc.poll() is None
    return jsonify({**_sale_fetch_status, "running": running})


@app.route("/api/download-pois", methods=["POST"])
def download_pois():
    """Download all Milan POIs in one bulk Overpass query → milan_pois.json."""
    try:
        from enrich_geo import download_milan_pois
        pois = download_milan_pois()
        counts = {k: len(v) for k, v in pois.items() if isinstance(v, list)}
        return jsonify({"ok": True, **counts})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/pois-status", methods=["GET"])
def pois_status():
    """Return whether milan_pois.json exists and its element counts."""
    from pathlib import Path as _Path
    path = BASE_DIR / "milan_pois.json"
    if not path.exists():
        return jsonify({"exists": False})
    try:
        data = _json.loads(path.read_text())
        counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
        return jsonify({"exists": True, "downloaded_at": data.get("downloaded_at"), **counts})
    except Exception as exc:
        return jsonify({"exists": False, "error": str(exc)})


@app.route("/geo-enrich", methods=["POST"])
def start_geo_enrich():
    """
    Start background geo enrichment of existing listings.

    Body / query is optional. Recognised:
      ?city=roma     → enrich a single city's rentals (default: milano).
      Otherwise behaves identically to the legacy Milan-only endpoint.
    """
    city = (request.args.get("city") or
            (request.get_json(silent=True) or {}).get("city") or
            "milano").lower()
    if _geo_job(city, "rental").get("running"):
        return jsonify({"ok": False, "error": f"{city} rental enrichment already running"}), 409
    t = threading.Thread(target=_geo_enrich_worker, args=(city,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"{city} rental enrichment started"})


@app.route("/geo-enrich", methods=["DELETE"])
def stop_geo_enrich():
    """
    Stop everything: the active worker, any pending sales worker, and the
    multi-city queue. Slots that are currently `queued` flip to `stopped`
    here so the dashboard reflects the change on the next poll without
    requiring a manual refresh — previously they sat at "Queued" forever.
    """
    _geo_stop.set()
    _geo_sale_stop.set()
    _geo_queue_stop.set()
    with _geo_queue_lock:
        cancelled = list(_geo_queue)
        _geo_queue.clear()
    # Flip queued + still-running rows immediately so the next /scanner-status
    # poll already shows the right thing. The worker's own `finally` block
    # will also overwrite its slot, but in the meantime the user sees a
    # responsive UI rather than "Queued" frozen for 10s+.
    now = _time.time()
    for c, k in cancelled:
        s = _geo_job(c, k)
        if s.get("state") == "queued":
            s.update({"state": "stopped", "running": False,
                      "phase": None, "phase_label": None,
                      "finished_at": now})
    for s in _geo_jobs.values():
        if s.get("running"):
            # Workers also mark themselves as stopped in their finally
            # block; pre-marking the phase here gives the user instant
            # "Stopping…" feedback while the chunk in flight finishes.
            s["phase"]       = "stopping"
            s["phase_label"] = "Stopping…"
    return jsonify({"ok": True})


# ── Multi-city orchestrator ──────────────────────────────────────────────────
def _geo_orchestrator_worker(jobs: list[tuple[str, str]]):
    """
    Sequentially drain `jobs` (list of (city, kind) tuples), running one
    worker at a time. Each job's progress is observable via
    /scanner-status. A DELETE /geo-enrich aborts both the current worker
    and the queue.

    Optimisation: jobs whose preview pending count is 0 are short-circuited
    to "done" immediately. The per-city worker would otherwise still run
    its end-of-job rescore + supabase push (10–30 s for a city the size of
    Milan), which is pure waste when nothing actually got enriched.
    """
    _geo_queue_stop.clear()
    with _geo_queue_lock:
        _geo_queue[:] = list(jobs)
        for city, kind in jobs:
            # Eagerly create a "queued" slot so the dashboard renders the
            # full queue immediately, before any worker has actually run.
            _geo_job_init(city, kind, state="queued")

    try:
        while True:
            with _geo_queue_lock:
                if _geo_queue_stop.is_set():
                    # User pressed Stop while items were still queued.
                    # Flip every queued slot to "stopped" so the dashboard
                    # immediately stops showing them as "Queued" (the
                    # previous behaviour required a manual refresh).
                    for c, k in _geo_queue:
                        s = _geo_job(c, k)
                        if s.get("state") == "queued":
                            s.update({"state": "stopped", "running": False,
                                      "phase": None, "phase_label": None,
                                      "finished_at": _time.time()})
                    _geo_queue.clear()
                    break
                if not _geo_queue:
                    break
                city, kind = _geo_queue.pop(0)

            # Skip jobs with zero pending listings — fast-path the slot
            # straight to "done" so the user sees a ✓ immediately and the
            # next job starts right away. We re-check the preview here
            # rather than trusting the cached count, since enrichment of a
            # previous job may have invalidated unrelated cache slots.
            preview = _count_pending_for(city, kind)
            if preview["count"] == 0 and not preview["stale"]:
                slot = _geo_job(city, kind)
                slot.update({
                    "state":       "done",
                    "running":     False,
                    "done":        0,
                    "total":       0,
                    "error":       None,
                    "started_at":  _time.time(),
                    "finished_at": _time.time(),
                })
                print(f"  [geo-orch] {city}:{kind} skipped (0 pending)")
                continue

            # The per-city worker also calls _geo_stop.clear() — but since
            # the orchestrator is the only producer here we don't need
            # any extra coordination.
            target = _geo_enrich_worker if kind == "rental" else _geo_enrich_sales_worker
            try:
                target(city)
            except Exception as exc:
                print(f"  [geo-orch] {city}:{kind} crashed: {exc}")
    finally:
        with _geo_queue_lock:
            _geo_queue.clear()


@app.route("/geo-enrich-queue", methods=["POST"])
def start_geo_enrich_queue():
    """
    Start a sequential queue covering one or more (city, kind) jobs.

    Body (optional, all fields default sensibly):
      {
        "cities": ["milano", "roma", "napoli", "la_maddalena"],
        "kinds":  ["rental", "sale"]
      }

    Default = every active city × both kinds. Refuses to start a new
    queue while another is in progress; DELETE /geo-enrich cancels.
    """
    global _geo_orchestrator_thread
    if _geo_orchestrator_thread and _geo_orchestrator_thread.is_alive():
        return jsonify({"ok": False, "error": "queue already running"}), 409

    body  = request.get_json(silent=True) or {}
    cities = body.get("cities") or ["milano", "roma", "napoli", "la_maddalena"]
    kinds  = body.get("kinds")  or ["rental", "sale"]
    jobs   = [(c, k) for c in cities for k in kinds]

    _geo_orchestrator_thread = threading.Thread(
        target=_geo_orchestrator_worker, args=(jobs,), daemon=True)
    _geo_orchestrator_thread.start()
    return jsonify({"ok": True, "queued": [{"city": c, "kind": k} for c, k in jobs]})


@app.route("/cache-stats")
def cache_stats():
    """Return enrichment cache statistics."""
    try:
        import enrichment_cache as _ecache
        _ecache.load()
        return jsonify(_ecache.stats())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/apply-omi", methods=["POST"])
def apply_omi_now():
    """
    Apply OMI polygon fields (omi_loc_mid etc.) to all listings that have
    coordinates but are missing polygon data, then rescore and save.
    Fast — no network calls, pure in-memory polygon lookup.
    """
    if _geo_status.get("running"):
        return jsonify({"ok": False, "error": "Geo enrichment is running — wait for it to finish"}), 409
    try:
        import enrichment_cache as _ecache
        import scoring as _scoring
        rent_path = DASHBOARD_DIR / "rentals_latest.json"
        if not rent_path.exists():
            _load_env()
            from supabase_sync import hydrate_local_json
            if not hydrate_local_json(rent_path, "rental"):
                return jsonify({"ok": False, "error": "rentals_latest.json not found and Supabase hydrate failed"}), 404
        data = _json.loads(rent_path.read_text())
        _ecache.load()
        updated = _apply_omi_polygon(data, _ecache)
        scored  = _scoring.score_all(data)
        from explain import explain_all
        explain_all(scored)
        clean   = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
        from dashboard_io import write_snapshot
        write_snapshot(rent_path, clean)
        try:
            from supabase_sync import push_local_json
            push_local_json(rent_path, "rental")
        except Exception as exc:
            print(f"[apply-omi] supabase push failed: {exc}")
        return jsonify({"ok": True, "updated": updated, "total": len(clean)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Run POST /cache-clear once after deploying omi_lookup to force re-enrichment
# with polygon-matched zones.  Old cache entries have stale keyword-based OMI fields.
@app.route("/cache-clear", methods=["POST"])
def cache_clear():
    """Delete enriched_cache.json and reset the in-memory cache."""
    if _geo_status.get("running"):
        return jsonify({"ok": False, "error": "Geo enrichment is running — stop it first"}), 409
    try:
        import enrichment_cache as _ecache
        _ecache.clear()
        return jsonify({"ok": True, "message": "Cache cleared"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/email-config", methods=["GET"])
def get_email_config():
    from email_digest import load_config
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "smtp_pass"}
    safe["smtp_pass"] = "••••••••" if cfg.get("smtp_pass") else ""
    return jsonify(safe)


@app.route("/email-config", methods=["POST"])
def set_email_config():
    from email_digest import load_config, save_config
    body = request.get_json(silent=True) or {}
    cfg  = load_config()
    for key in ("enabled", "smtp_host", "smtp_port", "smtp_user", "to_addrs",
                "digest_hour"):
        if key in body:
            cfg[key] = body[key]
    # Only overwrite password if a real value was sent (not the masked placeholder)
    if body.get("smtp_pass") and body["smtp_pass"] != "••••••••":
        cfg["smtp_pass"] = body["smtp_pass"]
    if "filters" in body:
        cfg["filters"] = {**cfg.get("filters", {}), **body["filters"]}
    save_config(cfg)
    return jsonify({"saved": True})


@app.route("/send-digest-now", methods=["POST"])
def send_digest_now():
    """Immediately send the digest using the current rentals file + email config."""
    from email_digest import load_config, send_digest
    cfg       = load_config()
    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(rent_path, "rental"):
            return jsonify({"error": "No rentals data yet"}), 404
    try:
        listings = _json.loads(rent_path.read_text())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Override enabled flag so test send always fires
    test_cfg           = dict(cfg)
    test_cfg["enabled"] = True
    try:
        send_digest(listings, test_cfg)
        return jsonify({"sent": True, "count": len(listings)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/zone-mappings")
def zone_mappings_page():
    return send_file(DASHBOARD_DIR / "mappings.html")


# ── Custom OMI zone-mapping endpoints ─────────────────────────────────────────

CUSTOM_MAPPINGS_PATH = BASE_DIR / "custom_omi_mappings.json"
AREA_SETTINGS_PATH   = BASE_DIR / "area_settings.json"

# All 43 official OMI zones for Milan (2° sem 2025) with rmin/rmax for the
# dropdown — keyed by zone code, grouped by OMI fascia (B/C/D/E).
_OMI_OFFICIAL_ZONES = {
    "B12": {"fascia_label": "Centrale",    "desc": "Duomo, Sanbabila, Montenapoleone, Missori",              "fascia": "A", "rmin": 25.0, "rmax": 35.0},
    "B13": {"fascia_label": "Centrale",    "desc": "Università Statale, San Lorenzo",                        "fascia": "A", "rmin": 19.0, "rmax": 26.0},
    "B15": {"fascia_label": "Centrale",    "desc": "Brera",                                                  "fascia": "A", "rmin": 26.0, "rmax": 35.0},
    "B16": {"fascia_label": "Centrale",    "desc": "Sant'Ambrogio, Cadorna, Via Dante",                      "fascia": "A", "rmin": 22.0, "rmax": 28.5},
    "B17": {"fascia_label": "Centrale",    "desc": "Parco Sempione, Arco della Pace, Corso Magenta",         "fascia": "A", "rmin": 17.5, "rmax": 26.5},
    "B18": {"fascia_label": "Centrale",    "desc": "Turati, Moscova, Corso Venezia",                         "fascia": "A", "rmin": 21.5, "rmax": 29.0},
    "B19": {"fascia_label": "Centrale",    "desc": "Venezia, Porta Vittoria, Porta Romana",                  "fascia": "A", "rmin": 19.0, "rmax": 27.0},
    "B20": {"fascia_label": "Centrale",    "desc": "Porta Vigentina, Porta Romana (outer)",                  "fascia": "A", "rmin": 17.0, "rmax": 22.0},
    "B21": {"fascia_label": "Centrale",    "desc": "Porta Ticinese, Porta Genova, Via San Vittore",          "fascia": "A", "rmin": 15.0, "rmax": 22.0},
    "C12": {"fascia_label": "Semicentrale","desc": "Pisani, Buenos Aires, Regina Giovanna",                  "fascia": "A", "rmin": 16.0, "rmax": 23.5},
    "C13": {"fascia_label": "Semicentrale","desc": "City Life",                                              "fascia": "A", "rmin": 28.0, "rmax": 40.0},
    "C14": {"fascia_label": "Semicentrale","desc": "Porta Nuova",                                            "fascia": "A", "rmin": 16.0, "rmax": 24.0},
    "C15": {"fascia_label": "Semicentrale","desc": "Stazione Centrale, Viale Stelvio",                       "fascia": "A", "rmin": 13.5, "rmax": 19.0},
    "C16": {"fascia_label": "Semicentrale","desc": "Cenisio, Farini, Sarpi",                                 "fascia": "A", "rmin": 13.5, "rmax": 18.0},
    "C17": {"fascia_label": "Semicentrale","desc": "Sempione, Pagano, Washington",                           "fascia": "A", "rmin": 16.0, "rmax": 18.5},
    "C18": {"fascia_label": "Semicentrale","desc": "Solari, Porta Genova, Ascanio Sforza",                   "fascia": "A", "rmin": 15.0, "rmax": 20.0},
    "C19": {"fascia_label": "Semicentrale","desc": "Tabacchi, Sarfatti, Crema (near Città Studi/Bocconi)",   "fascia": "B", "rmin": 13.5, "rmax": 21.0},
    "C20": {"fascia_label": "Semicentrale","desc": "Libia, XXII Marzo, Indipendenza (Porta Vittoria outer)", "fascia": "B", "rmin": 11.5, "rmax": 18.5},
    "D10": {"fascia_label": "Periferica",  "desc": "Parco Lambro, Feltre, Udine",                            "fascia": "B", "rmin": 11.0, "rmax": 15.5},
    "D12": {"fascia_label": "Periferica",  "desc": "Piola, Argonne, Corsica",                                "fascia": "B", "rmin": 12.0, "rmax": 15.5},
    "D13": {"fascia_label": "Periferica",  "desc": "Lambrate, Rubattino, Rombon",                            "fascia": "B", "rmin": 12.0, "rmax": 17.0},
    "D16": {"fascia_label": "Periferica",  "desc": "Tito Livio, Tertulliano, Longanesi, Ortomercato",        "fascia": "C", "rmin": 11.0, "rmax": 15.0},
    "D18": {"fascia_label": "Periferica",  "desc": "Marocchetti, Vigentino, Chiesa Rossa",                   "fascia": "C", "rmin": 11.0, "rmax": 15.0},
    "D20": {"fascia_label": "Periferica",  "desc": "Ortles, Spadolini, Bazzi",                               "fascia": "B", "rmin": 10.5, "rmax": 15.0},
    "D21": {"fascia_label": "Periferica",  "desc": "Barona, Famagosta, Faenza",                              "fascia": "C", "rmin": 10.5, "rmax": 15.0},
    "D24": {"fascia_label": "Periferica",  "desc": "Segesta, Aretusa, Vespri Siciliani (San Siro area)",     "fascia": "B", "rmin": 12.5, "rmax": 16.0},
    "D25": {"fascia_label": "Periferica",  "desc": "Lorenteggio, Inganni, Bisceglie, San Carlo B.",          "fascia": "C", "rmin": 10.5, "rmax": 14.0},
    "D28": {"fascia_label": "Periferica",  "desc": "Ippodromo, Caprilli, Monte Stella",                      "fascia": "B", "rmin": 11.5, "rmax": 15.5},
    "D31": {"fascia_label": "Periferica",  "desc": "Bovisa, Bausan, Imbonati",                               "fascia": "B", "rmin": 10.5, "rmax": 14.5},
    "D32": {"fascia_label": "Periferica",  "desc": "Bovisasca, Affori, P. Rossi, Comasina",                  "fascia": "B", "rmin":  9.5, "rmax": 12.0},
    "D33": {"fascia_label": "Periferica",  "desc": "Niguarda, Bignami, Parco Nord",                          "fascia": "B", "rmin": 11.0, "rmax": 14.5},
    "D34": {"fascia_label": "Periferica",  "desc": "Sarca, Bicocca",                                         "fascia": "B", "rmin": 11.0, "rmax": 15.0},
    "D35": {"fascia_label": "Periferica",  "desc": "Monza, Crescenzago, Gorla, Quartiere Adriano",           "fascia": "B", "rmin":  9.0, "rmax": 14.0},
    "D36": {"fascia_label": "Periferica",  "desc": "Maggiolina, Parco Trotter, Leoncavallo",                 "fascia": "B", "rmin":  9.5, "rmax": 14.0},
    "D37": {"fascia_label": "Periferica",  "desc": "Forlanini, Mecenate, Ponte Lambro",                      "fascia": "C", "rmin": 12.0, "rmax": 15.0},
    "D38": {"fascia_label": "Periferica",  "desc": "Santa Giulia, Rogoredo",                                 "fascia": "C", "rmin": 11.0, "rmax": 14.0},
    "D39": {"fascia_label": "Periferica",  "desc": "Cascina Merlata, Expo",                                  "fascia": "C", "rmin": 14.0, "rmax": 21.0},
    "D40": {"fascia_label": "Periferica",  "desc": "Musocco, Certosa",                                       "fascia": "C", "rmin": 10.0, "rmax": 14.5},
    "E5":  {"fascia_label": "Suburbana",   "desc": "Baggio, Q. Romano, Muggiano",                            "fascia": "C", "rmin":  9.0, "rmax": 11.8},
    "E6":  {"fascia_label": "Suburbana",   "desc": "Gallaratese, Lampugnano, P. Trenno, Bonola",             "fascia": "C", "rmin": 10.0, "rmax": 14.0},
    "E7":  {"fascia_label": "Suburbana",   "desc": "Missaglia, Gratosoglio",                                 "fascia": "C", "rmin":  6.5, "rmax": 10.0},
    "E8":  {"fascia_label": "Suburbana",   "desc": "Quarto Oggiaro, Sacco",                                  "fascia": "C", "rmin":  7.5, "rmax": 10.0},
    "R2":  {"fascia_label": "Rurale",      "desc": "Ronchetto, Chiaravalle, Ripamonti",                      "fascia": "C", "rmin":  9.0, "rmax": 13.0},
}


def _rescore_with_new_mappings():
    """Reload fetch_rentals match_omi cache + rescore all listings."""
    import importlib
    import fetch_rentals as _fr
    # Invalidate the in-memory cache so next call re-reads the file
    _fr._custom_map_cache = None
    _fr._custom_map_mtime = 0.0

    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(rent_path, "rental"):
            return 0
    data = _json.loads(rent_path.read_text())
    if not data:
        return 0

    # Re-match OMI for every listing and rebuild the omi dict
    for l in data:
        nb  = l.get("neighbourhood") or ""
        omi = _fr.match_omi(nb)
        l["omi"] = {
            "zone":   omi["zone"],
            "fascia": omi["fascia"],
            "rmin":   omi["rmin"],
            "rmax":   omi["rmax"],
        }

    import scoring as _scoring
    scored = _scoring.score_all(data)
    from explain import explain_all
    explain_all(scored)
    clean  = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
    from dashboard_io import write_snapshot
    write_snapshot(rent_path, clean)
    try:
        from supabase_sync import push_local_json
        push_local_json(rent_path, "rental")
    except Exception as exc:
        print(f"[rescore-mappings] supabase push failed: {exc}")
    return len(clean)


def _load_area_settings() -> dict:
    if AREA_SETTINGS_PATH.exists():
        try:
            data = _json.loads(AREA_SETTINGS_PATH.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    legacy = {}
    if CUSTOM_MAPPINGS_PATH.exists():
        try:
            legacy_raw = _json.loads(CUSTOM_MAPPINGS_PATH.read_text())
            for nb, entry in legacy_raw.items():
                if not isinstance(entry, dict):
                    continue
                zone_code = entry.get("zone_code")
                legacy[nb] = {
                    "omi_zones": [zone_code] if zone_code else [],
                    "zone_code": zone_code,
                    "fascia": entry.get("fascia"),
                    "rmin": entry.get("rmin"),
                    "rmax": entry.get("rmax"),
                }
        except Exception:
            pass

    return {"city": "milano", "mappings": legacy}


def _save_area_settings(settings: dict) -> None:
    AREA_SETTINGS_PATH.write_text(_json.dumps(settings, ensure_ascii=False, indent=2))


def _to_url_slug(name: str) -> str:
    """Delegate to fetch_rentals.to_url_slug (imported lazily to keep startup lean)."""
    from fetch_rentals import to_url_slug as _fr_slug
    return _fr_slug(name)


def _load_area_settings_v2() -> dict:
    """Load the new-format area settings (list of areas with active flag).
    Falls back to migrating active areas from scan_prefs.json if the file
    is missing or still in the old OMI-mapping format."""
    if AREA_SETTINGS_PATH.exists():
        try:
            data = _json.loads(AREA_SETTINGS_PATH.read_text())
            if isinstance(data, dict) and "areas" in data:
                return data
        except Exception:
            pass
    # Migrate: seed from scan_prefs.json areas
    areas = []
    if SCAN_PREFS_PATH.exists():
        try:
            prefs = _json.loads(SCAN_PREFS_PATH.read_text())
            for name in (prefs.get("areas") or []):
                name = name.strip()
                if name:
                    areas.append({"id": _to_url_slug(name), "name": name, "active": True})
        except Exception:
            pass
    return {"areas": areas, "last_saved": None}


@app.route("/area-settings", methods=["GET"])
def get_area_settings_v2():
    """Return the new-format area settings (areas list with active flags)."""
    return jsonify(_load_area_settings_v2())


@app.route("/area-settings", methods=["POST"])
def save_area_settings_v2():
    """Save the new-format area settings and sync active names to scan_prefs.json."""
    from datetime import datetime as _dt
    body = request.get_json(force=True, silent=True) or {}
    areas = body.get("areas")
    if not isinstance(areas, list):
        return jsonify({"error": "areas must be a list"}), 400

    settings = {
        "areas": areas,
        "last_saved": _dt.now().isoformat(timespec="seconds"),
    }
    AREA_SETTINGS_PATH.write_text(_json.dumps(settings, ensure_ascii=False, indent=2))

    # Keep scan_prefs.json in sync so the daemon auto-start uses the right areas
    active_names = [a["name"] for a in areas if isinstance(a, dict) and a.get("active")]
    try:
        prefs = _json.loads(SCAN_PREFS_PATH.read_text())
    except Exception:
        prefs = {}
    prefs["areas"] = active_names
    SCAN_PREFS_PATH.write_text(_json.dumps(prefs, indent=2))

    return jsonify({"saved": True, "active_count": len(active_names)})


def _normalize_settings_entry(zone_codes: list[str]) -> dict | None:
    valid_codes = []
    for code in zone_codes:
        code = str(code).upper().strip()
        if not code:
            continue
        if code not in _OMI_OFFICIAL_ZONES:
            raise ValueError(f"Unknown zone code: {code}")
        if code not in valid_codes:
            valid_codes.append(code)
    if not valid_codes:
        return None

    primary = valid_codes[0]
    z = _OMI_OFFICIAL_ZONES[primary]
    return {
        "omi_zones": valid_codes,
        "zone_code": primary,
        "fascia": z["fascia"],
        "rmin": z["rmin"],
        "rmax": z["rmax"],
    }


def _write_legacy_custom_mappings(area_settings: dict) -> None:
    legacy = {}
    for nb, entry in (area_settings.get("mappings") or {}).items():
        if not isinstance(entry, dict):
            continue
        zone_code = entry.get("zone_code")
        if not zone_code:
            continue
        legacy[nb] = {
            "zone_code": zone_code,
            "fascia": entry.get("fascia"),
            "rmin": entry.get("rmin"),
            "rmax": entry.get("rmax"),
        }
    CUSTOM_MAPPINGS_PATH.write_text(_json.dumps(legacy, ensure_ascii=False, indent=2))


@app.route("/api/unmatched")
def api_unmatched():
    """Return neighbourhoods that fall back to the city-average OMI zone."""
    import fetch_rentals as _fr

    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        return jsonify({"unmatched": [], "zones": _OMI_OFFICIAL_ZONES})

    data = _json.loads(rent_path.read_text())
    custom = _fr._load_custom_mappings()

    counts: dict = {}
    for l in data:
        nb  = (l.get("neighbourhood") or "").strip()
        omi = _fr.match_omi(nb)
        if omi.get("zone") == "city average":
            counts[nb] = counts.get(nb, 0) + 1

    unmatched = [
        {
            "neighbourhood": nb,
            "count":         cnt,
            "assigned":      custom.get(nb.lower(), None),
        }
        for nb, cnt in sorted(counts.items(), key=lambda x: -x[1])
    ]
    return jsonify({"unmatched": unmatched, "zones": _OMI_OFFICIAL_ZONES})


@app.route("/api/immobiliare-zones")
def api_immobiliare_zones():
    """
    Return the list of distinct Immobiliare.it neighbourhood names that appear
    in rentals_latest.json for the given city (default: milano).
    Used by the Settings tab to let the user map areas → OMI zones.

    Query params:
      city   — city key (default: milano)
    """
    city = (request.args.get("city") or "milano").lower()
    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    areas: list[str] = []
    if rent_path.exists():
        try:
            data = _json.loads(rent_path.read_text())
            seen: set[str] = set()
            for l in data:
                if (l.get("city_key") or "milano") == city:
                    nb = (l.get("neighbourhood") or "").strip()
                    if nb and nb not in seen:
                        seen.add(nb)
                        areas.append(nb)
            areas.sort(key=str.lower)
        except Exception:
            pass
    # Also return known OMI zones so the UI can offer them as targets
    try:
        import omi_lookup as _omi
        omi_zones = [
            {
                "code":    code,
                "fascia":  info.get("fascia", "?"),
                "descr":   info.get("descr", ""),
                "loc_mid": info.get("loc_mid"),
            }
            for code, info in sorted(_omi.ZONES.items())
            if info.get("loc_mid") is not None
        ]
    except Exception:
        omi_zones = []
    return jsonify({"city": city, "areas": areas, "omi_zones": omi_zones})


@app.route("/api/mappings", methods=["GET"])
def get_mappings():
    """Return all current area settings and OMI mappings."""
    return jsonify(_load_area_settings())


@app.route("/api/mappings", methods=["POST"])
def save_mappings():
    """
    Merge posted area settings into area_settings.json and mirror the primary
    zone selection into the legacy custom mappings file used by match_omi().
    Triggers a full rescore immediately.
    """
    body = request.get_json(force=True, silent=True) or {}
    updates = body.get("mappings", {})
    settings = _load_area_settings()
    existing = settings.get("mappings") or {}

    changed = 0
    for nb_raw, value in updates.items():
        nb = nb_raw.strip().lower()
        if not nb:
            continue
        if value is None:
            if nb in existing:
                del existing[nb]
                changed += 1
            continue

        zone_codes = value.get("omi_zones") if isinstance(value, dict) else value
        if not isinstance(zone_codes, list):
            zone_codes = [zone_codes]
        try:
            normalized = _normalize_settings_entry(zone_codes)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if normalized is None:
            if nb in existing:
                del existing[nb]
                changed += 1
            continue

        if existing.get(nb) != normalized:
            existing[nb] = normalized
            changed += 1

    settings["city"] = (body.get("city") or settings.get("city") or "milano").lower()
    settings["mappings"] = existing
    _save_area_settings(settings)
    _write_legacy_custom_mappings(settings)

    # Rescore
    n = _rescore_with_new_mappings()
    return jsonify({"saved": changed, "rescored": n})


@app.route("/api/mappings", methods=["DELETE"])
def delete_mapping():
    """Remove a single mapping by neighbourhood name."""
    nb = (request.args.get("nb") or "").strip().lower()
    if not nb:
        return jsonify({"error": "nb param required"}), 400
    settings = _load_area_settings()
    existing = settings.get("mappings") or {}
    if nb in existing:
        del existing[nb]
        settings["mappings"] = existing
        _save_area_settings(settings)
        _write_legacy_custom_mappings(settings)
        _rescore_with_new_mappings()
        return jsonify({"deleted": nb})
    return jsonify({"error": "not found"}), 404


_SCORING_SETTINGS_PATH = BASE_DIR / "scoring_settings.json"

def _load_scoring_settings() -> dict:
    try:
        with open(_SCORING_SETTINGS_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


@app.route("/api/scoring-settings", methods=["GET"])
def get_scoring_settings():
    return jsonify(_load_scoring_settings())


@app.route("/api/scoring-settings", methods=["POST"])
def save_scoring_settings():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "empty body"}), 400
    existing = _load_scoring_settings()
    existing.update(data)
    try:
        with open(_SCORING_SETTINGS_PATH, "w") as f:
            _json.dump(existing, f, indent=2)
        return jsonify({"ok": True, "saved": list(data.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rescore", methods=["POST"])
def api_rescore():
    """Re-run scoring + explain on the current rentals_latest.json in-process."""
    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(rent_path, "rental"):
            return jsonify({"error": "rentals_latest.json not found and Supabase hydrate failed"}), 404
    try:
        from scoring import score_all, _load_settings
        from explain import explain_all
        with open(rent_path) as f:
            listings = _json.load(f)
        settings = _load_settings()
        scored   = score_all(listings, settings)
        explain_all(scored)
        with open(rent_path, "w") as f:
            _json.dump(scored, f, indent=2)
        # Push fresh scores back to Supabase so the deployed dashboard
        # reflects them on next reload. Quiet failure if creds missing.
        try:
            from supabase_sync import push_local_json
            push_local_json(rent_path, "rental")
        except Exception as exc:
            print(f"[rescore] supabase push failed: {exc}")
        return jsonify({"ok": True, "count": len(scored)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rescore-sales", methods=["POST"])
def api_rescore_sales():
    """Re-run scoring + explain on the current sales_latest.json in-process."""
    global _DQ_SALE_CACHE
    sale_path = DASHBOARD_DIR / "sales_latest.json"
    if not sale_path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(sale_path, "sale"):
            return jsonify({"error": "sales_latest.json not found and Supabase hydrate failed"}), 404
    try:
        from scoring import score_all_sales, _load_settings
        from explain import explain_all_sales
        with open(sale_path) as f:
            listings = _json.load(f)
        settings = _load_settings()
        scored   = score_all_sales(listings, settings)
        explain_all_sales(scored)
        with open(sale_path, "w") as f:
            _json.dump(scored, f, indent=2)
        try:
            from supabase_sync import push_local_json
            push_local_json(sale_path, "sale")
        except Exception as exc:
            print(f"[rescore-sales] supabase push failed: {exc}")
        _DQ_SALE_CACHE = None  # invalidate DQ cache
        return jsonify({"ok": True, "count": len(scored)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/geo-enrich-sales", methods=["POST"])
def start_geo_enrich_sales():
    """Start background geo enrichment of existing sale listings (one city)."""
    city = (request.args.get("city") or
            (request.get_json(silent=True) or {}).get("city") or
            "milano").lower()
    if _geo_job(city, "sale").get("running"):
        return jsonify({"ok": False, "error": f"{city} sale enrichment already running"}), 409
    t = threading.Thread(target=_geo_enrich_sales_worker, args=(city,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"{city} sale enrichment started"})


@app.route("/geo-enrich-sales", methods=["DELETE"])
def stop_geo_enrich_sales():
    """Alias for DELETE /geo-enrich — stops everything for symmetry with
    the legacy POST /geo-enrich-sales endpoint."""
    return stop_geo_enrich()


@app.route("/fetch", methods=["POST"])
def run_fetch():
    global _fetch_running

    with _fetch_lock:
        if _fetch_running:
            return jsonify({"error": "A fetch is already running"}), 409

    body   = request.get_json(silent=True) or {}
    cities = [c for c in body.get("cities", []) if c]
    pages  = body.get("pages")

    # Build subprocess command
    cmd = [sys.executable, "-u", str(SCRIPT)]
    if cities:
        cmd += ["--cities"] + cities
    if pages:
        cmd += ["--pages", str(int(pages))]
    for flag, key in [
        ("--max-price", "max_price"),
        ("--min-price", "min_price"),
        ("--min-sqm",   "min_sqm"),
        ("--max-sqm",   "max_sqm"),
        ("--min-rooms", "min_rooms"),
    ]:
        val = body.get(key)
        if val:
            cmd += [flag, str(int(val))]

    def generate():
        global _fetch_running
        _fetch_running = True
        proc = None
        try:
            yield f"data: $ {' '.join(cmd)}\n\n"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(BASE_DIR),
            )
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line:
                    yield f"data: {line}\n\n"
            proc.wait()
            rc = proc.returncode
            if rc == 0:
                yield "data: ✓ Fetch complete\n\n"
            else:
                yield f"data: ✗ Failed (exit {rc})\n\n"
            yield "event: done\ndata: {}\n\n"
        except GeneratorExit:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception as exc:
            yield f"data: ✗ {exc}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            _fetch_running = False

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _rescore_existing_json():
    """
    Rescore rentals_latest.json with the current scoring formula on startup.
    Also applies OMI polygon fields (omi_loc_mid etc.) to any listing that
    has coordinates but was cached before omi_lookup was introduced.
    """
    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        return
    try:
        data = _json.loads(rent_path.read_text())
        if not data:
            return

        # Apply polygon OMI fields to listings that are missing them
        # (fast, no network — pure in-memory lookup)
        _apply_omi_polygon(data)

        import scoring as _scoring
        scored = _scoring.score_all(data)
        from explain import explain_all
        explain_all(scored)
        clean  = [{k: v for k, v in l.items() if k != 'omi'} for l in scored]
        from dashboard_io import write_snapshot
        write_snapshot(rent_path, clean)
        print(f"  Rescore    → {len(clean)} listings updated")
    except Exception as exc:
        print(f"  Rescore    → skipped ({exc})")


def _rescore_existing_sales_json():
    """Rescore sales_latest.json with the current sale scoring formula on startup."""
    sale_path = DASHBOARD_DIR / "sales_latest.json"
    if not sale_path.exists():
        return
    try:
        data = _json.loads(sale_path.read_text())
        if not data:
            return
        _apply_omi_polygon(data)
        from scoring import score_all_sales as _score_sales
        from explain import explain_all_sales as _explain_sales
        scored = _score_sales(data)
        _explain_sales(scored)
        clean  = [{k: v for k, v in l.items() if k != 'omi'} for l in scored]
        from dashboard_io import write_snapshot
        write_snapshot(sale_path, clean)
        print(f"  Rescore (sales) → {len(clean)} listings updated")
    except Exception as exc:
        print(f"  Rescore (sales) → skipped ({exc})")


@app.route("/data-quality")
def data_quality():
    global _dq_cache, _dq_cache_ts
    if _dq_cache is not None and _time.time() - _dq_cache_ts < _DQ_CACHE_TTL:
        return jsonify(_dq_cache)
    path = DASHBOARD_DIR / "rentals_latest.json"
    if not path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(path, "rental"):
            return jsonify({"summary": {"total_listings": 0}, "listings": []})
    try:
        import data_quality as _dq
        listings = _json.loads(path.read_text())
        result = _dq.audit_all(listings)
        _dq_cache = result
        _dq_cache_ts = _time.time()
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/data-quality/sales")
def data_quality_sales():
    global _DQ_SALE_CACHE, _dq_sale_cache_ts
    if _DQ_SALE_CACHE is not None and _time.time() - _dq_sale_cache_ts < _DQ_CACHE_TTL:
        return jsonify(_DQ_SALE_CACHE)
    path = DASHBOARD_DIR / "sales_latest.json"
    if not path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(path, "sale"):
            return jsonify({"summary": {"total_listings": 0}, "listings": []})
    try:
        import data_quality as _dq
        listings = _json.loads(path.read_text())
        result = _dq.audit_all(listings, mode='sale')
        _DQ_SALE_CACHE = result
        _dq_sale_cache_ts = _time.time()
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/data-quality/listing/<listing_id>")
def data_quality_single(listing_id):
    path = DASHBOARD_DIR / "rentals_latest.json"
    if not path.exists():
        _load_env()
        from supabase_sync import hydrate_local_json
        if not hydrate_local_json(path, "rental"):
            return jsonify({"error": "no data"}), 404
    try:
        import data_quality as _dq
        listings = _json.loads(path.read_text())
        listing = next((l for l in listings if str(l.get("id")) == str(listing_id)), None)
        if listing is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(_dq.audit_single(listing))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/gems")
def gems():
    """Return listings flagged as hidden_gem=True, sorted by score_total descending."""
    path = DASHBOARD_DIR / "rentals_latest.json"
    if not path.exists():
        return jsonify([])
    try:
        listings = _json.loads(path.read_text())
        result = sorted(
            [l for l in listings if l.get("hidden_gem")],
            key=lambda l: l.get("score_total") or 0,
            reverse=True,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/cache-clear-single", methods=["POST"])
def cache_clear_single():
    body = request.get_json(silent=True) or {}
    listing_id = str(body.get("listing_id", "")).strip()
    if not listing_id:
        return jsonify({"error": "listing_id required"}), 400
    cache_path = BASE_DIR / "enriched_cache.json"
    if not cache_path.exists():
        return jsonify({"cleared": False, "reason": "cache not found"}), 404
    try:
        cache = _json.loads(cache_path.read_text())
        # Remove any source-prefixed key matching this listing_id
        removed = []
        for key in list(cache.keys()):
            if key.split(":", 1)[-1] == listing_id:
                del cache[key]
                removed.append(key)
        if removed:
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(cache, ensure_ascii=False, indent=2))
            tmp.replace(cache_path)
            from datetime import datetime
            print(f"  [cache-clear] {datetime.now():%H:%M:%S} removed {removed}", flush=True)
        global _dq_cache
        _dq_cache = None  # invalidate DQ cache
        return jsonify({"cleared": bool(removed), "listing_id": listing_id, "keys_removed": removed})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Valuation tab endpoints ──────────────────────────────────────────────────

@app.route("/api/address-autocomplete")
def api_address_autocomplete():
    """
    Address autocomplete for the Valuation tab. Calls Photon (komoot.io,
    free OSM-based geocoder — same provider already used by enrich_geo)
    biased toward Milan, returns up to 5 suggestions with coordinates
    pre-resolved and OMI zone attached. Selecting a suggestion fills the
    form without a second round-trip.
    """
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"results": []})

    import urllib.parse, urllib.request
    # Bias toward Milan (Duomo coordinates) with a tight location_bias_scale
    params = urllib.parse.urlencode({
        "q":      q + " Milano",
        "lat":    "45.4642",
        "lon":    "9.1900",
        "limit":  "8",
    })
    url = f"https://photon.komoot.io/api/?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "lume/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        return jsonify({"results": [], "error": str(exc)}), 200

    try:
        import omi_lookup as _omi
        _has_omi = True
    except Exception:
        _omi = None
        _has_omi = False

    results = []
    for feat in (data.get("features") or [])[:8]:
        if len(results) >= 5: break
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2: continue
        lon, lat = float(coords[0]), float(coords[1])
        props = feat.get("properties") or {}
        # Only return results inside the Milan OMI polygon set
        if _has_omi:
            try:
                zone, src = _omi.lookup(lat, lon)
                if src != "polygon" or not zone:
                    continue
            except Exception:
                zone = None
        else:
            zone = None
        # Build a human-readable single-line label
        bits = []
        street = props.get("street") or props.get("name") or ""
        hnum   = props.get("housenumber") or ""
        if street: bits.append(f"{street} {hnum}".strip())
        elif props.get("name"): bits.append(props["name"])
        if props.get("district") or props.get("city"):
            bits.append(props.get("district") or props.get("city"))
        if props.get("postcode"): bits.append(props["postcode"])
        label = ", ".join([b for b in bits if b])
        if not label: continue
        results.append({
            "label":      label,
            "lat":        lat,
            "lng":        lon,
            "omi_zona":   zone.get("zona")   if zone else None,
            "omi_fascia": zone.get("fascia") if zone else None,
            "omi_descr":  zone.get("descr")  if zone else None,
        })
    return jsonify({"results": results})


@app.route("/api/valuation-geocode")
def api_valuation_geocode():
    """
    Geocode a free-form address (Milan-biased) and resolve its OMI zone via
    point-in-polygon. Returns { lat, lng, omi_zona, omi_fascia, omi_descr }
    or { error }. Used by the Valuation tab's address-input live preview.
    """
    address = request.args.get("address", "").strip()
    if not address:
        return jsonify({"error": "no address provided"}), 400

    # Reuse enrich_geo's polygon-validated multi-result geocoder so we
    # automatically reject same-named streets in adjacent municipalities.
    try:
        import enrich_geo as _eg
    except Exception as exc:
        return jsonify({"error": f"geocoder unavailable: {exc}"}), 500

    coords = _eg._geocode(address)
    if not coords:
        # Try once more with explicit "Milano" hint
        coords = _eg._geocode(f"{address}, Milano")
    if not coords:
        return jsonify({"error": "address not found inside Milan"}), 404

    lat, lng = coords
    try:
        import omi_lookup as _omi
        zone, src = _omi.lookup(lat, lng)
    except Exception:
        zone, src = None, "failed"

    if not zone or src != "polygon":
        return jsonify({
            "lat": lat, "lng": lng, "omi_zona": None,
            "error": "coordinates outside Milan OMI zones",
        }), 200

    return jsonify({
        "lat":        lat,
        "lng":        lng,
        "omi_zona":   zone.get("zona"),
        "omi_fascia": zone.get("fascia"),
        "omi_descr":  zone.get("descr"),
        "omi_source": "nominatim+polygon",
    })


@app.route("/api/scrape-listing")
def api_scrape_listing():
    """
    Scrape one listing on demand for the Valuation tab. Reuses the
    Immobiliare __NEXT_DATA__ extraction from fetch_rentals.py — heavy
    (~5 s) because it spins up a real Edge browser, so this is local-only.
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    if "immobiliare.it" not in url and "idealista.it" not in url:
        return jsonify({"error": "unsupported URL — paste an Immobiliare or Idealista listing"}), 400

    try:
        listing = _scrape_listing_sync(url)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not listing:
        return jsonify({"error": "could not parse listing — try filling the form manually"}), 422
    return jsonify({"listing": listing})


def _scrape_listing_sync(url: str) -> dict | None:
    """Run a single-listing scrape via nodriver. Blocks ~5s."""
    import asyncio
    try:
        return asyncio.run(_scrape_listing_async(url))
    except RuntimeError:
        # Already inside an event loop (rare in Flask threaded mode) — use a
        # nested loop. asyncio.new_event_loop is the conservative path.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_scrape_listing_async(url))
        finally:
            loop.close()


async def _scrape_listing_async(url: str) -> dict | None:
    """
    Scrape one listing. Opens Edge VISIBLY so the user can solve the
    Immobiliare/Idealista bot-check captcha if it appears. Polls the page
    for up to SCRAPE_TIMEOUT seconds waiting for the real listing data to
    appear. This mirrors the manual-captcha pattern the daemon scanner
    uses in fetch_rentals.py.
    """
    import asyncio as _asyncio
    import nodriver as uc
    from fetch_rentals import EDGE_PATH

    is_immobiliare = "immobiliare.it" in url
    is_idealista   = "idealista.it" in url

    SCRAPE_TIMEOUT = 30.0   # gives the user time to solve a manual captcha
    POLL_INTERVAL  = 1.0

    def _find_real_estate(nd):
        """Walk parsed __NEXT_DATA__ looking for a realEstate dict. Some
        Immobiliare layouts ship realEstate as a JSON-encoded *string* or
        wrap queries in malformed entries — we tolerate both and only
        return a dict that has `properties` (the actual listing payload).
        """
        if not isinstance(nd, dict):
            return None

        def _coerce(v):
            """Accept a dict, or a JSON-string that decodes to a dict.
            Returns None for anything else."""
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    parsed = _json.loads(v)
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    return None
            return None

        def _is_real_estate(v):
            d = _coerce(v)
            return d if (d is not None and d.get("properties")) else None

        # Fast path: dehydratedState queries
        props_root = nd.get("props") if isinstance(nd.get("props"), dict) else {}
        page_props = props_root.get("pageProps") if isinstance(props_root.get("pageProps"), dict) else {}
        queries = (page_props.get("dehydratedState") or {}).get("queries") or []
        if isinstance(queries, list):
            for q in queries:
                if not isinstance(q, dict): continue
                state = q.get("state") if isinstance(q.get("state"), dict) else {}
                data  = state.get("data")
                data  = _coerce(data) or {}
                re_v  = _is_real_estate(data.get("realEstate"))
                if re_v: return re_v

        # Second path: realEstate placed directly on pageProps
        re_v = _is_real_estate(page_props.get("realEstate"))
        if re_v: return re_v

        # DFS fallback — find any dict with 'realEstate' whose decoded value
        # is itself a dict carrying 'properties'.
        stack, seen = [nd], 0
        while stack and seen < 5000:
            seen += 1
            v = stack.pop()
            if isinstance(v, dict):
                cand = _is_real_estate(v.get("realEstate"))
                if cand: return cand
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
        return None

    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False, lang="it-IT",
    )
    try:
        tab = await browser.get(url)
        # Bring the window forward so the user notices the captcha
        try: await tab.activate()
        except Exception: pass

        deadline = _asyncio.get_event_loop().time() + SCRAPE_TIMEOUT

        if is_immobiliare:
            re_data = None
            while _asyncio.get_event_loop().time() < deadline:
                text = await tab.evaluate(
                    "(()=>{const el=document.getElementById('__NEXT_DATA__');return el?el.textContent:null;})()"
                )
                if text:
                    try:
                        nd = _json.loads(text)
                    except Exception:
                        nd = None
                    re_data = _find_real_estate(nd) if nd else None
                    if re_data:
                        break
                await _asyncio.sleep(POLL_INTERVAL)
            if not re_data:
                return None
            return _parse_immobiliare_detail(re_data)

        if is_idealista:
            dom = None
            while _asyncio.get_event_loop().time() < deadline:
                dom = await tab.evaluate("""
                  (() => {
                    const t = sel => { const el=document.querySelector(sel); return el?(el.innerText||el.textContent||'').trim():''; };
                    return {
                      price_text:     t('.info-data-price') || t('[class*="price"]'),
                      size_text:      t('[class*="size-feature"]') || t('[class*="superficie"]'),
                      rooms_text:     t('[class*="rooms"]') || t('[class*="locali"]'),
                      floor_text:     t('[class*="floor"]') || t('[class*="piano"]'),
                      address_text:   t('.main-info__title-minor') || t('h1.jumbotron-title') || t('.txt-title'),
                      condition_text: t('[class*="condition"]') || t('[class*="stato"]'),
                    };
                  })()
                """)
                # Real listing has at least price + size populated
                if dom and dom.get("price_text") and dom.get("size_text"):
                    return _parse_idealista_detail(dom)
                await _asyncio.sleep(POLL_INTERVAL)
            return None
    finally:
        try: await browser.close()
        except Exception: pass
    return None


def _parse_immobiliare_detail(re_data) -> dict:
    """Pull a Valuation-tab-shaped dict from Immobiliare's realEstate object.
    Uses the same field-alias logic as fetch_rentals.parse_rental — the
    Immobiliare API uses inconsistent key names across pages so each field
    needs multiple fallbacks. Defensive against unexpected shapes too."""
    re_data = re_data if isinstance(re_data, dict) else {}

    def _d(v): return v if isinstance(v, dict) else {}
    def _int(v):
        try: return int(float(str(v).split(" ")[0]))
        except Exception: return None

    def _bool(*vals):
        """Coerce the first truthy/known-falsy candidate into bool. Returns
        None if every candidate is None (= "unknown")."""
        for v in vals:
            if v is None: continue
            if isinstance(v, bool): return v
            if isinstance(v, (int, float)): return bool(v)
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("", "null"): continue
                return s not in ("false", "no", "0")
        return None

    props_list = re_data.get("properties") if isinstance(re_data.get("properties"), list) else []
    prop       = _d(props_list[0]) if props_list else {}
    price_data = _d(re_data.get("price"))
    location   = _d(prop.get("location"))
    floor_raw  = _d(prop.get("floor"))

    # Build a feature-type set (Immobiliare ships some signals only here)
    feats_list = prop.get("features") if isinstance(prop.get("features"), list) else (
                 re_data.get("features") if isinstance(re_data.get("features"), list) else [])
    feat_types: set = set()
    feat_labels: str = ""
    for f in feats_list:
        if isinstance(f, dict):
            ft = (f.get("type") or f.get("name") or "")
            feat_types.add(str(ft).lower())
            feat_labels += " " + str(f.get("label") or "").lower()

    def _feat_has(*keywords):
        for k in keywords:
            kl = k.lower()
            if kl in feat_types: return True
            if kl in feat_labels: return True
        return False

    # Condition — try the same priority chain as parse_rental
    typology = _d(re_data.get("typology"))
    condition = (prop.get("ga4Condition")
                 or prop.get("condition")
                 or typology.get("name")
                 or "")
    if isinstance(condition, dict):
        condition = condition.get("value") or ""
    condition = str(condition or "").strip().lower()

    # Elevator
    elevator = _bool(prop.get("elevator"), prop.get("hasElevator"))

    # Balcony / terrace / garden
    has_balcony = _bool(prop.get("hasBalcony"), prop.get("balcony"),
                        prop.get("hasTerrace"), prop.get("terrace"),
                        prop.get("hasGarden"), prop.get("garden"))
    if has_balcony is None and _feat_has("balcony","terrace","garden","balcon","terrazza","giardino"):
        has_balcony = True

    # Parking / box / garage
    has_parking = _bool(prop.get("hasParking"), prop.get("parking"),
                        prop.get("hasGarage"), prop.get("garage"),
                        prop.get("hasBox"), prop.get("box"))
    if has_parking is None and _feat_has("parking","garage","box","parcheggio","posto auto"):
        has_parking = True

    # Furnished
    furnished = _bool(prop.get("furnished"), prop.get("isFurnished"),
                      prop.get("arredato"), re_data.get("furnished"))
    if furnished is None and _feat_has("arredato","furnished"):
        furnished = True

    return {
        "source":      "immobiliare",
        "address":     location.get("address") or "",
        "latitude":    location.get("latitude"),
        "longitude":   location.get("longitude"),
        "sqm":         _int(prop.get("surface") or prop.get("surfaceValue")),
        "price":       _int(price_data.get("value")),
        "rooms":       _int(prop.get("rooms")),
        "floor_n":     _int(floor_raw.get("value") or floor_raw.get("abbreviation")),
        "floor_label": str(floor_raw.get("abbreviation") or ""),
        "condition":   condition,
        "elevator":    elevator,
        "has_balcony": has_balcony,
        "has_parking": has_parking,
        "furnished":   furnished,
    }


def _parse_idealista_detail(dom: dict) -> dict:
    """Pull a Valuation-tab-shaped dict from an Idealista detail page DOM."""
    import re as _re
    def _int(txt):
        s = str(txt or "").replace(".", "").replace(",", "")
        m = _re.search(r"\d+", s)
        return int(m.group(0)) if m else None
    return {
        "source":      "idealista",
        "address":     dom.get("address_text") or "",
        "latitude":    None,
        "longitude":   None,
        "sqm":         _int(dom.get("size_text")),
        "price":       _int(dom.get("price_text")),
        "rooms":       _int(dom.get("rooms_text")),
        "floor_n":     _int(dom.get("floor_text")),
        "floor_label": str(dom.get("floor_text") or ""),
        "condition":   str(dom.get("condition_text") or "").lower(),
        "elevator":    None,
        "has_balcony": None,
        "has_parking": None,
        "furnished":   None,
    }


def _supabase_autosync_watcher() -> None:
    """
    Background loop: when dashboard/{rentals,sales}_latest.json is rewritten
    (a scanner cycle finished, or rescore/enrich mutated it), push the new
    rows to Supabase so the deployed dashboard sees them on next reload.

    Without this, locally-triggered scans only update the JSON on disk —
    Supabase stays stale until the daily GitHub Actions sync runs.

    Quietly no-ops when SUPABASE_URL / SUPABASE_SERVICE_KEY aren't set.
    """
    import os as _os
    import time as _t
    _load_env()
    if not (_os.environ.get("SUPABASE_URL") and _os.environ.get("SUPABASE_SERVICE_KEY")):
        print("  Autosync  → disabled (Supabase env not set)", flush=True)
        return

    paths = [
        (DASHBOARD_DIR / "rentals_latest.json", "rental"),
        (DASHBOARD_DIR / "sales_latest.json",   "sale"),
    ]
    # Prime baselines from current mtime so we don't re-push on startup.
    last_mtime: dict[str, float] = {
        str(p): (p.stat().st_mtime if p.exists() else 0.0) for p, _ in paths
    }
    print("  Autosync  → watching dashboard/*.json for scanner cycles", flush=True)

    POLL_SEC    = 30
    QUIET_SEC   = 10  # require file unchanged for this long before syncing
                      # (avoids pushing mid-write while scoring is still flushing)
    while True:
        _t.sleep(POLL_SEC)
        try:
            from supabase_sync import push_local_json
        except Exception as exc:
            print(f"  [autosync] import failed, stopping: {exc}")
            return

        for path, kind in paths:
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            prev  = last_mtime.get(str(path), 0.0)
            if mtime <= prev:
                continue
            # Wait until the file has been quiet for QUIET_SEC — avoids
            # racing the scanner's atomic .tmp → final replace.
            if _t.time() - mtime < QUIET_SEC:
                continue
            try:
                ok = push_local_json(path, kind)
                last_mtime[str(path)] = mtime
                if ok:
                    print(f"  [autosync] pushed {path.name} → Supabase", flush=True)
            except Exception as exc:
                print(f"  [autosync] push {path.name} failed: {exc}", flush=True)


if __name__ == "__main__":
    # Load saved scan prefs (written by the dashboard whenever filters change)
    _prefs = {}
    if SCAN_PREFS_PATH.exists():
        try:
            _prefs = _json.loads(SCAN_PREFS_PATH.read_text())
        except Exception:
            pass

    # Rescore existing JSON with current formula before starting
    _rescore_existing_json()
    _rescore_existing_sales_json()

    # Auto-push to Supabase whenever a scan cycle rewrites the local JSON.
    # Background daemon — dies with the parent process on Ctrl+C.
    threading.Thread(target=_supabase_autosync_watcher, daemon=True).start()

    print(f"\n  Immobiliare Scorer")
    print(f"  Dashboard  → http://localhost:8000/")
    print(f"  Scanner    → stopped (start from dashboard when needed)")
    print(f"  Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
