#!/usr/bin/env python3
"""
scrape_idealista_areas.py
─────────────────────────
Scrapes Idealista's full neighbourhood hierarchy for Milano:

    Milano
    └── Zone (e.g. "Abbiategrasso - Chiesa Rossa")
        └── Sub-zone (e.g. "Tibaldi - Pezzotti", "Gratosoglio", …)

Strategy
--------
Idealista exposes the sub-zone list in a dropdown that appears on any zone-level
search page when the user selects "only this zone".  The dropdown is rendered
server-side inside an <ul> element, so we can read it after navigating to the
zone page — no click needed.

For each top-level zone in idealista_area_settings.json we:
  1. Navigate to  /affitto-case/milano-milano/<slug>/
  2. Wait for the area-selector list to render
  3. Extract every <a> in the list → (name, url, count)
  4. Derive the slug from the href

Output → idealista_neighbourhood_map.json
  {
    "source": "idealista.it",
    "scraped_at": "2026-05-01T…",
    "zones": [
      {
        "name": "Abbiategrasso - Chiesa Rossa",
        "slug": "abbiategrasso-chiesa-rossa",
        "url":  "https://www.idealista.it/affitto-case/milano-milano/abbiategrasso-chiesa-rossa/",
        "subzones": [
          {"name": "Tibaldi - Pezzotti", "slug": "tibaldi-pezzotti",
           "url": "https://www.idealista.it/affitto-case/...", "listings": 1},
          …
        ]
      },
      …
    ]
  }

Usage
-----
  python3 scrape_idealista_areas.py           # all active zones
  python3 scrape_idealista_areas.py --all     # all zones (incl. inactive)
  python3 scrape_idealista_areas.py --headful # show browser window
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import nodriver as uc

# ── Paths / constants ─────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
AREAS_PATH = BASE_DIR / "idealista_area_settings.json"
OUT_PATH   = BASE_DIR / "idealista_neighbourhood_map.json"

IDEALISTA_CITY_URL   = "https://www.idealista.it/affitto-case/milano-milano/"
IDEALISTA_ZONE_BASE  = "https://www.idealista.it/affitto-case/milano/"
# Zone pages use /affitto-case/milano/<zone-slug>/
# Sub-zone pages:   /affitto-case/milano/<zone-slug>/<subzone-slug>/

# ── JS: read zone list and sub-zone list from the page ───────────────────────
#
# Idealista zone pages (https://www.idealista.it/affitto-case/milano/<zone>/)
# render two useful ULs:
#   ul.breadcrumb-dropdown-list        — all city zones (top level)
#   ul.breadcrumb-dropdown-subitem-list — sub-zones of the CURRENT zone only
#
# Each <li> contains an <a> with the zone href and text, plus a trailing
# number (listing count).  Example li text: "Chiesa Rossa\n52"

_ZONES_LIST_JS = r"""
JSON.stringify((() => {
    // Top-level zone list (all zones for Milano)
    const ul = document.querySelector('ul.breadcrumb-dropdown-list');
    if (!ul) return null;
    const out = [];
    const seen = new Set();
    for (const li of ul.querySelectorAll(':scope > li')) {
        const a = li.querySelector('a');
        if (!a) continue;
        const href = a.href || '';
        const name = (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim();
        if (!href || !name || seen.has(href)) continue;
        seen.add(href);
        // Count is in a sibling span or as trailing text in the li
        const liText = (li.innerText || li.textContent || '').replace(/\s+/g,' ').trim();
        const countM = liText.replace(name,'').match(/\d+/);
        const count  = countM ? parseInt(countM[0], 10) : null;
        out.push({name, href, count});
    }
    return out.length ? out : null;
})())
"""

_SUBZONES_LIST_JS = r"""
JSON.stringify((() => {
    // Sub-zones for the current zone
    const ul = document.querySelector('ul.breadcrumb-dropdown-subitem-list');
    if (!ul) return null;
    const out = [];
    const seen = new Set();
    for (const li of ul.querySelectorAll(':scope > li')) {
        const a = li.querySelector('a');
        if (!a) continue;
        const href = a.href || '';
        const name = (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim();
        if (!href || !name || seen.has(href)) continue;
        seen.add(href);
        const liText = (li.innerText || li.textContent || '').replace(/\s+/g,' ').trim();
        const countM = liText.replace(name,'').match(/\d+/);
        const count  = countM ? parseInt(countM[0], 10) : null;
        out.push({name, href, count});
    }
    return out.length ? out : null;
})())
"""

_STATUS_JS = r"""
JSON.stringify({
    url:   window.location.href,
    h1:    (document.querySelector('h1')?.innerText || '').slice(0, 120),
    items: document.querySelectorAll('article.item').length,
    hasList: !!document.querySelector('ul.breadcrumb-dropdown-list'),
    hasSubList: !!document.querySelector('ul.breadcrumb-dropdown-subitem-list'),
})
"""

# JS to detect if the page rendered a "no listings" / redirect / error state
_STATUS_JS = r"""
JSON.stringify({
    url:   window.location.href,
    h1:    (document.querySelector('h1')?.innerText || '').slice(0, 120),
    items: document.querySelectorAll('article.item').length,
    body:  (document.body?.innerText || '').slice(0, 200),
})
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug_from_href(href: str) -> tuple[str, str]:
    """
    Extract (zone_slug, subzone_slug) from an Idealista area href.
    URL patterns:
      /affitto-case/milano/<zone>/              → (zone, '')
      /affitto-case/milano/<zone>/<subzone>/    → (zone, subzone)
    Returns ('', '') if unrecognised.
    """
    # Match both /milano/ and /milano-milano/ for robustness
    m = re.search(r'/affitto-case/milano(?:-milano)?/([^/?#]+)(?:/([^/?#]+))?/?', href)
    if m:
        return m.group(1) or '', m.group(2) or ''
    return '', ''


def _zone_url(slug: str) -> str:
    return f"{IDEALISTA_ZONE_BASE}{slug}/"


def _subzone_url(zone_slug: str, subzone_slug: str) -> str:
    return f"{IDEALISTA_ZONE_BASE}{zone_slug}/{subzone_slug}/"


async def _wait_for_page(tab, timeout: float = 20.0) -> bool:
    """Wait until body has content (past blank/JS-boot phase)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            n = await tab.evaluate("(document.body?.innerText || '').length")
            if n and int(n) > 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


async def _load_zone_page(tab, url: str, label: str = "") -> bool:
    """Navigate to a zone page and wait for it to fully render."""
    await tab.get(url)
    await asyncio.sleep(3.0)
    await _wait_for_page(tab)
    await asyncio.sleep(8.0)   # let breadcrumb dropdowns render

    status_raw = await tab.evaluate(_STATUS_JS)
    if not status_raw or not isinstance(status_raw, str):
        return False
    try:
        st = json.loads(status_raw)
    except Exception:
        return False

    actual = st.get("url", "")
    if "idealista.it" not in actual:
        print(f"    [skip] redirected: {actual}", flush=True)
        return False

    print(f"    h1={st.get('h1','')!r}  items={st.get('items',0)}"
          f"  list={st.get('hasList')}  sublist={st.get('hasSubList')}",
          flush=True)
    return True


async def _scrape_all_zones(tab) -> list:
    """
    Navigate to the first zone's page to read the full top-level zone list
    from ul.breadcrumb-dropdown-list.  Returns [{name, slug, url, listings}].
    """
    # Use "abbiategrasso-chiesa-rossa" as a known-good first zone
    seed_url = _zone_url("abbiategrasso-chiesa-rossa")
    print(f"Reading zone list from seed page: {seed_url}", flush=True)
    ok = await _load_zone_page(tab, seed_url, "seed")
    if not ok:
        return []

    raw = await tab.evaluate(_ZONES_LIST_JS)
    if not raw or not isinstance(raw, str) or raw == "null":
        return []
    try:
        items = json.loads(raw)
    except Exception:
        return []

    zones = []
    for item in (items or []):
        name = item.get("name", "").strip()
        href = item.get("href", "").strip()
        if not name or not href:
            continue
        z_slug, sz_slug = _slug_from_href(href)
        if not z_slug or sz_slug:   # skip non-zone or sub-zone links
            continue
        full_url = href if href.startswith("http") else f"https://www.idealista.it{href}"
        zones.append({"name": name, "slug": z_slug,
                      "url": full_url, "listings": item.get("count")})

    # The seed zone itself appears as a <span> (not a link) in the breadcrumb,
    # so it's absent from the list. Add it explicitly.
    SEED_SLUG = "abbiategrasso-chiesa-rossa"
    if not any(z["slug"] == SEED_SLUG for z in zones):
        zones.insert(0, {
            "name": "Abbiategrasso - Chiesa Rossa",
            "slug": SEED_SLUG,
            "url":  _zone_url(SEED_SLUG),
            "listings": None,
        })

    print(f"Found {len(zones)} top-level zones", flush=True)
    return zones


async def _scrape_subzones(tab, zone_name: str, zone_slug: str,
                            debug: bool = False) -> list:
    """
    Navigate to a zone page and return its sub-zones from
    ul.breadcrumb-dropdown-subitem-list.
    """
    url = _zone_url(zone_slug)
    print(f"  ↳ {zone_name}", flush=True)
    ok = await _load_zone_page(tab, url, zone_name)
    if not ok:
        return []

    raw = await tab.evaluate(_SUBZONES_LIST_JS)
    if not raw or not isinstance(raw, str) or raw == "null":
        print(f"    → no sub-zones", flush=True)
        return []
    try:
        items = json.loads(raw)
    except Exception as e:
        print(f"    [parse error: {e}]", flush=True)
        return []

    subzones = []
    for item in (items or []):
        name  = item.get("name", "").strip()
        href  = item.get("href", "").strip()
        count = item.get("count")
        if not name or not href:
            continue
        _, sz_slug = _slug_from_href(href)
        if not sz_slug:
            continue
        full_url = href if href.startswith("http") else f"https://www.idealista.it{href}"
        subzones.append({"name": name, "slug": sz_slug,
                         "url": full_url, "listings": count})

    print(f"    → {len(subzones)} sub-zones: "
          + ", ".join(f"{s['name']} ({s['listings']})" for s in subzones),
          flush=True)
    return subzones


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_zones(all_zones: bool) -> list:
    """Return list of (name, active) from idealista_area_settings.json."""
    if not AREAS_PATH.exists():
        print(f"[warn] {AREAS_PATH} not found — using hardcoded zone list")
        return [
            {"name": "Abbiategrasso - Chiesa Rossa", "active": True},
            {"name": "Baggio",                        "active": True},
            {"name": "Certosa",                       "active": True},
            {"name": "Città Studi - Lambrate",        "active": True},
            {"name": "Comasina - Bicocca",            "active": True},
            {"name": "Corvetto - Rogoredo",           "active": True},
            {"name": "Famagosta - Naviglio Grande",   "active": True},
            {"name": "Fiera - De Angeli",             "active": True},
            {"name": "Forlanini",                     "active": True},
            {"name": "Garibaldi - Porta Venezia",     "active": True},
            {"name": "Greco - Turro",                 "active": True},
            {"name": "Lorenteggio - Bande Nere",      "active": True},
            {"name": "Navigli - Bocconi",             "active": True},
            {"name": "Porta Vittoria",                "active": True},
            {"name": "San Siro - Trenno",             "active": True},
            {"name": "Vigentino - Ripamonti",         "active": True},
        ]

    data = json.loads(AREAS_PATH.read_text())
    zones = data.get("areas", [])
    if all_zones:
        return zones
    # Only zones inside the city of Milano (exclude province)
    city_zones = [z for z in zones if z.get("active") or
                  z.get("listings", 0) > 5]  # rough heuristic
    return city_zones


def to_url_slug(name: str) -> str:
    """Convert zone display name to Idealista URL slug."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"['\"]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


async def main(headless: bool, all_zones: bool, debug: bool):
    from fetch_rentals import EDGE_PATH

    # Load active-zone metadata for active/listings flags
    zone_meta = {z["name"]: z for z in _load_zones(all_zones=True)}

    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=headless,
        lang="it-IT",
    )

    result = {
        "source":     "idealista.it",
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "city_url":   IDEALISTA_CITY_URL,
        "zone_base":  IDEALISTA_ZONE_BASE,
        "zones":      [],
    }

    try:
        tab = await browser.get("about:blank")

        # ── Pass 1: get the authoritative zone list from Idealista ────────────
        live_zones = await _scrape_all_zones(tab)

        if not live_zones:
            # Fallback: use our local settings file
            print("[warn] Could not read live zone list — using local settings",
                  flush=True)
            settings_zones = _load_zones(all_zones=all_zones)
            live_zones = [{"name": z["name"],
                           "slug": to_url_slug(z["name"]),
                           "url":  _zone_url(to_url_slug(z["name"])),
                           "listings": z.get("listings")}
                          for z in settings_zones]

        # Filter if not --all (keep only city-of-Milano zones, not province)
        if not all_zones:
            # Province zones have very different slugs (e.g. "rhodense", "martesana")
            # and no match in zone_meta active list; keep zones that ARE in our settings.
            settings_slugs = {to_url_slug(n) for n in zone_meta}
            live_zones = [z for z in live_zones if z["slug"] in settings_slugs]

        print(f"\nScraping sub-zones for {len(live_zones)} zones…\n", flush=True)

        # ── Pass 2: per-zone sub-zone scrape ─────────────────────────────────
        for zone in live_zones:
            name  = zone["name"]
            slug  = zone["slug"]
            meta  = zone_meta.get(name, {})

            subzones = await _scrape_subzones(tab, name, slug, debug=debug)

            result["zones"].append({
                "name":     name,
                "slug":     slug,
                "url":      zone["url"],
                "active":   meta.get("active", False),
                "listings": zone.get("listings") or meta.get("listings"),
                "subzones": subzones,
            })

            await asyncio.sleep(1.0)   # polite pause

    finally:
        browser.stop()

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n✓ Written to {OUT_PATH}")

    total_sub = sum(len(z["subzones"]) for z in result["zones"])
    with_sub  = sum(1 for z in result["zones"] if z["subzones"])
    print(f"  Zones: {len(result['zones'])}  |  With sub-zones: {with_sub}  "
          f"|  Total sub-zones: {total_sub}")

    return result


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--all",     action="store_true",
                   help="Include all zones (not just active/city)")
    p.add_argument("--headful", action="store_true",
                   help="Show browser window (default: headless)")
    p.add_argument("--debug",   action="store_true",
                   help="Print detailed DOM debug info per zone")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(headless=not args.headful, all_zones=args.all, debug=args.debug))
