"""Milan street addresses → coordinates, offline.

Built from the Comune di Milano house-number dataset ("Numeri civici con
coordinate geografiche", ds634, CC BY — dati.comune.milano.it): ~65k house
numbers with WGS84 coordinates. Turns "Via Carlo Perini, 18, Quarto
Oggiaro, Milano" into coordinates instantly, with no geocoding service and
no rate limit; enrich_geo falls back to Photon / Nominatim only for what
this can't place.

Build or refresh the index (python3 tools/build_civici_index.py) after
downloading the dataset's CSV zip into data_sources/milano_civici/.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
INDEX_PATH = BASE / "data_sources" / "milano_civici" / "civici_index.json"

# A street with no house number gets its centroid only when it is short —
# Viale Monza's centroid could be 2 km from the flat.
MAX_STREET_EXTENT_M = 700
# No exact house number: the nearest one on the same side, if this close.
MAX_NUMBER_GAP = 20

_ABBREV = {
    "v": "via", "v.": "via", "v.le": "viale", "vle": "viale", "c.so": "corso", "cso": "corso",
    "p.za": "piazza", "p.zza": "piazza", "pza": "piazza", "p.le": "piazzale", "ple": "piazzale",
    "l.go": "largo", "lgo": "largo", "vic.": "vicolo", "gall.": "galleria",
}
_ARTICLES = {"di", "del", "della", "dei", "degli", "delle", "dello", "de", "d", "da", "al", "alla",
             "e", "privata", "privato"}
_TYPES = {"via", "viale", "corso", "piazza", "piazzale", "largo", "vicolo", "galleria", "alzaia",
          "ripa", "foro", "bastioni", "piazzetta", "passaggio", "strada",
          "cavalcavia", "sottopasso", "rotonda", "giardino", "parco", "sito", "salita", "molo"}


def norm_street(s: str) -> str:
    """Comparable street key: lower case, no accents or punctuation,
    abbreviations expanded, articles dropped ("Piazza del Duomo" →
    "piazza duomo")."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(([^)]*)\)", r" \1 ", s)
    words = []
    for w in s.replace("'", " ").replace("’", " ").split():
        w = _ABBREV.get(w, w)
        w = re.sub(r"[^a-z0-9]", "", w)
        if w and w not in _ARTICLES:
            words.append(w)
    return " ".join(words)


def split_address(address: str) -> tuple[str, str | None]:
    """("via carlo perini", "18") from "Via Carlo Perini, 18, Quarto
    Oggiaro, Milano" — or a number written inside the first part
    ("Via Torino 23")."""
    parts = [p.strip() for p in (address or "").split(",")
             if p.strip() and p.strip().lower() not in ("milano", "milan", "mi", "italia", "italy")]
    if not parts:
        return "", None
    street, number = parts[0], None
    m = re.match(r"^(.*?)[\s,]+(\d{1,4}\s*[/]?\s*[a-zA-Z]?)$", street)
    if m and not re.search(r"\d", m.group(1)):
        street, number = m.group(1), m.group(2)
    elif len(parts) > 1 and re.match(r"^\d{1,4}\s*[/]?\s*[a-zA-Z]?$", parts[1]):
        number = parts[1]
    if number:
        number = re.sub(r"[\s/]", "", number).upper()
    return norm_street(street), number


_index: dict | None = None


def _load() -> dict:
    global _index
    if _index is None:
        try:
            _index = json.loads(INDEX_PATH.read_text())
        except (OSError, ValueError):
            _index = {"streets": {}, "alias": {}}
    return _index


_tokens: list | None = None


def _street_tokens() -> list:
    """[(type, {name tokens}, key)] for the tolerant match."""
    global _tokens
    if _tokens is None:
        _tokens = []
        for key in _load()["streets"]:
            t, _, name = key.partition(" ")
            _tokens.append((t, set(name.split()), key))
    return _tokens


def _fuzzy(key: str) -> str | None:
    """The one street whose name is contained in the query's (or the other
    way round): "via angelo poliziano" → "via poliziano", "via fulvio testi"
    → "viale fulvio testi". Same street type preferred; ties → no answer."""
    t, _, name = key.partition(" ")
    q = set(name.split())
    if not q or not any(len(w) >= 4 for w in q):
        return None
    for same_type in (True, False):
        cands = [(len(n), k) for st, n, k in _street_tokens()
                 if (st == t) == same_type and n and any(len(w) >= 4 for w in n)
                 and (n <= q or q <= n)]
        if cands:
            cands.sort(reverse=True)
            if len(cands) == 1 or cands[0][0] > cands[1][0]:
                return cands[0][1]
            return None
    return None


def available() -> bool:
    return bool(_load()["streets"])


def street_key(address: str) -> str | None:
    """The index's key for the street of `address` ("via ettore ponti"), or
    None when it isn't known."""
    key = split_address(address)[0]
    if not key:
        return None
    if key.split(" ", 1)[0] not in _TYPES:
        key = "via " + key
    streets = _load()["streets"]
    for k in (key, _load()["alias"].get(key, "")):
        if k in streets:
            return k
    return _fuzzy(key)


def lookup(address: str) -> tuple[float, float, str] | None:
    """(lat, lon, precision) — precision "civico" (exact house number),
    "near" (nearest number on the same side) or "street" (centroid of a
    short street). None when the street isn't known or too long to guess."""
    idx = _load()
    number = split_address(address)[1]
    st = idx["streets"].get(street_key(address) or "")
    if not st:
        return None
    nums = st["n"]
    if number:
        if number in nums:
            return (*nums[number], "civico")
        digits = re.match(r"\d+", number)
        if digits and nums:
            n = int(digits.group(0))
            best = None
            for k, v in nums.items():
                d = re.match(r"\d+", k)
                if not d:
                    continue
                kn = int(d.group(0))
                if kn % 2 != n % 2 or abs(kn - n) > MAX_NUMBER_GAP:
                    continue
                if best is None or abs(kn - n) < best[0]:
                    best = (abs(kn - n), v)
            if best:
                return (*best[1], "near")
    if st.get("ext", 1e9) <= MAX_STREET_EXTENT_M:
        return (*st["c"], "street")
    return None


# ── Pins far from their neighbourhood ────────────────────────────────────────
# 2026-10-24: 178 portal listings sat over 2 km from the other listings of
# the neighbourhood the site names (Bovisa in Cordusio, Sulmona 15 km out of
# town) — coordinates a geocoder guessed before this index existed, kept in
# enriched_cache. The portals' neighbourhood names are reliable; Subito's
# come from the pin itself, so Subito and Casa.it rows are left alone.
FAR_PIN_KM = 2.0
# With no street-index answer, a pin only this far off moves to the centre:
# at 2–3 km it may be right in a wide neighbourhood (Via Lodovico il Moro).
FAR_PIN_CENTRE_KM = 3.0
GEO_DERIVED = (
    "metro_nearest_name", "metro_nearest_line", "metro_nearest_dist_m", "metro_walk_min",
    "metro_walk_routed", "metro_walk_grade", "omi_zona", "omi_fascia", "omi_descr",
    "omi_loc_mid", "omi_loc_min", "omi_loc_max", "omi_compr_mid", "omi_compr_min",
    "omi_compr_max", "omi_source", "omi_fallback", "geo_score", "park_nearest_dist_m",
    "supermarket_nearest_dist_m", "university_nearest_dist_m", "tram_nearest_dist_m",
)


def _km(a: tuple, b: tuple) -> float:
    import math
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 78.0)


def zone_centres(rows: list, min_n: int = 5) -> dict:
    """Median pin of each neighbourhood's live portal listings."""
    import statistics
    pts: dict = {}
    for r in rows:
        if r.get("is_stale") or r.get("source") in ("subito", "casait") or r.get("latitude") is None:
            continue
        if r.get("neighbourhood"):
            pts.setdefault(r["neighbourhood"], []).append((r["latitude"], r["longitude"]))
    return {z: (statistics.median(p[0] for p in v), statistics.median(p[1] for p in v))
            for z, v in pts.items() if len(v) >= min_n}


def repin_far(rows: list, max_km: float = FAR_PIN_KM, centre_km: float = FAR_PIN_CENTRE_KM) -> list:
    """Re-place portal listings pinned more than max_km from their
    neighbourhood's centre: the street index when it lands within max_km of
    that centre (geo_precision None for a house number, "street" otherwise),
    else, beyond centre_km, the centre itself (geo_precision "zone" — no
    label, like Subito's).
    Clears the fields derived from the old pin so enrichment recomputes
    them; returns the "source:id" keys to drop from enriched_cache."""
    centres = zone_centres(rows)
    keys = []
    for r in rows:
        if r.get("source") in ("subito", "casait") or r.get("latitude") is None:
            continue
        c = centres.get(r.get("neighbourhood") or "")
        off = _km((r["latitude"], r["longitude"]), c) if c else 0
        if off <= max_km:
            continue
        hit = lookup(r.get("address") or "") if available() else None
        if hit and _km(hit[:2], c) <= max_km:
            r["latitude"], r["longitude"] = hit[0], hit[1]
            r["geo_precision"] = None if hit[2] in ("civico", "near") else "street"
        elif off > centre_km:
            r["latitude"], r["longitude"] = c
            r["geo_precision"] = "zone"
        else:
            continue
        r["coords_from"] = "repinned"
        for k in GEO_DERIVED:
            r[k] = None
        keys.append(f"{r.get('source', 'immobiliare')}:{r['id']}")
    return keys
