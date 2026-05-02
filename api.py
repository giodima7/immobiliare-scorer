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
_geo_status: dict = {"running": False, "done": 0, "total": 0, "error": None}
_geo_stop   = threading.Event()

# ── Background geo enrichment — sales ────────────────────────────────────────
_geo_sale_status: dict = {"running": False, "done": 0, "total": 0, "error": None}
_geo_sale_stop   = threading.Event()

# ── Sales fetch (fetch_listings.py) ──────────────────────────────────────────
_sale_fetch_lock   = threading.Lock()
_sale_proc: subprocess.Popen | None = None
_sale_fetch_status: dict = {"running": False, "last_run": None, "count": 0, "error": None}

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


def _geo_enrich_worker():
    """
    Background thread: enrich all listings in rentals_latest.json that are
    not yet in the enrichment cache.

    Strategy:
      • OMI polygon lookup + haversine POI distances: in-memory, parallel (fast)
      • OSRM walk times: one table API call per metro station per 500-listing chunk
      • Flush to disk after every chunk so the dashboard updates in real time
    """
    global _geo_status
    _geo_stop.clear()
    _geo_status = {"running": True, "done": 0, "total": 0, "error": None}
    try:
        import enrichment_cache as _ecache
        import scoring as _scoring

        rent_path = DASHBOARD_DIR / "rentals_latest.json"
        if not rent_path.exists():
            _geo_status["error"] = "rentals_latest.json not found"
            return

        data = _json.loads(rent_path.read_text())
        _ecache.load()   # warm up in-memory cache

        # ── Pass 1: fast in-memory OMI polygon application ───────────────────
        # Fixes all listings that have coordinates but were cached before
        # omi_lookup existed (no Overpass call needed).
        n_omi = _apply_omi_polygon(data, _ecache)
        if n_omi > 0:
            _flush_geo(data, rent_path, _scoring)

        # ── Pass 2: Overpass POI enrichment for uncached listings ─────────────
        # Skip only if the cache entry already contains Overpass data (geo_score).
        # A cache entry with only OMI polygon fields (no geo_score) still needs
        # Overpass enrichment — this happens when the scan's inline enrich_batch
        # timed out but the OMI polygon pass ran and partially populated the cache.
        def _needs_overpass(l):
            cached = _ecache.get(l.get("source", "immobiliare"), l["id"])
            if cached is None:
                return True
            # Re-enrich only if geo_score is missing entirely.
            # metro_walk_routed is no longer used as a trigger: the table API
            # (/table/v1/foot/) is unavailable on the public OSRM server, so
            # walk times are always haversine-derived in batch mode.
            return cached.get("geo_score") is None

        need_idx = [
            i for i, l in enumerate(data)
            if _needs_overpass(l) and l.get("latitude") and l.get("longitude")
        ]
        _geo_status["total"] = len(need_idx) + n_omi
        _geo_status["done"]  = n_omi   # polygon pass already counted
        print(f"  [geo] background enrichment: {len(need_idx)} listings to process")

        from enrich_geo import enrich_batch as _enrich_batch

        WORKERS         = 8    # parallel threads inside each enrich_batch() call
        CHUNK_SIZE      = 500  # listings per batch — large = far fewer OSRM round-trips
        MAX_NULL_STREAK = 10   # stop if a whole chunk returns no geo data
        done_count      = 0
        null_streak     = 0

        # Large chunks: all OMI polygon lookups and haversine POI distances run
        # in parallel (pure in-memory, ~0.6s per 500 listings).  No OSRM table
        # calls are made — the public server only exposes foot-profile routing,
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
            _geo_status["done"] = n_omi + done_count

            # Flush after every chunk so the dashboard reflects progress in real time
            clean = [{k: v for k, v in l.items() if k != "omi"} for l in data]
            tmp = rent_path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
            tmp.replace(rent_path)
            print(f"  [geo] {done_count}/{len(need_idx)} done")

        # Final flush — mark total complete so UI shows 100 %
        _geo_status["done"] = _geo_status["total"]
        _flush_geo(data, rent_path, _scoring)
        print(f"  [geo] enrichment complete: {n_omi + done_count} done")

    except Exception as exc:
        _geo_status["error"] = str(exc)
        print(f"  [geo] worker crashed: {exc}")
    finally:
        _geo_status["running"] = False


def _flush_geo(data: list, path: Path, scoring_mod):
    """Rescore and write JSON atomically."""
    global _dq_cache
    _dq_cache = None  # invalidate data-quality cache after new data is written
    scored = scoring_mod.score_all(list(data))
    clean  = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
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
    tmp.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _geo_enrich_sales_worker():
    """
    Background thread: enrich all listings in sales_latest.json that are
    not yet in the enrichment cache. Uses `sale:` prefix for cache keys.
    """
    global _geo_sale_status
    _geo_sale_stop.clear()
    _geo_sale_status = {"running": True, "done": 0, "total": 0, "error": None}
    try:
        import enrichment_cache as _ecache

        sale_path = DASHBOARD_DIR / "sales_latest.json"
        if not sale_path.exists():
            _geo_sale_status["error"] = "sales_latest.json not found"
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
            print(f"  [geo-sale] backfill: {n_backfill} from cache, {n_omi} OMI polygon")
            _flush_sale_geo(data, sale_path)

        def _needs_overpass(l):
            cached = _ecache.get(l.get("source", "sale"), l["id"])
            if cached is None:
                return True
            return cached.get("geo_score") is None

        need_idx = [
            i for i, l in enumerate(data)
            if _needs_overpass(l) and l.get("latitude") and l.get("longitude")
        ]
        _geo_sale_status["total"] = len(need_idx) + n_omi
        _geo_sale_status["done"]  = n_omi
        print(f"  [geo-sale] background enrichment: {len(need_idx)} listings to process")

        from enrich_geo import enrich_batch as _enrich_batch

        WORKERS    = 8
        CHUNK_SIZE = 500
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
            _geo_sale_status["done"] = n_omi + done_count

            clean = [{k: v for k, v in l.items() if k != "omi"} for l in data]
            tmp = sale_path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
            tmp.replace(sale_path)
            print(f"  [geo-sale] {done_count}/{len(need_idx)} done")

        _geo_sale_status["done"] = _geo_sale_status["total"]
        _flush_sale_geo(data, sale_path)
        print(f"  [geo-sale] enrichment complete: {n_omi + done_count} done")

    except Exception as exc:
        _geo_sale_status["error"] = str(exc)
        print(f"  [geo-sale] worker crashed: {exc}")
    finally:
        _geo_sale_status["running"] = False

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

def _build_scanner_cmd(body: dict) -> list:
    """Build the fetch_rentals.py --daemon command from a prefs dict.
    Areas are NOT passed here — fetch_rentals.py reads active areas from
    area_settings.json directly via _load_active_areas()."""
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_rentals.py"), "--daemon"]
    if body.get("max_rent"):
        cmd += ["--max-rent",  str(int(body["max_rent"]))]
    if body.get("min_rooms"):
        cmd += ["--min-rooms", str(int(body["min_rooms"]))]
    if body.get("pages"):
        cmd += ["--pages",     str(int(body["pages"]))]
    if body.get("email"):
        cmd.append("--email")
    return cmd


def _start_scanner_proc(prefs: dict) -> subprocess.Popen:
    cmd    = _build_scanner_cmd(prefs)
    log_fh = open(BASE_DIR / "scanner.log", "a")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_fh, stderr=log_fh)


def _start_idealista_proc(prefs: dict) -> subprocess.Popen:
    cmd = [sys.executable, "-u", str(BASE_DIR / "fetch_idealista.py"), "--daemon"]
    # Areas are driven by area_settings.json — not passed here.
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
    global _scanner_proc
    with _scanner_lock:
        if _scanner_proc and _scanner_proc.poll() is None:
            return jsonify({"error": "already running", "pid": _scanner_proc.pid}), 409
        body = request.get_json(silent=True) or {}
        # Persist prefs (without areas — areas are owned by area_settings.json)
        prefs_to_save = {k: v for k, v in body.items() if k != "areas"}
        SCAN_PREFS_PATH.write_text(_json.dumps(prefs_to_save, indent=2))
        _scanner_proc = _start_scanner_proc(body)
        result = {"started": True, "pid": _scanner_proc.pid}
        # Always start the Idealista daemon alongside Immobiliare.
        # (fetch_idealista.py uses browser automation — no API credentials needed)
        global _idealista_proc
        _idealista_proc = _start_idealista_proc(body)
        result["idealista_pid"] = _idealista_proc.pid
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
    """Start background geo enrichment of existing listings."""
    if _geo_status.get("running"):
        return jsonify({"ok": False, "error": "already running"}), 409
    t = threading.Thread(target=_geo_enrich_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "geo enrichment started"})


@app.route("/geo-enrich", methods=["DELETE"])
def stop_geo_enrich():
    """Stop the background geo enrichment."""
    _geo_stop.set()
    return jsonify({"ok": True})


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
            return jsonify({"ok": False, "error": "rentals_latest.json not found"}), 404
        data = _json.loads(rent_path.read_text())
        _ecache.load()
        updated = _apply_omi_polygon(data, _ecache)
        scored  = _scoring.score_all(data)
        clean   = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
        rent_path.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
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
    clean  = [{k: v for k, v in l.items() if k != "omi"} for l in scored]
    rent_path.write_text(_json.dumps(clean, ensure_ascii=False))
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


# ── Idealista area settings ────────────────────────────────────────────────────
IDEALISTA_AREA_SETTINGS_PATH = BASE_DIR / "idealista_area_settings.json"


def _load_idealista_area_settings() -> dict:
    """Load idealista_area_settings.json, returning {areas: [...]}."""
    if IDEALISTA_AREA_SETTINGS_PATH.exists():
        try:
            return _json.loads(IDEALISTA_AREA_SETTINGS_PATH.read_text())
        except Exception:
            pass
    return {"areas": []}


@app.route("/idealista-area-settings", methods=["GET"])
def get_idealista_area_settings():
    """Return Idealista zone settings (areas list with active flags)."""
    return jsonify(_load_idealista_area_settings())


@app.route("/idealista-area-settings", methods=["POST"])
def save_idealista_area_settings():
    """Save Idealista zone settings to idealista_area_settings.json."""
    from datetime import datetime as _dt
    body  = request.get_json(force=True, silent=True) or {}
    areas = body.get("areas")
    if not isinstance(areas, list):
        return jsonify({"error": "areas must be a list"}), 400

    existing = _load_idealista_area_settings()
    # Preserve any extra fields (listings count, etc.) from the original file
    existing_by_name = {a["name"]: a for a in existing.get("areas", []) if isinstance(a, dict)}
    merged = []
    for a in areas:
        if not isinstance(a, dict):
            continue
        name = a.get("name", "")
        base = {**existing_by_name.get(name, {}), **a}
        merged.append(base)
    # Also keep any zones that weren't sent (preserve their active=False state)
    sent_names = {a["name"] for a in merged}
    for name, orig in existing_by_name.items():
        if name not in sent_names:
            merged.append({**orig, "active": False})

    settings = {
        **{k: v for k, v in existing.items() if k not in ("areas", "last_saved")},
        "areas": merged,
        "last_saved": _dt.now().isoformat(timespec="seconds"),
    }
    IDEALISTA_AREA_SETTINGS_PATH.write_text(
        _json.dumps(settings, ensure_ascii=False, indent=2)
    )
    active_count = sum(1 for a in merged if a.get("active"))
    return jsonify({"saved": True, "active_count": active_count})


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
        return jsonify({"error": "rentals_latest.json not found"}), 404
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
        return jsonify({"ok": True, "count": len(scored)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rescore-sales", methods=["POST"])
def api_rescore_sales():
    """Re-run scoring + explain on the current sales_latest.json in-process."""
    global _DQ_SALE_CACHE
    sale_path = DASHBOARD_DIR / "sales_latest.json"
    if not sale_path.exists():
        return jsonify({"error": "sales_latest.json not found"}), 404
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
        _DQ_SALE_CACHE = None  # invalidate DQ cache
        return jsonify({"ok": True, "count": len(scored)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/geo-enrich-sales", methods=["POST"])
def start_geo_enrich_sales():
    """Start background geo enrichment of existing sale listings."""
    if _geo_sale_status.get("running"):
        return jsonify({"ok": False, "error": "already running"}), 409
    t = threading.Thread(target=_geo_enrich_sales_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "sales geo enrichment started"})


@app.route("/geo-enrich-sales", methods=["DELETE"])
def stop_geo_enrich_sales():
    """Stop the background sales geo enrichment."""
    _geo_sale_stop.set()
    return jsonify({"ok": True})


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
        clean  = [{k: v for k, v in l.items() if k != 'omi'} for l in scored]
        rent_path.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
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
        sale_path.write_text(_json.dumps(clean, ensure_ascii=False, indent=2))
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

    print(f"\n  Immobiliare Scorer")
    print(f"  Dashboard  → http://localhost:8000/")
    print(f"  Scanner    → stopped (start from dashboard when needed)")
    print(f"  Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
