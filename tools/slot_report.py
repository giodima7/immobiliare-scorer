"""Summarise the spread-scan pilot from scan_slots_<city>.jsonl (written by
fetch_rentals.run_slot) and the detail drip lines in scanner.log.

    python3 tools/slot_report.py                      # last 48 h, both sites
    python3 tools/slot_report.py --hours 24 --source idealista

What to look at: incomplete slots (blocks), hours between two complete
sweeps of an area (should stay ≤ ~24), new listings found by a sweep
rather than discover (discover missing them), and listings missed once
that came back (how often one complete sweep misses a live listing).
"""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--city", default="milano")
    ap.add_argument("--source", choices=("immobiliare", "idealista"))
    a = ap.parse_args()
    since = datetime.now() - timedelta(hours=a.hours)
    path = ROOT / f"scan_slots_{a.city}.jsonl"
    recs = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        r["_at"] = datetime.fromisoformat(r["at"])
        r.setdefault("source", "immobiliare")   # the first slots had no source
        if r["_at"] >= since and (a.source is None or r["source"] == a.source):
            recs.append(r)
    if not recs:
        raise SystemExit(f"no slots since {since:%Y-%m-%d %H:%M} in {path}")
    for source in ("immobiliare", "idealista"):
        rs = [r for r in recs if r["source"] == source]
        if rs:
            print(f"\n══ {source} ══")
            report(rs, since, ROOT / ("scanner.log" if source == "immobiliare"
                                      else "scanner_idealista.log"), source)


def report(recs: list, since: datetime, log: Path, source: str) -> None:
    span_h = max((recs[-1]["_at"] - recs[0]["_at"]).total_seconds() / 3600, 1)

    complete = [r for r in recs if r["complete"]]
    pages = sum(r["pages"] for r in recs)
    disc = [r["discover"] for r in recs if r.get("discover")]
    dpages = sum(d["pages"] for d in disc)
    print(f"Slots {recs[0]['_at']:%d/%m %H:%M} → {recs[-1]['_at']:%d/%m %H:%M} ({span_h:.0f} h)")
    print(f"  {len(recs)} slots · {len(complete)} read their area in full · "
          f"{len(recs) - len(complete)} not")
    for r in recs:
        if not r["complete"]:
            why = "; ".join(r.get("skipped") or []) or f"{r['parse_failed']} unreadable page(s)"
            print(f"    ✗ {r['_at']:%d/%m %H:%M} {r['area']}: {why[:120]}")
    rate = f" ≈ {(pages + dpages) / span_h * 24:.0f} per 24 h" if span_h >= 12 else ""
    print(f"  page loads: {pages} sweep + {dpages} discover{rate} · "
          f"max {max(r['pages'] for r in recs)} in one slot")
    print(f"  slot time: avg {sum(r['seconds'] for r in recs) / len(recs):.0f} s")

    by_area = defaultdict(list)
    for r in complete:
        by_area[r["area"]].append(r["_at"])
    print("\nAreas (complete sweeps, longest gap between two):")
    for area in sorted({r["area"] for r in recs}):
        ts = sorted(by_area.get(area, []))
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:])]
        gap = f"{max(gaps):.1f} h" if gaps else "—"
        print(f"  {area:38s} {len(ts):2d}× · longest gap {gap}")

    new = sum(r.get("new", 0) for r in recs)
    via_d = sum(r.get("new_via_discover", 0) for r in recs)
    via_s = sum(r.get("new_via_sweep", 0) for r in recs)
    print(f"\nNew listings: {new} ({via_d} via discover, {via_s} via sweep)")
    if disc:
        caught = sum(d["caught_up"] for d in disc)
        print(f"  discover: {len(disc)} runs · {dpages} pages · {sum(d['new'] for d in disc)} new · "
              f"caught up with the previous run {caught}/{len(disc)}")

    missed = sum(r.get("missed", 0) for r in recs)
    hidden = sum(r.get("hidden", 0) for r in recs)
    revived = sum(r.get("revived", 0) for r in recs)
    print(f"\nDe-listing: {missed} missed once · {hidden} hidden · {revived} came back after a miss")
    distrusted = [(r["area"], r["_at"]) for r in recs if r.get("distrusted")]
    for area, at in distrusted:
        print(f"  ! {at:%d/%m %H:%M} {area}: sweep too thin, not used to de-list")

    ok = gone = failed = blocked = 0
    rx = re.compile(rf"^\[(\S+ \S+)\] \[details\] {source}: (?:(\d+) ok, (\d+) gone, (\d+) failed|.*blocked)")
    seen = set()
    for line in log.read_text(errors="replace").splitlines() if log.exists() else []:
        m = rx.match(line)
        if not m or line in seen:     # the daemon logs each line twice
            continue
        seen.add(line)
        if datetime.fromisoformat(m.group(1)) < since:
            continue
        if m.group(2) is None:
            blocked += 1
        else:
            ok, gone, failed = ok + int(m.group(2)), gone + int(m.group(3)), failed + int(m.group(4))
    print(f"\nDetail drip: {ok} ok, {gone} gone, {failed} failed, {blocked} block(s)")


if __name__ == "__main__":
    main()
