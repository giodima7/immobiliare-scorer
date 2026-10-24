"""Build dashboard/assets/icons.svg — an SVG sprite of the Tabler outline
icons the dashboard uses (MIT, https://tabler.io/icons), pinned version.

Use in markup: <svg class="i"><use href="assets/icons.svg#NAME"/></svg>
(app.js helper: icon("NAME")). Add a name to ICONS and re-run:
    python3 tools/build_icons.py
"""
import re
import sys
import urllib.request
from pathlib import Path

VERSION = "3.19.0"
URL = "https://cdn.jsdelivr.net/npm/@tabler/icons@{v}/icons/outline/{n}.svg"
OUT = Path(__file__).resolve().parent.parent / "dashboard" / "assets" / "icons.svg"

ICONS = """
map-pin map currency-euro ruler-2 adjustments-horizontal filter arrows-sort x check
armchair plant car elevator flame bath sparkles alert-triangle train bus building clock
external-link heart bookmark share photo shopping-cart trees chevron-down chevron-right
layout-sidebar-right info-circle trending-down refresh home chart-bar settings star bell
user school search key bed stairs bolt sun eye calendar file-text air-conditioning
diamond coin moon device-desktop arrow-up-right copy trash plus minus mail lock list-details world
circle-half-2 language player-play player-stop player-pause pencil folder arrow-down arrow-up
selector walk percentage tool database circle-check arrow-left dots-vertical door tag maximize
chevron-left
""".split()


def fetch(name: str) -> str:
    with urllib.request.urlopen(URL.format(v=VERSION, n=name), timeout=20) as r:
        return r.read().decode()


def main() -> None:
    symbols, missing = [], []
    for name in ICONS:
        try:
            svg = fetch(name)
        except Exception:
            missing.append(name)
            continue
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
        inner = re.sub(r'<path stroke="none" d="M0 0h24v24H0z" fill="none"\s*/>', "", inner)
        inner = re.sub(r"\s+", " ", inner).strip()
        symbols.append(f'<symbol id="{name}" viewBox="0 0 24 24">{inner}</symbol>')
    OUT.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
        f"<!-- Tabler Icons {VERSION} (MIT) — built by tools/build_icons.py -->\n"
        + "\n".join(symbols) + "\n</svg>\n")
    print(f"{len(symbols)} icons → {OUT} ({OUT.stat().st_size // 1024} KB)")
    if missing:
        print("missing:", " ".join(missing), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
