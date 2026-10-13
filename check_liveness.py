"""
check_liveness.py — bulk liveness check + archive for expired listings.

Opens each listing's URL in nodriver (Edge). If the listing redirects
away (Immobiliare delists by redirecting /annunci/{id}/ → /annunci/ or
a city search page; Idealista returns a 404-redirect), it is archived:
  • local JSON: is_stale=True, excluded=True, excluded_reason set
  • Supabase: same patch (optional — skipped if env vars missing)

Supports resume: completed IDs are written to a .progress JSON file
after each batch, so a re-run skips already-checked listings.

Usage:
    python3 check_liveness.py --dry-run
    python3 check_liveness.py --limit 50
    python3 check_liveness.py --listing-type sale
    python3 check_liveness.py --listing-type all
    python3 check_liveness.py --listing-type rental --skip-stale

Requires Microsoft Edge installed at the default path.
Supabase env vars (SUPABASE_URL + SUPABASE_SERVICE_KEY) are optional;
set them (e.g. via .env) to also patch Supabase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import nodriver as uc

BASE_DIR   = Path(__file__).resolve().parent
DASHBOARD  = BASE_DIR / "dashboard"
EDGE_PATH  = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROGRESS_F = BASE_DIR / ".check_liveness_progress.json"

# Seconds between requests — enough to avoid triggering rate limits.
DEFAULT_DELAY = 4.0

FILES = {
    "rental": [
        DASHBOARD / "milano_rentals_latest.json",
        DASHBOARD / "rentals_latest.json",
    ],
    "sale": [
        DASHBOARD / "milano_sales_latest.json",
        DASHBOARD / "sales_latest.json",
    ],
}


# ── Supabase helpers (optional) ──────────────────────────────────────────────

def _supabase_env() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if url and key:
        return url, key
    return None


def _supabase_patch(listing_id: str, body: dict, env: tuple) -> None:
    base_url, key = env
    url  = f"{base_url}/rest/v1/listings?id=eq.{urllib.parse.quote(listing_id)}"
    data = json.dumps(body).encode("utf-8")
    headers = {
        "apikey":           key,
        "Authorization":    f"Bearer {key}",
        "Content-Type":     "application/json",
        "Prefer":           "return=minimal",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _ = resp.read()
    except Exception as exc:
        print(f"      [supabase-warn] PATCH failed: {exc}")


# ── Local JSON helpers ───────────────────────────────────────────────────────

def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [warn] could not read {path.name}: {exc}")
        return []


def _save_json(path: Path, data: list[dict]) -> None:
    # Use write_snapshot so slim_listing applies (renames _excluded → excluded etc.)
    sys.path.insert(0, str(BASE_DIR))
    from dashboard_io import write_snapshot
    tmp = path.with_suffix(path.suffix + ".tmp")
    write_snapshot(tmp, data)
    tmp.replace(path)


def _archive_in_file(path: Path, listing_id: str, reason: str) -> bool:
    """Set is_stale + _excluded on a listing in a JSON file. Returns True if found."""
    data = _load_json(path)
    found = False
    for row in data:
        if str(row.get("id", "")) == listing_id:
            row["is_stale"]       = True
            row["_excluded"]      = True
            row["_excluded_reason"] = reason
            found = True
            break
    if found:
        _save_json(path, data)
    return found


# ── Progress tracking ────────────────────────────────────────────────────────

def _load_progress() -> set[str]:
    if PROGRESS_F.exists():
        try:
            return set(json.loads(PROGRESS_F.read_text()))
        except Exception:
            pass
    return set()


def _save_progress(done: set[str]) -> None:
    PROGRESS_F.write_text(json.dumps(sorted(done)))


# ── Candidate collection ─────────────────────────────────────────────────────

def collect_candidates(listing_type: str, skip_stale: bool) -> list[dict]:
    """Return list of {id, url, source, files:[paths]} dicts to check."""
    types = ["rental", "sale"] if listing_type == "all" else [listing_type]
    seen_ids: dict[str, dict] = {}

    for lt in types:
        for path in FILES[lt]:
            data = _load_json(path)
            for row in data:
                lid = str(row.get("id", ""))
                if not lid:
                    continue
                if skip_stale and (row.get("is_stale") or row.get("excluded")):
                    continue
                url = row.get("url", "")
                if not url:
                    src = row.get("source", "")
                    if src == "idealista":
                        num = lid.replace("id_", "")
                        url = f"https://www.idealista.it/immobile/{num}/"
                    else:
                        url = f"https://www.immobiliare.it/annunci/{lid}/"
                if lid not in seen_ids:
                    seen_ids[lid] = {"id": lid, "url": url,
                                     "source": row.get("source", ""),
                                     "files": []}
                if path not in seen_ids[lid]["files"]:
                    seen_ids[lid]["files"].append(path)

    return list(seen_ids.values())


# ── URL-pattern for "still on this listing" ─────────────────────────────────

def _still_on_listing(current_url: str, listing_id: str, source: str) -> bool:
    """Return True if the browser is still showing the original listing."""
    if not current_url:
        return False
    if source == "idealista":
        num = listing_id.replace("id_", "")
        return f"/immobile/{num}" in current_url
    # immobiliare + sale
    return f"/annunci/{listing_id}" in current_url


# ── Main async loop ──────────────────────────────────────────────────────────

async def run(candidates: list[dict], delay: float, dry_run: bool,
              supabase_env) -> dict:
    done     = _load_progress()
    to_check = [c for c in candidates if c["id"] not in done]

    print(f"\nCandidates: {len(candidates)}  already-done: {len(done)}"
          f"  to-check: {len(to_check)}")
    if not to_check:
        print("Nothing left to check.")
        return {"checked": 0, "delisted": 0, "live": 0, "error": 0}

    if dry_run:
        print("[dry-run] would check:")
        for c in to_check[:20]:
            print(f"  {c['id']:20s}  {c['url']}")
        if len(to_check) > 20:
            print(f"  … and {len(to_check)-20} more")
        return {"checked": 0, "delisted": 0, "live": 0, "error": 0}

    print(f"Opening Edge for {len(to_check)} liveness check(s) @ {delay}s delay…")
    browser = await uc.start(
        browser_executable_path=EDGE_PATH,
        headless=False,
        lang="it-IT",
    )

    checked = delisted = live = errors = 0
    try:
        for i, c in enumerate(to_check, 1):
            lid    = c["id"]
            url    = c["url"]
            source = c["source"]
            print(f"\n  [{i}/{len(to_check)}] {lid} — {url}", flush=True)
            try:
                tab = await browser.get(url)
                await asyncio.sleep(delay)
                cur = await tab.evaluate("window.location.href")
                still_here = _still_on_listing(cur or "", lid, source)
                if still_here:
                    print(f"      [live]")
                    live += 1
                else:
                    print(f"      [delisted] redirected → {cur}")
                    reason = "Delisted — listing no longer available on source"
                    for fpath in c["files"]:
                        hit = _archive_in_file(fpath, lid, reason)
                        print(f"      → archived in {fpath.name} ({hit})")
                    if supabase_env:
                        _supabase_patch(lid, {
                            "is_stale":        True,
                            "excluded":        True,
                            "excluded_reason": reason,
                        }, supabase_env)
                        print(f"      → Supabase patched")
                    delisted += 1
                checked += 1
            except Exception as exc:
                print(f"      [error] {type(exc).__name__}: {exc}")
                errors += 1

            done.add(lid)
            if i % 25 == 0:
                _save_progress(done)
                print(f"  [progress saved — {i} checked, {delisted} delisted so far]")

    finally:
        _save_progress(done)
        try:
            await browser.stop()
        except Exception:
            pass

    return {"checked": checked, "delisted": delisted, "live": live, "error": errors}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listing-type", default="rental",
                   choices=["rental", "sale", "all"],
                   help="Which listings to check (default: rental)")
    p.add_argument("--limit", type=int, default=0,
                   help="Max candidates to check (0 = no limit)")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between requests (default: {DEFAULT_DELAY})")
    p.add_argument("--dry-run", action="store_true",
                   help="List candidates without opening browser")
    p.add_argument("--skip-stale", action="store_true", default=True,
                   help="Skip listings already marked stale/excluded (default: on)")
    p.add_argument("--reset-progress", action="store_true",
                   help="Clear saved progress and re-check from scratch")
    args = p.parse_args()

    if args.reset_progress and PROGRESS_F.exists():
        PROGRESS_F.unlink()
        print("Progress reset.")

    # Auto-load .env if present
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    supabase_env = _supabase_env()
    if supabase_env:
        print("Supabase: connected (will patch delisted rows)")
    else:
        print("Supabase: not configured (local-JSON only)")

    candidates = collect_candidates(args.listing_type, args.skip_stale)
    if args.limit:
        candidates = candidates[:args.limit]

    print(f"Listing type: {args.listing_type}  |  candidates: {len(candidates)}"
          f"  |  delay: {args.delay}s")

    result = asyncio.run(run(candidates, args.delay, args.dry_run, supabase_env))

    print(f"\n── Summary ──")
    print(f"  Checked:   {result['checked']}")
    print(f"  Live:      {result['live']}")
    print(f"  Delisted:  {result['delisted']}")
    print(f"  Errors:    {result['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
