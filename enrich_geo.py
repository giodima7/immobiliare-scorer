#!/usr/bin/env python3
"""
enrich_geo.py
─────────────
Geospatial enrichment for rental listings.

For each listing this module:
  1. Resolves coordinates (listing lat/lng → latitude/longitude → Nominatim geocoding)
  2. Calls omi_lookup.lookup() for exact polygon-based OMI zone + price benchmarks
  3. Queries Overpass API for nearby POIs (metro, tram, supermarket, park, university)

Returns a dict of geo+OMI fields (NOT the full listing).

Usage:
    from enrich_geo import enrich, enrich_batch
    geo = enrich(listing)         # returns geo dict only
    geos = enrich_batch(listings) # returns list of geo dicts (parallel)

    python3 enrich_geo.py --test  # self-test with three Milan coordinates
"""

import json
import math
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

_OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

SEARCH_RADIUS = 2000   # metres

# ── Local POI dataset (bulk download once, then offline) ──────────────────────
# Milan bounding box (lat_min, lon_min, lat_max, lon_max)
_MILAN_BBOX   = (45.38, 9.02, 45.56, 9.32)
_POIS_PATH    = Path(__file__).parent / "milan_pois.json"
_local_pois   = None   # loaded on first call to load_local_pois()
_pois_checked = False  # avoid repeated stat() calls
_pois_lock    = threading.Lock()  # prevents race when multiple threads load simultaneously


def load_local_pois() -> "dict | None":
    """Return in-memory local POI dataset, loading from disk on first call."""
    global _local_pois, _pois_checked
    with _pois_lock:
        if _pois_checked:
            return _local_pois
        _pois_checked = True
        if not _POIS_PATH.exists():
            return None
        try:
            _local_pois = json.loads(_POIS_PATH.read_text())
            n = sum(len(v) for v in _local_pois.values() if isinstance(v, list))
            print(f"  [geo] local POI dataset: {n} elements", file=sys.stderr)
        except Exception as exc:
            print(f"  [geo] failed to load milan_pois.json: {exc}", file=sys.stderr)
            _local_pois = None
        return _local_pois


def download_milan_pois() -> dict:
    """
    Download all POIs for the Milan area in ONE bulk Overpass query and save to
    milan_pois.json.  After this runs, enrich() uses the local file and makes
    zero Overpass calls per listing.
    """
    global _local_pois, _pois_checked
    lat_min, lon_min, lat_max, lon_max = _MILAN_BBOX
    bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    query = (
        f"[out:json][timeout:90];\n(\n"
        f'  node["station"="subway"]({bbox});\n'
        f'  node["railway"="subway_entrance"]({bbox});\n'
        f'  node["railway"="station"]["subway"="yes"]({bbox});\n'
        f'  node["railway"="tram_stop"]({bbox});\n'
        f'  node["shop"="supermarket"]({bbox});\n'
        f'  way["shop"="supermarket"]({bbox});\n'
        f'  way["leisure"="park"]({bbox});\n'
        f'  node["amenity"="university"]({bbox});\n'
        f'  way["amenity"="university"]({bbox});\n'
        f'  node["amenity"="college"]({bbox});\n'
        f'  way["amenity"="college"]({bbox});\n'
        f");\nout center bb;"
    )
    print("  [geo] downloading Milan POI dataset (one-time)…", file=sys.stderr)
    elements = _overpass(query, timeout_http=90, retries=5)

    pois: dict = {"metro": [], "tram": [], "supermarket": [], "park": [], "university": []}
    for el in elements:
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {})
        if _is_metro(el):
            pois["metro"].append({
                "lat": lat, "lon": lon,
                "name":  tags.get("name") or tags.get("name:it") or "",
                "lines": _metro_lines(tags),
                "is_station": tags.get("station") == "subway",
            })
        elif _is_tram(el):
            pois["tram"].append({"lat": lat, "lon": lon})
        elif _is_supermarket(el):
            pois["supermarket"].append({"lat": lat, "lon": lon})
        elif tags.get("leisure") == "park":
            bounds = el.get("bounds") or {}
            area = 0.0
            if bounds:
                dlat = abs(bounds.get("maxlat", 0) - bounds.get("minlat", 0))
                dlon = abs(bounds.get("maxlon", 0) - bounds.get("minlon", 0))
                area = dlat * 111_000 * dlon * 111_000 * math.cos(math.radians(lat))
            if area >= 5000 or not bounds:
                pois["park"].append({"lat": lat, "lon": lon})
        elif _is_university(el):
            pois["university"].append({"lat": lat, "lon": lon})

    from datetime import datetime
    pois["downloaded_at"] = datetime.now().isoformat(timespec="seconds")
    _POIS_PATH.write_text(json.dumps(pois, ensure_ascii=False, indent=2))
    _local_pois   = pois
    _pois_checked = True

    counts = {k: len(v) for k, v in pois.items() if isinstance(v, list)}
    print(f"  [geo] POI dataset saved → {counts}", file=sys.stderr)
    return pois


def _nearest_dist(elements: list, lat: float, lon: float) -> "int | None":
    """Distance in metres to the nearest element in a local POI list."""
    best = None
    for el in elements:
        d = _dist_m(lat, lon, el["lat"], el["lon"])
        if best is None or d < best:
            best = d
    return best


def _nearest_metro_local(elements: list, lat: float, lon: float):
    """Return (dist_m, name, lines, el_lat, el_lon) for nearest metro from local list."""
    if not elements:
        return None, None, None, None, None
    best_d, best_el = None, None
    for el in elements:
        d = _dist_m(lat, lon, el["lat"], el["lon"])
        if best_d is None or d < best_d:
            best_d, best_el = d, el
    if best_el is None:
        return None, None, None, None, None
    name  = best_el.get("name") or None
    lines = best_el.get("lines") or None
    if not lines:
        for el in elements:
            if el.get("is_station") and el.get("lines"):
                d = _dist_m(lat, lon, el["lat"], el["lon"])
                if best_d is None or d <= best_d + 100:
                    lines = el["lines"]
                    if not name:
                        name = el.get("name") or None
                    break
    return best_d, name or None, lines or None, best_el["lat"], best_el["lon"]


_osrm_semaphore = threading.Semaphore(3)  # max 3 concurrent single-listing OSRM calls


def _osrm_walk(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> "tuple[int, int] | None":
    """
    Actual walking distance + time via OSRM public API (foot profile).
    Returns (dist_m, walk_min) or None on failure.
    Falls back gracefully so callers can use haversine instead.
    """
    with _osrm_semaphore:
        try:
            url = (
                f"https://router.project-osrm.org/route/v1/foot/"
                f"{from_lon},{from_lat};{to_lon},{to_lat}"
                f"?overview=false"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "ImmobiliareScorer/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]
                dist_m   = round(route["distance"])
                walk_min = max(1, round(route["duration"] / 60))
                return dist_m, walk_min
        except Exception as exc:
            print(f"  [geo] OSRM failed: {exc}", file=sys.stderr)
    return None


def _osrm_table(metro_el: dict, dest_coords: list) -> "list[tuple[None,int]|None]":
    """
    OSRM table API: one source (metro station) → N destinations (listings).
    Returns list of (None, walk_min) or None per destination.
    Distance is not requested (unsupported on the public foot-profile table endpoint);
    the caller keeps haversine metro_nearest_dist_m when OSRM is unavailable.
    Much more efficient than N individual route calls.
    """
    if not dest_coords:
        return []
    n = len(dest_coords)
    coords_str = f"{metro_el['lon']},{metro_el['lat']}"
    for lat, lon in dest_coords:
        coords_str += f";{lon},{lat}"
    dests = ",".join(str(i) for i in range(1, n + 1))
    url = (
        f"https://router.project-osrm.org/table/v1/foot/{coords_str}"
        f"?sources=0&destinations={dests}&annotations=duration"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ImmobiliareScorer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data.get("code") != "Ok":
            return [None] * n
        durations = (data.get("durations") or [[]])[0]
        results = []
        for i in range(n):
            d_secs = durations[i] if i < len(durations) else None
            if d_secs is None:
                results.append(None)
            else:
                results.append((None, max(1, round(d_secs / 60))))
        return results
    except Exception as exc:
        print(f"  [geo] OSRM table failed ({n} dests): {exc}", file=sys.stderr)
        return [None] * n


def _batch_osrm_metro(listings: list, results: list, metro_elements: list) -> None:
    """
    Post-process results: replace haversine metro walk times with OSRM-routed times.
    Groups by nearest metro station → one table API call per unique station.
    Mutates results in-place.

    NOTE: Not called from enrich_batch() — the public OSRM server at
    router.project-osrm.org exposes /table/v1/ only for the car profile.
    Kept here for use with a self-hosted OSRM instance.
    """
    if not metro_elements:
        return
    groups: dict = {}
    for i, listing in enumerate(listings):
        if not results[i].get("geo_score") and results[i].get("geo_score") != 0:
            continue  # listing failed enrichment
        lat = listing.get("latitude") or listing.get("lat")
        lon = listing.get("longitude") or listing.get("lng")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        _, _, _, m_lat, m_lon = _nearest_metro_local(metro_elements, lat, lon)
        if m_lat is None:
            continue
        groups.setdefault((m_lat, m_lon), []).append((i, lat, lon))

    TABLE_CHUNK = 50   # max destinations per table call (keeps URL short)
    total_routed = 0
    for (m_lat, m_lon), group in groups.items():
        metro_el = {"lat": m_lat, "lon": m_lon}
        for chunk_start in range(0, len(group), TABLE_CHUNK):
            chunk = group[chunk_start:chunk_start + TABLE_CHUNK]
            dest_coords = [(lat, lon) for _, lat, lon in chunk]
            osrm_results = _osrm_table(metro_el, dest_coords)
            for (result_idx, _, _), osrm in zip(chunk, osrm_results):
                if osrm:
                    dist_m, walk_min = osrm
                    r = results[result_idx]
                    # Preserve haversine metro_nearest_dist_m: OSRM table API
                    # only returns durations, not distances.
                    if dist_m is not None:
                        r["metro_nearest_dist_m"] = dist_m
                    r["metro_walk_min"]    = walk_min
                    r["metro_walk_routed"] = True
                    r["geo_score"] = _geo_score(
                        r.get("metro_nearest_dist_m"),
                        r.get("tram_nearest_dist_m"),
                        r.get("supermarket_nearest_dist_m"),
                        r.get("park_nearest_dist_m"),
                        r.get("university_nearest_dist_m"),
                    )
                    total_routed += 1
    print(f"  [geo] OSRM table: {total_routed}/{len(listings)} walk times routed", file=sys.stderr)

_GEO_FIELDS = (
    # OMI polygon fields
    "omi_zona", "omi_fascia", "omi_descr",
    "omi_loc_min", "omi_loc_max", "omi_loc_mid",
    "omi_compr_min", "omi_compr_max", "omi_compr_mid",
    "omi_source",
    # Overpass POI fields
    "metro_nearest_name", "metro_nearest_line", "metro_nearest_dist_m",
    "metro_walk_min", "metro_walk_routed", "tram_nearest_dist_m",
    "supermarket_nearest_dist_m", "park_nearest_dist_m",
    "university_nearest_dist_m", "geo_score",
)

_NULL_OMI = {
    "omi_zona": None, "omi_fascia": None, "omi_descr": None,
    "omi_loc_min": None, "omi_loc_max": None, "omi_loc_mid": None,
    "omi_compr_min": None, "omi_compr_max": None, "omi_compr_mid": None,
    "omi_source": None,
}

_NULL_OVERPASS = {
    "metro_nearest_name": None, "metro_nearest_line": None,
    "metro_nearest_dist_m": None, "metro_walk_min": None,
    "metro_walk_routed": False,
    "tram_nearest_dist_m": None, "supermarket_nearest_dist_m": None,
    "park_nearest_dist_m": None, "university_nearest_dist_m": None,
    "geo_score": None,
}


def _null_geo() -> dict:
    """Return all geo+OMI fields as None."""
    return {f: None for f in _GEO_FIELDS}


# ── omi_lookup import (optional — falls back gracefully if unavailable) ─────────

try:
    import omi_lookup as _omi_lookup
    _OMI_AVAILABLE = True
except Exception as _omi_err:
    _OMI_AVAILABLE = False
    print(f"  [geo] omi_lookup unavailable: {_omi_err}", file=sys.stderr)


def _overpass(query: str, timeout_http: int = 25, retries: int = 3) -> list:
    """
    Run an Overpass QL query and return the elements list.

    Picks the first endpoint via round-robin (_next_endpoint) so concurrent
    callers are spread evenly across servers from the very first attempt.
    On failure it rotates to the next endpoint with exponential back-off.
    """
    post_data = urllib.parse.urlencode({"data": query}).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent":   "ImmobiliareScorer/1.0 (rental enrichment)",
        "Accept":       "application/json",
    }
    n = len(_OVERPASS_ENDPOINTS)
    last_exc = None
    for attempt in range(retries):
        url = _OVERPASS_ENDPOINTS[attempt % n]
        req = urllib.request.Request(url, data=post_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_http) as resp:
                result = json.loads(resp.read())
            return result.get("elements", [])
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 1.5 * (attempt + 1)
                print(f"  [geo] {url} failed ({exc}), retry in {wait:.0f}s…",
                      file=sys.stderr)
                time.sleep(wait)
    raise last_exc


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Haversine distance in metres."""
    R  = 6_371_000
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a  = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return round(2 * R * math.asin(math.sqrt(a)))


def _nearest(elements: list, lat: float, lng: float) -> Optional[dict]:
    """Return the element closest to (lat, lng), or None."""
    best_dist, best_el = None, None
    for el in elements:
        el_lat = el.get("lat") or (el.get("center", {}) or {}).get("lat")
        el_lon = el.get("lon") or (el.get("center", {}) or {}).get("lon")
        if el_lat is None or el_lon is None:
            continue
        d = _dist_m(lat, lng, el_lat, el_lon)
        if best_dist is None or d < best_dist:
            best_dist, best_el = d, el
    if best_el is None:
        return None
    return {"element": best_el, "dist_m": best_dist}


def _metro_lines(tags: dict) -> str:
    """Extract Milan metro line(s) from OSM tags."""
    import re as _re
    desc = tags.get("description", "")
    if desc:
        found = _re.findall(r'[Mm]i(\d)', desc)
        if found:
            return ", ".join(sorted({f"M{n}" for n in found}))
    for key in ("line", "lines", "network:metro", "ref"):
        val = tags.get(key, "")
        if val:
            parts = [p.strip() for p in val.replace(";", ",").split(",") if p.strip()]
            lines = []
            for p in parts:
                p_upper = p.upper()
                if p_upper.startswith("M") and len(p_upper) <= 3:
                    lines.append(p_upper)
                elif p.isdigit():
                    lines.append(f"M{p}")
            if lines:
                return ", ".join(sorted(set(lines)))
    return ""


def _geo_score(metro_m, tram_m, super_m, park_m, uni_m) -> int:
    """Compute geo_score 0–100 from five distances (each may be None)."""
    score = 0
    if metro_m is not None:
        if   metro_m <= 300:  score += 40
        elif metro_m <= 500:  score += 35
        elif metro_m <= 800:  score += 28
        elif metro_m <= 1200: score += 18
        elif metro_m <= 2000: score += 8
    if tram_m is not None:
        if   tram_m <= 200:  score += 15
        elif tram_m <= 500:  score += 10
        elif tram_m <= 1000: score += 5
    if super_m is not None:
        if   super_m <= 300:  score += 20
        elif super_m <= 600:  score += 14
        elif super_m <= 1000: score += 8
    if park_m is not None:
        if   park_m <= 300:  score += 15
        elif park_m <= 800:  score += 10
        elif park_m <= 1500: score += 5
    if uni_m is not None:
        if   uni_m <= 500:  score += 10
        elif uni_m <= 1500: score += 6
    return min(100, score)


# ── Element type classifiers ───────────────────────────────────────────────────

def _is_metro(el: dict) -> bool:
    tags = el.get("tags", {})
    return (
        tags.get("station") == "subway"
        or tags.get("railway") == "subway_entrance"
        or (tags.get("railway") == "station" and tags.get("subway") == "yes")
    )


def _is_tram(el: dict) -> bool:
    return el.get("tags", {}).get("railway") == "tram_stop"


def _is_supermarket(el: dict) -> bool:
    return el.get("tags", {}).get("shop") == "supermarket"


def _is_park(el: dict, lat: float) -> bool:
    if el.get("tags", {}).get("leisure") != "park":
        return False
    bounds = el.get("bounds") or {}
    if bounds:
        dlat = abs(bounds.get("maxlat", 0) - bounds.get("minlat", 0))
        dlon = abs(bounds.get("maxlon", 0) - bounds.get("minlon", 0))
        area = dlat * 111_000 * dlon * 111_000 * math.cos(math.radians(lat))
        return area >= 5000
    return True   # no bounds data — include it


def _is_university(el: dict) -> bool:
    return el.get("tags", {}).get("amenity") in ("university", "college")


# ── Combined single-query enrichment ──────────────────────────────────────────

def _combined_query(lat: float, lng: float) -> str:
    """Build one Overpass QL query that fetches ALL POI types at once."""
    r = SEARCH_RADIUS
    return (
        f'[out:json][timeout:12];\n'
        f'(\n'
        # Metro stations / subway entrances
        f'  node["station"="subway"](around:{r},{lat},{lng});\n'
        f'  node["railway"="subway_entrance"](around:{r},{lat},{lng});\n'
        f'  node["railway"="station"]["subway"="yes"](around:{r},{lat},{lng});\n'
        # Tram stops
        f'  node["railway"="tram_stop"](around:{r},{lat},{lng});\n'
        # Supermarkets
        f'  node["shop"="supermarket"](around:{r},{lat},{lng});\n'
        f'  way["shop"="supermarket"](around:{r},{lat},{lng});\n'
        # Parks
        f'  way["leisure"="park"](around:{r},{lat},{lng});\n'
        # Universities / colleges
        f'  node["amenity"="university"](around:{r},{lat},{lng});\n'
        f'  way["amenity"="university"](around:{r},{lat},{lng});\n'
        f'  node["amenity"="college"](around:{r},{lat},{lng});\n'
        f'  way["amenity"="college"](around:{r},{lat},{lng});\n'
        f');\n'
        f'out center;'
    )


# ── Nominatim geocoding (fallback when coordinates are missing) ─────────────────

def _nominatim_geocode(address: str) -> Optional[tuple[float, float]]:
    """
    Geocode an address via Nominatim.  Returns (lat, lng) or None.
    Sleeps 1.0 s after each attempt to respect Nominatim's usage policy.

    Strategy:
      1. Structured query: extract street+number and query with city=Milano
         (avoids doubling the city name that happens with raw free-text search)
      2. Free-text fallback: strip neighbourhood tokens and try q= with city appended
    """
    import re as _re_geo

    def _do_request(params: dict) -> Optional[tuple[float, float]]:
        q = urllib.parse.urlencode({"format": "json", "limit": "1",
                                    "countrycodes": "it", **params})
        url = f"https://nominatim.openstreetmap.org/search?{q}"
        req = urllib.request.Request(url,
                                     headers={"User-Agent": "immobiliare-scorer/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            time.sleep(1.0)
            if results:
                return float(results[0]["lat"]), float(results[0]["lon"])
        except Exception as exc:
            print(f"  [geo] Nominatim error: {exc}", file=sys.stderr)
            time.sleep(1.0)
        return None

    if not address:
        return None

    # ── Attempt 1: structured query ──────────────────────────────────────────
    # Split on commas, keep only the first two meaningful parts (street + number),
    # strip Italian neighbourhood suffixes and city names that appear mid-address.
    raw_parts = [p.strip() for p in address.split(",")]
    # Drop obvious city/country tokens from the end
    city_tokens = {"milano", "italy", "italia", "milan"}
    street_parts = [p for p in raw_parts
                    if p.lower() not in city_tokens
                    and not _re_geo.match(r'^(s\.n\.c\.?|snc)$', p, _re_geo.I)]
    # First 1–2 parts are street [+ number]; the rest are neighbourhood/city
    street_str = ", ".join(street_parts[:2]).strip(", ")
    if street_str:
        result = _do_request({"street": street_str, "city": "Milano"})
        if result:
            return result

    # ── Attempt 2: free-text with just street, Milano, Italia ───────────────
    # Drop everything after the house number (neighbourhood tokens) to avoid
    # confusing Nominatim with micro-neighbourhood names it doesn't know.
    clean = ", ".join(street_parts[:2]).strip(", ")
    if clean and clean != street_str:
        result = _do_request({"q": f"{clean}, Milano, Italia"})
        if result:
            return result

    # ── Attempt 3: original free-text (legacy fallback) ──────────────────────
    # Strip duplicate "Milano"/"Italia" suffixes already present in the address.
    stripped = _re_geo.sub(
        r',?\s*(Milano|Italy|Italia)\s*$', '', address,
        flags=_re_geo.IGNORECASE,
    ).strip(", ")
    if stripped:
        result = _do_request({"q": f"{stripped}, Milano, Italia"})
        if result:
            return result

    print(f"  [geo] Nominatim: no result for '{address}'", file=sys.stderr)
    return None


# ── Public enrichment API ──────────────────────────────────────────────────────

def enrich(listing: dict, _skip_osrm: bool = False) -> dict:
    """
    Enrich one listing with OMI zone data and geospatial POI distances.

    Coordinate resolution (strict priority):
      1. listing['lat'] / listing['lng']  (API coordinates, most listings)
      2. listing['latitude'] / listing['longitude']  (alternate field names)
      3. Nominatim geocoding of listing['address']  (fallback)
      4. No coordinates → return nulls with omi_source='no_coordinates'

    Returns a dict of geo+OMI fields ONLY.
    Use `listing.update(enrich(listing))` to merge back.
    """
    lid = listing.get("id", "?")

    # ── Step 1: resolve coordinates ───────────────────────────────────────────
    lat = lng = None
    geocoded = False

    # Priority 1+2: stored coordinates
    for lat_key, lng_key in (("lat", "lng"), ("latitude", "longitude")):
        raw_lat = listing.get(lat_key)
        raw_lng = listing.get(lng_key)
        if raw_lat is not None and raw_lng is not None:
            try:
                _lat, _lng = float(raw_lat), float(raw_lng)
                if _lat != 0.0 and _lng != 0.0:
                    lat, lng = _lat, _lng
                    break
            except (TypeError, ValueError):
                pass

    # Priority 3: Nominatim geocoding
    if lat is None:
        address = listing.get("address", "").strip()
        if address:
            result = _nominatim_geocode(address)
            if result:
                lat, lng = result
                geocoded = True
                # Persist resolved coordinates so the cache never re-geocodes this listing
                listing["latitude"]  = lat
                listing["longitude"] = lng

    # Priority 4: no coordinates at all
    if lat is None:
        print(f"  [geo] no coordinates for {lid} (address={listing.get('address')})",
              file=sys.stderr)
        return {**_NULL_OMI, "omi_source": "no_coordinates", **_NULL_OVERPASS}

    # ── Step 2: OMI polygon lookup ────────────────────────────────────────────
    omi_fields = dict(_NULL_OMI)
    if _OMI_AVAILABLE:
        try:
            zone, src = _omi_lookup.lookup(lat, lng)
            if zone:
                prefix = "nominatim+" if geocoded else ""
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
                    "omi_source":    prefix + src,
                }
            else:
                omi_fields["omi_source"] = "failed"
        except Exception as exc:
            print(f"  [geo] omi_lookup error for {lid}: {exc}", file=sys.stderr)
            omi_fields["omi_source"] = "failed"

    # ── Step 3: POI distances ────────────────────────────────────────────────
    _metro_routed = False   # True only if OSRM actually returned a route
    pois = load_local_pois()
    if pois:
        # Fast path: in-memory haversine for all POIs, then one OSRM call for metro
        metro_m, metro_name, metro_line, metro_lat, metro_lon = _nearest_metro_local(pois["metro"], lat, lng)
        tram_m  = _nearest_dist(pois["tram"],        lat, lng)
        super_m = _nearest_dist(pois["supermarket"], lat, lng)
        park_m  = _nearest_dist(pois["park"],        lat, lng)
        uni_m   = _nearest_dist(pois["university"],  lat, lng)
        if metro_lat is not None and not _skip_osrm:
            osrm = _osrm_walk(lat, lng, metro_lat, metro_lon)
            if osrm:
                metro_m, _metro_walk_min = osrm
                _metro_routed = True
            else:
                _metro_walk_min = round(metro_m / 60) if metro_m is not None else None
        else:
            _metro_walk_min = round(metro_m / 60) if metro_m is not None else None
    else:
        # Fallback: per-listing Overpass query
        try:
            elements = _overpass(_combined_query(lat, lng), timeout_http=15, retries=4)
        except Exception as exc:
            print(f"  [geo] Overpass failed for {lid}: {exc}", file=sys.stderr)
            return {**omi_fields, **_NULL_OVERPASS}

        metro_els = [e for e in elements if _is_metro(e)]
        tram_els  = [e for e in elements if _is_tram(e)]
        super_els = [e for e in elements if _is_supermarket(e)]
        park_els  = [e for e in elements if _is_park(e, lat)]
        uni_els   = [e for e in elements if _is_university(e)]

        metro_name, metro_line, metro_m, _metro_walk_min = None, None, None, None
        hit = _nearest(metro_els, lat, lng)
        if hit:
            metro_m  = hit["dist_m"]
            tags     = hit["element"].get("tags", {})
            metro_name = tags.get("name") or tags.get("name:it") or None
            metro_line = _metro_lines(tags) or None
            if not metro_line:
                station_els = [e for e in metro_els if e.get("tags", {}).get("station") == "subway"]
                hit2 = _nearest(station_els, lat, lng)
                if hit2:
                    metro_line = _metro_lines(hit2["element"].get("tags", {})) or None
                    if not metro_name:
                        t2 = hit2["element"].get("tags", {})
                        metro_name = t2.get("name") or t2.get("name:it") or None
            el_lat = hit["element"].get("lat") or (hit["element"].get("center") or {}).get("lat")
            el_lon = hit["element"].get("lon") or (hit["element"].get("center") or {}).get("lon")
            if el_lat and el_lon and not _skip_osrm:
                osrm = _osrm_walk(lat, lng, el_lat, el_lon)
                if osrm:
                    metro_m, _metro_walk_min = osrm
                    _metro_routed = True
                else:
                    _metro_walk_min = round(metro_m / 60) if metro_m is not None else None
            else:
                _metro_walk_min = round(metro_m / 60) if metro_m is not None else None

        tram_hit  = _nearest(tram_els,  lat, lng)
        super_hit = _nearest(super_els, lat, lng)
        park_hit  = _nearest(park_els,  lat, lng)
        uni_hit   = _nearest(uni_els,   lat, lng)

        tram_m  = tram_hit["dist_m"]  if tram_hit  else None
        super_m = super_hit["dist_m"] if super_hit else None
        park_m  = park_hit["dist_m"]  if park_hit  else None
        uni_m   = uni_hit["dist_m"]   if uni_hit   else None

    return {
        **omi_fields,
        # Always include resolved coordinates so they are stored in the enrichment
        # cache and merged back into the listing record. Without this, Nominatim-
        # geocoded coordinates are set on the listing object in-place but never
        # persisted to the cache, causing them to be lost on the next run.
        "latitude":                   lat,
        "longitude":                  lng,
        "metro_nearest_name":         metro_name,
        "metro_nearest_line":         metro_line,
        "metro_nearest_dist_m":       metro_m,
        "metro_walk_min":             _metro_walk_min,
        "tram_nearest_dist_m":        tram_m,
        "supermarket_nearest_dist_m": super_m,
        "park_nearest_dist_m":        park_m,
        "university_nearest_dist_m":  uni_m,
        "geo_score":                  _geo_score(metro_m, tram_m, super_m, park_m, uni_m),
        "metro_walk_routed":          _metro_routed,
    }


def enrich_batch(listings: list, max_workers: int = 8) -> list:
    """
    Enrich multiple listings in parallel.
    Returns a list of geo dicts in the same order as the input.
    Per-listing failures return an empty dict rather than crashing the whole batch.

    Walk time estimation: haversine distance ÷ 60 m/min (3.6 km/h) — a deliberate
    underestimate of walking speed that implicitly accounts for urban grid detours.
    The public OSRM table API (/table/v1/foot/) is not available on router.project-osrm.org
    (foot profile is route-only there), so we skip batched OSRM routing in this path.
    Single enrich() calls (small inline batches) still use /route/v1/foot/ individually.

    Logs a coordinate/OMI resolution summary after completion:
      [omi] N polygon · N centroid · N nominatim+polygon · N no_coordinates
    """
    if not listings:
        return []
    from concurrent.futures import Future
    futures: list[Future] = []
    # _skip_osrm=True: thread pool handles OMI + haversine only (fast, all in-memory).
    # Individual OSRM route calls are skipped here because the table API is unavailable
    # and per-listing route calls would serialise on the semaphore, negating the speedup.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(enrich, l, True) for l in listings]
        results = []
        for fut in futures:
            try:
                results.append(fut.result(timeout=60))
            except Exception as exc:
                print(f"  [enrich] listing failed: {type(exc).__name__} — skipping", file=sys.stderr)
                results.append({})

    # Resolution summary
    from collections import Counter
    src_counts: Counter = Counter(r.get("omi_source") or "unknown" for r in results)
    parts = []
    for src in ("polygon", "centroid", "nominatim+polygon", "nominatim+centroid",
                "no_coordinates", "failed", "unknown"):
        n = src_counts.pop(src, 0)
        if n:
            parts.append(f"{n} {src}")
    for src, n in sorted(src_counts.items()):   # catch any unexpected values
        parts.append(f"{n} {src}")
    print(f"  [omi] {' · '.join(parts) or 'no results'}", file=sys.stderr)

    return results


# ── Self-test ──────────────────────────────────────────────────────────────────

def _run_test():
    """Run geo enrichment on three known Milan coordinates and print results."""
    TEST_COORDS = [
        {"id": "test-duomo",          "name": "Duomo",          "latitude": 45.4641, "longitude": 9.1919},
        {"id": "test-niguarda",       "name": "Niguarda",       "latitude": 45.5199, "longitude": 9.1936},
        {"id": "test-quarto-oggiaro", "name": "Quarto Oggiaro", "latitude": 45.5061, "longitude": 9.1385},
    ]

    print("enrich_geo self-test — three Milan coordinates\n")
    t0 = time.time()
    results = enrich_batch(TEST_COORDS, max_workers=3)
    elapsed = time.time() - t0
    print(f"enrich_batch completed in {elapsed:.1f}s  ({elapsed/len(TEST_COORDS):.2f}s per listing)\n")

    for coords, geo in zip(TEST_COORDS, results):
        print(f"▶ {coords['name']} ({coords['latitude']}, {coords['longitude']})")
        for f in _GEO_FIELDS:
            print(f"  {f:<35s} {geo.get(f)}")
        print()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Run self-test on three Milan coordinates")
    args = ap.parse_args()
    if args.test:
        _run_test()
    else:
        ap.print_help()
