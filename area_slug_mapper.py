"""
Area slug mapper — translates internal area names (e.g. "Cadore") into the
real Immobiliare.it macro-area slug ("porta-romana-cadore-montenero").

Several internal areas live under one Immobiliare macro-area. Without this
mapping the scanner either:
  • fetches the raw slug (e.g. /milano/cadore/) and gets 0 results, or
  • fetches three near-identical pages back to back, wasting requests and
    triggering anti-scraping.

Single source of truth: area_slug_mapping.json at the repo root. Shared by
fetch_rentals.py and fetch_listings.py so both speak the same vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
_MAPPING_PATH = _BASE_DIR / "area_slug_mapping.json"

# Cache: { city: { internal_slug: immobiliare_slug } }
_CACHE: dict | None = None
_CACHE_MTIME: float = 0.0


def _load_raw() -> dict:
    """Load (and lazily reload on file change) the mapping JSON."""
    global _CACHE, _CACHE_MTIME
    if not _MAPPING_PATH.exists():
        _CACHE = {}
        return _CACHE
    try:
        mtime = _MAPPING_PATH.stat().st_mtime
        if _CACHE is None or mtime > _CACHE_MTIME:
            raw = json.loads(_MAPPING_PATH.read_text())
            # Strip top-level "_doc"/"_comment" keys + nested "_comment"
            # placeholders so callers see only real city → mapping entries.
            cleaned: dict = {}
            for k, v in raw.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict):
                    cleaned[k] = {ik: iv for ik, iv in v.items() if not ik.startswith("_")}
            _CACHE = cleaned
            _CACHE_MTIME = mtime
    except Exception as exc:
        print(f"  [slug-map] warning: could not load {_MAPPING_PATH.name}: {exc}")
        _CACHE = _CACHE or {}
    return _CACHE


def get_slug_mapping(city: str = "milano") -> dict:
    """Return the {internal_slug: immobiliare_slug} dict for one city, or {} if absent."""
    return _load_raw().get(city, {})


def resolve_immobiliare_slug(area_slug: str, city: str = "milano") -> str:
    """Translate one internal area slug → Immobiliare.it slug. Passes through if unknown."""
    return get_slug_mapping(city).get(area_slug, area_slug)


def group_by_immobiliare_slug(area_slugs: list, city: str = "milano") -> dict:
    """
    Group internal area slugs by their Immobiliare macro-area.

    Returns {immobiliare_slug: [internal_slugs_that_map_to_it]} preserving
    the input order of internal slugs (Python 3.7+ dict ordering).

    >>> group_by_immobiliare_slug(["cadore", "porta-romana", "navigli"], "milano")
    {'porta-romana-cadore-montenero': ['cadore', 'porta-romana'], 'navigli': ['navigli']}
    """
    mapping = get_slug_mapping(city)
    grouped: dict = {}
    for slug in area_slugs:
        target = mapping.get(slug, slug)
        grouped.setdefault(target, []).append(slug)
    return grouped


def macro_areas_for_city(city: str = "milano") -> list:
    """
    Return the sorted, deduplicated list of Immobiliare macro-area slugs
    for one city — i.e. the set of values in get_slug_mapping(city), which
    is what the Settings UI displays as scannable cards.

    Empty list when the mapping is empty (Roma / Napoli / La Maddalena
    until verified).
    """
    mp = get_slug_mapping(city)
    return sorted({v for v in mp.values()})


_PRETTIFY_OVERRIDES = {
    # Explicit display names for the 32 verified Milan macro-area slugs.
    # Keeps the UI label clean (proper accents, correct multi-word grouping,
    # CityLife capitalisation) instead of relying on heuristics that
    # break "Chiesa Rossa" into "Chiesa, Rossa" etc.
    "abbiategrasso-chiesa-rossa":           "Abbiategrasso, Chiesa Rossa",
    "affori-bovisa":                        "Affori, Bovisa",
    "arco-della-pace-arena-pagano":         "Arco della Pace, Arena, Pagano",
    "bande-nere-inganni":                   "Bande Nere, Inganni",
    "bicocca-niguarda":                     "Bicocca, Niguarda",
    "bisceglie-baggio-olmi":                "Bisceglie, Baggio, Olmi",
    "cenisio-sarpi-isola":                  "Cenisio, Sarpi, Isola",
    "centrale-repubblica":                  "Centrale, Repubblica",
    "centro":                               "Centro",
    "cimiano-crescenzago-adriano":          "Cimiano, Crescenzago, Adriano",
    "citta-studi-susa":                     "Città Studi, Susa",
    "citta-studi":                          "Città Studi",
    "corvetto-rogoredo":                    "Corvetto, Rogoredo",
    "famagosta-barona":                     "Famagosta, Barona",
    "fiera-sempione-city-life-portello":    "Fiera, Sempione, CityLife, Portello",
    "forlanini":                            "Forlanini",
    "garibaldi-moscova-porta-nuova":        "Garibaldi, Moscova, Porta Nuova",
    "genova-ticinese":                      "Genova, Ticinese",
    "maggiolina-istria":                    "Maggiolina, Istria",
    "napoli-soderini":                      "Napoli, Soderini",
    "navigli":                              "Navigli",
    "pasteur-rovereto":                     "Pasteur, Rovereto",
    "ponte-lambro-santa-giulia":            "Ponte Lambro, Santa Giulia",
    "porta-romana-cadore-montenero":        "Porta Romana, Cadore, Montenero",
    "porta-venezia-indipendenza":           "Porta Venezia, Indipendenza",
    "porta-vittoria-lodi":                  "Porta Vittoria, Lodi",
    "precotto-turro":                       "Precotto, Turro",
    "quadronno-palestro-guastalla":         "Quadronno, Palestro, Guastalla",
    "ripamonti-vigentino":                  "Ripamonti, Vigentino",
    "san-siro-trenno":                      "San Siro, Trenno",
    "solari-washington":                    "Solari, Washington",
    "udine-lambrate":                       "Udine, Lambrate",
    "uptown-cascina-merlata-viale-certosa": "Uptown, Cascina Merlata, Viale Certosa",
    # Single-area slugs commonly used standalone.
    "bocconi":          "Bocconi",
    "de-angeli":        "De Angeli",
    "giambellino":      "Giambellino",
    "city-life":        "CityLife",
    "citylife":         "CityLife",
    "qt8":              "QT8",
}


def prettify_slug(slug: str) -> str:
    """
    Convert a slug like 'porta-romana-cadore-montenero' into a comma-
    separated display string 'Porta Romana, Cadore, Montenero'.

    Heuristic: split on hyphens, title-case each token, then group two-
    word names by re-joining adjacent tokens until we hit one of the
    known macro-area "seam" markers. Since Immobiliare slugs are flat
    hyphen-joined, the seam markers are inferred from the macro-area
    list — we just title-case + comma-join individual tokens, which
    yields imperfect-but-readable output for cases the override table
    doesn't cover.
    """
    if not slug:
        return ""
    if slug in _PRETTIFY_OVERRIDES:
        return _PRETTIFY_OVERRIDES[slug]
    # Default: title-case each hyphen-separated token, then join with
    # comma+space. "porta-romana-cadore-montenero" looks worse this way
    # than "Porta Romana, Cadore, Montenero", so we apply a tiny
    # heuristic: two adjacent capitalised tokens with no comma between
    # them get glued with a space (e.g. "Porta Romana"). The marker
    # we lean on is a small set of common Italian street/area prefixes.
    PREFIX_GLUE = {"porta", "viale", "via", "piazza", "piazzale", "largo",
                   "corso", "san", "sant", "santa", "santo", "monte",
                   "ponte", "cascina", "fondazione", "scalo", "casa",
                   "tre", "argonne", "borgogna", "buenos", "cinque",
                   "città", "citta", "le", "alzaia", "ascanio", "bande",
                   "ca", "città", "garibaldi", "isola", "lodi", "porta-romana",
                   "quadrilatero", "quartiere", "quinto", "quarto"}
    tokens = slug.split("-")
    parts: list = []
    i = 0
    while i < len(tokens):
        t = tokens[i].lower()
        title = tokens[i].capitalize()
        # If this token is a glue-prefix and there's another token
        # after it, fuse them as one display word.
        if t in PREFIX_GLUE and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            parts.append(f"{title} {nxt.capitalize()}")
            i += 2
        else:
            parts.append(title)
            i += 1
    return ", ".join(parts)


def normalise_active_areas(active_areas: list, city: str = "milano") -> list:
    """
    Return macro slugs ready to scan, accepting either the new format
    (list of macro slugs already) or legacy format (internal zone names
    that need translation through get_slug_mapping).

    Both formats coexist during the Settings-redesign transition: the
    new UI writes macro slugs directly; existing area_settings_*.json
    files may still contain internal zone names from the dual-list era.
    """
    mp = get_slug_mapping(city)
    macro_values = set(mp.values())
    result: set = set()
    for entry in active_areas or []:
        # Tolerate dict entries from the legacy {areas: [{name, active}]}
        # format — pull the name field, skip inactive items.
        if isinstance(entry, dict):
            if not entry.get("active"):
                continue
            name = entry.get("name") or entry.get("id") or ""
        else:
            name = str(entry or "")
        if not name:
            continue
        # Normalise to slug form so user-typed display names also
        # resolve. Inline import to avoid a circular dep with
        # fetch_rentals.to_url_slug at module load.
        import re as _re
        import unicodedata as _ud
        s = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        s = _re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
        if s in macro_values:
            result.add(s)
        else:
            result.add(mp.get(s, s))
    return sorted(result)


def _macro_slugs(city: str) -> list:
    """The city's `_macro_areas_immobiliare` list (stripped by _load_raw)."""
    try:
        return json.loads(_MAPPING_PATH.read_text()).get(city, {}).get(
            "_macro_areas_immobiliare", [])
    except Exception:
        return []


def verify_slug_mapping(city: str, internal_slugs: list) -> list:
    """
    Compare the configured area list to the mapping. Returns the list of
    internal slugs that have no explicit mapping (they will be used as-is
    and may or may not work). Also prints a one-line warning when non-empty.
    """
    mapping = get_slug_mapping(city)
    # Callers pass slugs already resolved to Immobiliare macro slugs
    # (e.g. "affori-bovisa"), which are mapping VALUES / the
    # _macro_areas_immobiliare list, not keys — checking keys only made
    # every scan warn about all 17 Milan areas.
    known = set(mapping) | set(mapping.values()) | set(_macro_slugs(city))
    missing = [s for s in internal_slugs if s and s not in known]
    if missing:
        sample = ", ".join(missing[:10])
        more = "..." if len(missing) > 10 else ""
        print(f"  [{city}] ⚠ {len(missing)} area(s) have no slug mapping — "
              f"will try raw slug (may 0-result): {sample}{more}")
        print(f"  [{city}]   Update area_slug_mapping.json to silence this.")
    return missing


# ── HTTP smoke test ──────────────────────────────────────────────────────
# Fetch each unique Immobiliare slug's first page once and count listings.
# Catches dead mappings (renamed/removed slugs) before they cause silent
# data loss in a real scan. One HTTP request per unique slug, 1.5 s delay
# between calls — finishes in under a minute for Milan.

def _count_listings_in_html(html: str) -> int:
    """Best-effort listing-count extractor.

    Strategy:
      1. The page header text "1.234 annunci" — most reliable when present.
      2. Fallback: count "in-listing-card-title" occurrences in the HTML.
      3. Final fallback: count "data-cy=\"listing-item-card\"" markers.
    The first hit wins; the others are belt-and-suspenders against
    cosmetic markup changes.
    """
    import re as _re
    m = _re.search(r'(\d[\d\.\,]*)\s*(?:annunci|risultati)\b', html, _re.I)
    if m:
        digits = m.group(1).replace('.', '').replace(',', '')
        if digits.isdigit():
            return int(digits)
    n = len(_re.findall(r'in-listing-card-title', html))
    if n:
        return n
    n = len(_re.findall(r'data-cy="listing-item-card"', html))
    return n


def verify_mapping(city: str = "milano", delay: float = 1.5,
                   listing_type: str = "affitto") -> dict:
    """
    Smoke-test every unique Immobiliare macro-area slug for one city.

    Returns {slug: count}.
      count > 0   → working, the page returned listings
      count == 0  → page loaded but no listings — slug probably renamed
      count == -1 → HTTP error (404, timeout, blocked)

    IMPORTANT: Immobiliare actively blocks unauthenticated requests with
    HTTP 403 (Cloudflare anti-bot). Plain urllib will be blocked for every
    URL — the function detects this on the first hit and bails out with
    instructions for the real verification path
    (`python3 fetch_rentals.py --pages 1`), which uses the same nodriver
    browser the real scanner uses and bypasses the bot defence.

    listing_type: 'affitto' (rentals) or 'vendita' (sales).
    """
    import urllib.request
    import urllib.error
    import time

    mapping = get_slug_mapping(city)
    if not mapping:
        print(f"  [{city}] no mapping configured — nothing to verify")
        return {}

    # Build slug → [internal areas pointing at it] so the report explains
    # the blast radius of a broken slug (1 internal area vs 11).
    inverse: dict = {}
    for internal, target in mapping.items():
        inverse.setdefault(target, []).append(internal)
    unique_slugs = sorted(inverse.keys())

    base_path = "affitto-case" if listing_type == "affitto" else "vendita-case"
    print(f"  [{city}] verifying {len(unique_slugs)} unique {base_path} slug(s) "
          f"({sum(len(v) for v in inverse.values())} internal areas covered) "
          f"@ {delay}s delay")
    print()

    results: dict = {}
    blocked_streak = 0   # consecutive 403/anti-bot hits
    for slug in unique_slugs:
        url = f"https://www.immobiliare.it/{base_path}/{city}/{slug}/"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/125.0.0.0 Safari/537.36"),
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            count = _count_listings_in_html(html)
            results[slug] = count
            mark = "✓" if count > 0 else "✗"
            internals = inverse[slug]
            tail = (f"  ← {', '.join(internals)}" if len(internals) > 1
                    else "")
            print(f"  {mark} {slug}: {count}{tail}")
            blocked_streak = 0
        except urllib.error.HTTPError as exc:
            results[slug] = -1
            print(f"  ✗ {slug}: HTTP {exc.code}  ← {', '.join(inverse[slug])}")
            if exc.code == 403:
                blocked_streak += 1
                # Fail fast: three consecutive 403s means Cloudflare has
                # decided to block this IP. Hammering 28 more times won't
                # produce useful data — abort with the real-path hint.
                if blocked_streak >= 3:
                    print()
                    print(f"  ⛔ {blocked_streak} consecutive HTTP 403s — "
                          f"Immobiliare is blocking plain HTTP requests.")
                    print(f"     This verifier can't get past the anti-bot "
                          f"defence; only nodriver (real browser) can.")
                    print()
                    print(f"     Run the real scanner instead — it uses the "
                          f"same headless Edge that the scheduler uses, so "
                          f"its 'no results for slug X' output is the "
                          f"authoritative verification:")
                    print()
                    print(f"       python3 fetch_rentals.py "
                          f"--city {city} --pages 1")
                    print()
                    print(f"     Watch the log for 'No results for area X' — "
                          f"those are the broken slugs. Everything else is "
                          f"working.")
                    return results
        except Exception as exc:
            results[slug] = -1
            print(f"  ✗ {slug}: {type(exc).__name__}: {exc}  "
                  f"← {', '.join(inverse[slug])}")
        time.sleep(delay)

    working = sum(1 for c in results.values() if c > 0)
    broken  = [s for s, c in results.items() if c <= 0]
    print()
    print(f"  Summary: {working}/{len(unique_slugs)} slugs returning listings")
    if broken:
        print(f"  ⚠ {len(broken)} slug(s) need fixing in area_slug_mapping.json:")
        for s in broken:
            print(f"      {s}  (blocks {len(inverse[s])} internal area(s): "
                  f"{', '.join(inverse[s][:5])}"
                  f"{'…' if len(inverse[s]) > 5 else ''})")
    return results


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a]
    city = args[0] if args else "milano"
    listing_type = args[1] if len(args) > 1 else "affitto"
    verify_mapping(city, listing_type=listing_type)
