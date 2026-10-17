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


def lookup(address: str) -> tuple[float, float, str] | None:
    """(lat, lon, precision) — precision "civico" (exact house number),
    "near" (nearest number on the same side) or "street" (centroid of a
    short street). None when the street isn't known or too long to guess."""
    idx = _load()
    key, number = split_address(address)
    if not key:
        return None
    if key.split(" ", 1)[0] not in _TYPES:
        key = "via " + key
    st = idx["streets"].get(key) or idx["streets"].get(idx["alias"].get(key, ""))
    if not st:
        st = idx["streets"].get(_fuzzy(key) or "")
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
