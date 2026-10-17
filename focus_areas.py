"""Areas the user is searching right now (focus_areas.json): their listings
get their listing page (costs) and photos read before everything else
(listing_details.build_queue, photo_eval.cmd_select)."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

PATH = Path(__file__).resolve().parent / "focus_areas.json"
_cache: tuple | None = None   # (mtime, compiled areas)


def _areas() -> list:
    global _cache
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        return []
    if _cache is None or _cache[0] != mtime:
        try:
            raw = json.loads(PATH.read_text()).get("areas") or []
        except (OSError, ValueError):
            raw = []
        _cache = (mtime, [(re.compile(a["label"], re.I) if a.get("label") else None,
                           tuple(a["center"]) if a.get("center") else None,
                           float(a.get("radius_m") or 0)) for a in raw])
    return _cache[1]


def _dist_m(a: tuple, b: tuple) -> float:
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    return 6371000 * math.hypot(x, (b[0] - a[0]) * r)


def in_focus(l: dict) -> bool:
    """The listing's neighbourhood matches a focus area's label, or it lies
    within that area's radius."""
    nb = l.get("neighbourhood") or ""
    lat, lon = l.get("latitude"), l.get("longitude")
    for label, center, radius in _areas():
        if label and label.search(nb):
            return True
        if center and radius and lat and lon:
            try:
                if _dist_m(center, (float(lat), float(lon))) <= radius:
                    return True
            except (TypeError, ValueError):
                pass
    return False
