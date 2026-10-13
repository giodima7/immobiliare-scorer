"""
One-shot debug: dump the raw __NEXT_DATA__ search-results payload from
Immobiliare for one macro-area slug, so we can see exactly which fields
exist (and which are missing) for the listings whose bedrooms parse to
null in fetch_rentals.parse_rental.

Usage:
    python3 debug_search_payload.py centro
    python3 debug_search_payload.py porta-romana-cadore-montenero

Output:
    debug_search_payload.json    full first-page payload, pretty-printed
    debug_search_summary.txt     one line per listing — id, source-shape
                                 hints (which of bedRoomsNumber / bedrooms
                                 / bathrooms / rooms keys are present and
                                 their values), so the broken cases are
                                 immediately visible
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Reuse the existing scanner's nodriver + Edge setup so this script
# inherits all the anti-bot workarounds without reinventing them.
import nodriver as uc

EDGE_PATH = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
)

# Same field names parse_rental tries — printing both their values AND
# their presence (vs absent) makes it obvious whether the agent simply
# left the field blank or whether Immobiliare returns a different name
# we're not reading.
FIELDS_OF_INTEREST = (
    "rooms", "roomsNumber", "numRooms",
    "bedRoomsNumber", "bedrooms", "bedroomsNumber",
    "bathrooms", "bathRooms",
    "surface", "surfaceValue",
    "typology", "typologyId", "typologyValue",
)


async def _run(slug: str) -> None:
    url = f"https://www.immobiliare.it/affitto-case/milano/{slug}/"
    print(f"  GET {url}", flush=True)
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )
    try:
        tab = await browser.get(url)
        # Generous settle delay — anti-bot challenge sometimes injects a
        # brief JS check before __NEXT_DATA__ is populated.
        await asyncio.sleep(4.0)
        raw = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
    finally:
        browser.stop()

    if not raw:
        print("  [error] __NEXT_DATA__ empty — Cloudflare probably challenged.")
        sys.exit(1)

    nd = json.loads(raw)
    try:
        data = nd["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
        results = data.get("results", [])
    except Exception as exc:
        print(f"  [error] could not reach results[] in __NEXT_DATA__: {exc}")
        Path("debug_search_payload.json").write_text(json.dumps(nd, indent=2))
        print("  → wrote raw __NEXT_DATA__ to debug_search_payload.json")
        return

    print(f"  {len(results)} listings on page 1")
    Path("debug_search_payload.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    print(f"  → wrote full results[] to debug_search_payload.json")

    # Per-listing summary — one line per listing, fields-of-interest in a
    # compact form so the broken cases stand out at a glance.
    lines = []
    for item in results:
        re_data = item.get("realEstate", {})
        props   = re_data.get("properties", [{}])
        prop    = props[0] if props else {}
        lid     = re_data.get("id", "?")
        title   = (re_data.get("title") or "").strip()[:60]

        bits = []
        for k in FIELDS_OF_INTEREST:
            if k in prop:
                v = prop[k]
                if isinstance(v, (dict, list)):
                    v = f"<{type(v).__name__}:{len(v)}>"
                bits.append(f"{k}={v!r}")
        # Hunt for ANY key in prop that mentions "bed" or "camer" or
        # "bagn" — the goal is to surface field names we're not reading.
        extra_bed_keys = [k for k in prop.keys()
                          if "bed" in k.lower() or "camer" in k.lower() or "bagn" in k.lower()
                          and k not in FIELDS_OF_INTEREST]
        if extra_bed_keys:
            bits.append(f"EXTRA={extra_bed_keys}")
        lines.append(f"  {lid}  {title}\n      {' · '.join(bits)}")

    summary = "\n\n".join(lines)
    Path("debug_search_summary.txt").write_text(summary)
    print(f"  → wrote one-line-per-listing summary to debug_search_summary.txt")
    print()
    print("Now share both files — I'll grep them for the bedroom-field "
          "pattern and figure out whether the data is genuinely missing "
          "or just lives under a key we don't read.")


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "centro"
    asyncio.run(_run(slug))
    return 0


if __name__ == "__main__":
    sys.exit(main())
