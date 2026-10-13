"""
Dump __NEXT_DATA__ for one Immobiliare listing detail page.

Usage:
    python3 debug_listing_payload.py 128683744

Output:
    debug_listing_128683744.json   full __NEXT_DATA__ from the detail URL
    debug_listing_128683744.txt    flattened key:value summary, highlighting
                                   any path containing "bed", "camer", "bagn",
                                   "room", "local" — so the bedroom-field
                                   path becomes obvious at a glance

The detail page (/annunci/{id}/) and the search-results page
(/affitto-case/{slug}/) may hold the data under different keys; this
script captures the detail-page shape specifically so we can pin down
why specific listings come through with bedrooms=null.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import nodriver as uc

EDGE_PATH = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

# Paths whose key contains any of these substrings are interesting for
# the bedroom/bathroom/rooms hunt.
KEY_HINTS = ("bed", "camer", "bagn", "room", "local", "surface")


def _flatten(obj, path="", out=None):
    """Walk the nested structure, yielding ('a.b.c', value) for every
    scalar leaf. Lists are indexed; dicts use dotted keys."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{path}.{k}" if path else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{path}[{i}]", out)
    else:
        out.append((path, obj))
    return out


async def _run(listing_id: str) -> None:
    url = f"https://www.immobiliare.it/annunci/{listing_id}/"
    print(f"  GET {url}", flush=True)
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )
    try:
        tab = await browser.get(url)
        await asyncio.sleep(4.5)
        raw = await tab.evaluate("JSON.stringify(window.__NEXT_DATA__)")
    finally:
        browser.stop()

    if not raw:
        print("  [error] __NEXT_DATA__ empty — Cloudflare probably challenged.")
        sys.exit(1)
    nd = json.loads(raw)
    json_path = Path(f"debug_listing_{listing_id}.json")
    json_path.write_text(json.dumps(nd, ensure_ascii=False, indent=2))
    print(f"  → wrote raw __NEXT_DATA__ to {json_path}")

    # Flatten + filter to interesting keys
    flat = _flatten(nd)
    hits = [(k, v) for k, v in flat
            if any(h in k.lower() for h in KEY_HINTS)
            and not isinstance(v, (dict, list))]
    txt_path = Path(f"debug_listing_{listing_id}.txt")
    txt_path.write_text(
        "\n".join(f"{k} = {v!r}" for k, v in hits)
    )
    print(f"  → wrote {len(hits)} interesting key:value pairs to {txt_path}")
    print()
    print("Share the .txt file — it'll have lines like:")
    print("  props.pageProps.detailData.realEstate.properties[0].bedRoomsNumber = '2'")
    print("which tells us exactly where Immobiliare hides the bedrooms field.")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 debug_listing_payload.py <listing_id>")
        return 1
    asyncio.run(_run(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
