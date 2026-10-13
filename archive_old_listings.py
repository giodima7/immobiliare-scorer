"""
archive_old_listings.py — archive listings the scanners stopped seeing.

For each snapshot file, finds the most recent last_seen_date.
Listings not seen within ARCHIVE_AFTER_DAYS of it (or with no date) are marked:
  is_stale=True, _excluded=True, _excluded_reason set.

Listings already excluded/stale are left alone.

Usage:
    python3 archive_old_listings.py --dry-run   # show counts, no writes
    python3 archive_old_listings.py             # apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR  = Path(__file__).resolve().parent
DASHBOARD = BASE_DIR / "dashboard"

FILES = [
    DASHBOARD / "milano_rentals_latest.json",
    DASHBOARD / "milano_sales_latest.json",
    DASHBOARD / "rentals_latest.json",
    DASHBOARD / "sales_latest.json",
]

REASON = "Not seen in latest scan — archived pending liveness check"

# Daily scans are partial (new listings + a rotating 1/4 of areas), so a
# listing still on the market is only re-seen every few days. Archive only
# what the scanners haven't seen for this long — same threshold as
# fetch_rentals.STALE_DAYS_THRESHOLD.
ARCHIVE_AFTER_DAYS = 7


def process(path: Path, apply: bool) -> tuple[int, int, int]:
    """Returns (total, kept, archived)."""
    if not path.exists():
        print(f"  skip {path.name} — not found")
        return 0, 0, 0

    data = json.loads(path.read_text(encoding="utf-8"))
    if not data:
        return 0, 0, 0

    # Latest date present in this file
    dates = [l["last_seen_date"] for l in data if l.get("last_seen_date")]
    if not dates:
        print(f"  {path.name}: no last_seen_date on any listing — skipping")
        return len(data), 0, 0

    latest = max(dates)
    cutoff = (date.fromisoformat(latest) - timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat()
    print(f"  {path.name}: total={len(data)}  latest_scan={latest}  "
          f"archive if last seen before {cutoff}")

    kept = archived = already = 0
    for l in data:
        # Already archived → leave alone
        if l.get("is_stale") or l.get("excluded") or l.get("_excluded"):
            already += 1
            continue

        lsd = l.get("last_seen_date")
        if lsd and lsd > cutoff:
            kept += 1
        else:
            # Archive it
            l["is_stale"]         = True
            l["_excluded"]        = True
            l["_excluded_reason"] = REASON
            archived += 1

    print(f"    kept={kept}  to-archive={archived}  already-archived={already}")

    if apply and archived:
        sys.path.insert(0, str(BASE_DIR))
        from dashboard_io import write_snapshot
        tmp = path.with_suffix(path.suffix + ".tmp")
        write_snapshot(tmp, data)
        tmp.replace(path)
        print(f"    → wrote {path.name}")

    return len(data), kept, archived


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"archive_old_listings.py [{mode}]")
    print()

    totals = {"total": 0, "kept": 0, "archived": 0}
    for path in FILES:
        t, k, a = process(path, not args.dry_run)
        totals["total"]    += t
        totals["kept"]     += k
        totals["archived"] += a

    print()
    print(f"Total: {totals['total']}  kept: {totals['kept']}  archived: {totals['archived']}")
    if args.dry_run:
        print("Re-run without --dry-run to apply.")
    else:
        print()
        print("Next step: python3 check_liveness.py --listing-type all")
        print("  → checks archived listings, restores still-live ones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
