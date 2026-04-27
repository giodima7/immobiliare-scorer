#!/usr/bin/env python3
"""
trim_cache.py
─────────────
Remove enrichment cache entries older than --max-age-days (default 90).
Run before committing enriched_cache.json to keep the file under GitHub's
50 MB push limit.

Usage:
    python trim_cache.py
    python trim_cache.py --max-age-days 60
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def trim(max_age_days: int) -> None:
    path = Path("enriched_cache.json")
    if not path.exists():
        print("No cache file found — skipping")
        return
    cache = json.loads(path.read_text())
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    before = len(cache)
    cache = {
        k: v for k, v in cache.items()
        if datetime.fromisoformat(
            v.get("enriched_at", "2000-01-01T00:00:00+00:00")
        ) > cutoff
    }
    after = len(cache)
    path.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"Cache trimmed: {before} → {after} entries ({before - after} removed)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Trim old enrichment cache entries.")
    p.add_argument("--max-age-days", type=int, default=90,
                   help="Remove entries older than this many days (default: 90)")
    trim(p.parse_args().max_age_days)
