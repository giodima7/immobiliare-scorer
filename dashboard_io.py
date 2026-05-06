"""
dashboard_io.py
───────────────
Centralised writer for the dashboard's JSON snapshots
(rentals_latest.json / sales_latest.json / latest.json).

Cloudflare Pages caps assets at 25 MiB per file. Indented pretty-printed JSON
adds ~30% to file size and pushes the rentals snapshot over the limit. This
helper enforces:

  • compact JSON (no indentation, tight separators)
  • drops null / empty values to remove dead bytes
  • drops underscore-prefixed internal/debug fields the dashboard never reads

Call `write_snapshot(path, listings)` from any code that produces a snapshot
the dashboard reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Underscore-prefixed scoring/debug fields the dashboard never displays.
# If you add new ones in scoring.py / explain.py, prefix them with `_` and
# they'll be stripped automatically.
_INTERNAL_FIELDS: frozenset[str] = frozenset({
    "_fetched_area",
    "_absolute_value_gate_applied",
    "_corporate_ceiling_applied",
    "_is_corporate_rental",
    "_room_efficiency_flag",
})

# Legacy fields superseded by newer ones. Dropped from snapshots to stay
# under the 25 MiB Cloudflare cap.
#   score_explanation -> replaced by score_reasons (component-tagged dicts)
_LEGACY_FIELDS: frozenset[str] = frozenset({
    "score_explanation",
})

_EMPTY = (None, "", [], {})


def slim_listing(listing: dict) -> dict:
    """Drop null/empty values, internal debug fields, and legacy fields."""
    return {
        k: v
        for k, v in listing.items()
        if v not in _EMPTY
        and not k.startswith("_")
        and k not in _LEGACY_FIELDS
    }


def write_snapshot(path: str | Path, listings: list[dict]) -> int:
    """
    Serialise `listings` to `path` as compact JSON, returning the byte size.

    Replaces the previous `json.dump(..., indent=2)` pattern that was inflating
    snapshots ~30% beyond the actual content.
    """
    path = Path(path)
    slim = [slim_listing(l) for l in listings]
    payload = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    path.write_text(payload, encoding="utf-8")
    return len(payload.encode("utf-8"))


def slim_in_place(path: str | Path) -> tuple[int, int]:
    """Re-write an existing snapshot in slim form. Returns (before, after) bytes."""
    path = Path(path)
    before = path.stat().st_size
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a JSON array of listings")
    after = write_snapshot(path, data)
    return before, after
