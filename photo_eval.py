"""Read a listing's photos with Claude and record what they show.

Only the top listings (Hidden Gem / Great Value) are read: the point is to
check whether a deal holds up once you look at the flat — dated kitchen,
renders instead of photos, a basement — not to describe all 12k listings.

Runs on the Claude subscription, not API tokens: either `claude -p` (the
Claude Code CLI, logged in once with `claude` → /login) or a Claude Code
session that reads the sheets itself. Efficiency comes from:
  - up to 9 photos per listing, small (≤ 512 px), on ONE numbered 3×3 sheet
    (~2.4k image tokens per listing instead of ~1.5k per full-size photo);
  - several listings per call, plain JSON out, no tools;
  - a cache keyed by the photo URLs: a listing is read again only when its
    photos change.

    python3 photo_eval.py select --pilot 50     # queue (a seeded sample)
    python3 photo_eval.py select                # all top listings not read yet
    python3 photo_eval.py sheets                # download + build the sheets
    python3 photo_eval.py run --model haiku     # claude -p, batches of 8
    python3 photo_eval.py ingest results.json   # store answers from a session
    python3 photo_eval.py report                # agreement with stated condition

Files: photo_eval_cache/ (queue, sheets, batches) and photo_eval_milano.json
(the answers) — both local, not in git.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "dashboard" / "milano_rentals_latest.json"
CACHE = BASE / "photo_eval_cache"
SHEETS = CACHE / "sheets"
QUEUE = CACHE / "queue.json"
RESULTS = BASE / "photo_eval_milano.json"
PROMPT = BASE / "photo_eval_prompt.md"

TILE_W, TILE_H = 512, 384
MAX_PHOTOS = 9
MIN_PHOTOS = 4      # fewer can't show the flat — Idealista cards carry one
                    # photo until its listing page is read (listing_details.py)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")

CONDITION_FROM_LISTING = {   # stated condition → the 1–5 scale of the prompt
    "da ristrutturare": 1, "buono": 3, "abitabile": 3, "ottimo": 4,
    "ristrutturato": 4, "nuovo": 5, "in costruzione": 5,
}


# ── Data ─────────────────────────────────────────────────────────────────────
def load_listings() -> list[dict]:
    for _ in range(5):          # the scanners may be rewriting the file
        try:
            return json.loads(DATA.read_text())
        except json.JSONDecodeError:
            time.sleep(2)
    raise SystemExit("could not read " + str(DATA))


def load_results() -> dict:
    try:
        return json.loads(RESULTS.read_text())
    except (OSError, ValueError):
        return {}


def save_results(res: dict) -> None:
    tmp = RESULTS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    tmp.replace(RESULTS)


def photo_urls(l: dict) -> list[str]:
    """Unique interior-ish photos, small size: Immobiliare's 640 px variant,
    Idealista's 480 px; agency logos and duplicate formats dropped."""
    out, seen = [], set()
    for u in l.get("photos") or ([l["thumbnail"]] if l.get("thumbnail") else []):
        if not isinstance(u, str) or u.endswith(".gif"):
            continue
        if "im-cdn.it/image/" in u:
            u = re.sub(r"/[a-z]+(?:-c)?\.jpg$", "/xs-c.jpg", u)
            key = u
        elif "idealista" in u:
            if "image.master" not in u:
                continue
            u = re.sub(r"/blur/[^/]+/", "/blur/480_360_mq/", re.sub(r"\.webp$", ".jpg", u))
            key = u.rsplit("/", 1)[-1].split(".")[0]
        else:
            key = u
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out[:MAX_PHOTOS]


def fingerprint(urls: list[str]) -> str:
    return hashlib.sha1("\n".join(urls).encode()).hexdigest()[:12]


def is_top(l: dict) -> bool:
    return bool((l.get("hidden_gem") or l.get("good_value"))
                and not l.get("is_stale") and not (l.get("excluded") or l.get("_excluded")))


def context(l: dict) -> dict:
    """What the reader is told about the listing — not its stated condition,
    so the photo judgement stays blind and can be checked against it."""
    floor = l.get("floor_n")
    return {
        "id": str(l["id"]),
        "label": "Hidden Gem" if l.get("hidden_gem") else "Great Value",
        "area": l.get("neighbourhood"),
        "rent_eur_month": l.get("rent_mo") or l.get("price"),
        "sqm": l.get("sqm"),
        "rooms": l.get("rooms"),
        "floor": floor,
        "lift": l.get("elevator"),
        "discount_vs_nearby_pct": l.get("comps_delta_pct"),
    }


# ── select ───────────────────────────────────────────────────────────────────
def cmd_select(args) -> None:
    rows = load_listings()
    done = load_results()
    tops = [l for l in rows if is_top(l)]
    top = [l for l in tops if len(photo_urls(l)) >= MIN_PHOTOS]
    few = [l for l in tops if len(photo_urls(l)) < MIN_PHOTOS]
    print(f"top listings: {len(tops)} · too few photos (< {MIN_PHOTOS}) for now: {len(few)} "
          f"({sum(1 for l in few if l.get('source') == 'idealista')} Idealista)")
    todo = [l for l in top
            if done.get(str(l["id"]), {}).get("fingerprint") != fingerprint(photo_urls(l))]
    if args.pilot:
        rnd = random.Random(args.seed)
        todo = rnd.sample(todo, min(args.pilot, len(todo)))
    todo.sort(key=lambda l: -(l.get("score_total") or 0))
    CACHE.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps([{**context(l), "photos": photo_urls(l)} for l in todo],
                                ensure_ascii=False, indent=1))
    print(f"readable: {len(top)} · to read: {len(todo)} → {QUEUE}")


# ── sheets ───────────────────────────────────────────────────────────────────
def _fetch(url: str) -> bytes | None:
    # Some Immobiliare photos lack the 640 px variant: fall back to larger ones.
    tries = [url] + ([url.replace("/xs-c.jpg", v) for v in ("/s-c.jpg", "/m-c.jpg")]
                     if url.endswith("/xs-c.jpg") else [])
    referer = "https://www.idealista.it/" if "idealista" in url else "https://www.immobiliare.it/"
    for u in tries:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": referer})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception:
            time.sleep(0.5)
    return None


def build_sheet(item: dict, dest: Path) -> int:
    """Numbered grid of the listing's photos: 2×2, 3×2 or 3×3 tiles."""
    from PIL import Image, ImageDraw, ImageFont
    imgs = []
    for u in item["photos"]:
        raw = _fetch(u)
        time.sleep(0.3)                 # gentle on the portals' CDNs
        if not raw:
            continue
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            continue
        im.thumbnail((TILE_W, TILE_H))
        imgs.append(im)
    if not imgs:
        return 0
    n = len(imgs)
    cols, rows = (2, 2) if n <= 4 else (3, 2) if n <= 6 else (3, 3)
    sheet = Image.new("RGB", (cols * TILE_W, rows * TILE_H), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    for i, im in enumerate(imgs):
        x, y = (i % cols) * TILE_W, (i // cols) * TILE_H
        sheet.paste(im, (x + (TILE_W - im.width) // 2, y + (TILE_H - im.height) // 2))
        draw.rectangle([x + 6, y + 6, x + 44, y + 42], fill=(0, 0, 0))
        draw.text((x + 15, y + 9), str(i + 1), fill=(255, 255, 255), font=font)
    sheet.save(dest, "JPEG", quality=80)
    return n


def cmd_sheets(args) -> None:
    queue = json.loads(QUEUE.read_text())
    SHEETS.mkdir(parents=True, exist_ok=True)
    made = 0
    for i, item in enumerate(queue, 1):
        dest = SHEETS / f"{item['id']}.jpg"
        if dest.exists() and not args.force:
            item["n_photos"] = item.get("n_photos") or len(item["photos"])
            continue
        item["n_photos"] = build_sheet(item, dest)
        made += 1
        if i % 10 == 0:
            print(f"  {i}/{len(queue)}")
    QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    ok = sum(1 for it in queue if (SHEETS / f"{it['id']}.jpg").exists())
    print(f"sheets: {ok}/{len(queue)} ready ({made} built now) in {SHEETS}")


# ── run (claude -p) ──────────────────────────────────────────────────────────
def _clean_env() -> dict:
    """claude -p with the CLI's own login — not the environment of a Claude
    Code session it might be started from."""
    import os
    keep = ("HOME", "PATH", "USER", "LANG", "TMPDIR", "SHELL")
    return {k: os.environ[k] for k in keep if k in os.environ}


def parse_answer(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in the answer")
    return json.loads(m.group(0))


def cmd_run(args) -> None:
    queue = json.loads(QUEUE.read_text())
    queue = [it for it in queue if (SHEETS / f"{it['id']}.jpg").exists()]
    res = load_results()
    queue = [it for it in queue if res.get(it["id"], {}).get("fingerprint") != fingerprint(it["photos"])]
    if args.limit:
        queue = queue[: args.limit]
    rubric = PROMPT.read_text()
    log = CACHE / "runs.jsonl"
    for b in range(0, len(queue), args.batch):
        batch = queue[b: b + args.batch]
        content = []
        for it in batch:
            ctx = {k: v for k, v in it.items() if k not in ("photos",)}
            content.append({"type": "text", "text": "Listing: " + json.dumps(ctx, ensure_ascii=False)})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.b64encode((SHEETS / f"{it['id']}.jpg").read_bytes()).decode()}})
        content.append({"type": "text", "text": f"Answer for these {len(batch)} listings, in order, as one JSON array."})
        msg = {"type": "user", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None, "session_id": ""}
        t0 = time.time()
        p = subprocess.run(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--model", args.model, "--system-prompt", rubric, "--tools", "",
             "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence"],
            input=json.dumps(msg) + "\n", capture_output=True, text=True, env=_clean_env(),
            cwd=str(CACHE), timeout=900)
        result = None
        for line in p.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result":
                result = d
        if not result or result.get("is_error"):
            print("claude -p failed:", (result or {}).get("result") or p.stderr[-500:] or p.stdout[-500:])
            sys.exit(1)
        answers = parse_answer(result.get("result") or "")
        stored = _store(answers, batch, args.model)
        u = result.get("usage") or {}
        with log.open("a") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "model": args.model,
                                "listings": len(batch), "stored": stored, "seconds": round(time.time() - t0),
                                "usage": {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                                                 "cache_read_input_tokens", "output_tokens")},
                                "cost_equiv_usd": result.get("total_cost_usd")}) + "\n")
        print(f"batch {b // args.batch + 1}: {stored}/{len(batch)} stored · {round(time.time() - t0)} s · "
              f"in {u.get('input_tokens')}+{u.get('cache_creation_input_tokens')}+{u.get('cache_read_input_tokens')} "
              f"out {u.get('output_tokens')}")


# ── ingest / store ───────────────────────────────────────────────────────────
FIELDS = ("photos_usable", "rooms_seen", "condition", "kitchen", "bathroom", "renovation_era",
          "furnishing", "furniture_quality", "light", "space", "red_flags", "deal_check",
          "confidence", "note_it", "note_en")


def _store(answers: list[dict], batch: list[dict], model: str) -> int:
    res = load_results()
    by_id = {it["id"]: it for it in batch}
    n = 0
    for a in answers:
        lid = str(a.get("id", ""))
        it = by_id.get(lid)
        if not it:
            continue
        res[lid] = {**{k: a.get(k) for k in FIELDS}, "fingerprint": fingerprint(it["photos"]),
                    "n_photos": it.get("n_photos"), "model": model,
                    "read_at": datetime.now().isoformat(timespec="seconds")}
        n += 1
    save_results(res)
    return n


def cmd_ingest(args) -> None:
    queue = json.loads(QUEUE.read_text())
    answers = json.loads(Path(args.file).read_text())
    if isinstance(answers, dict):
        answers = answers.get("listings") or list(answers.values())
    print(f"stored {_store(answers, queue, args.model)} of {len(answers)}")


# ── report ───────────────────────────────────────────────────────────────────
def _stated(cond: str | None) -> int | None:
    c = (cond or "").lower()
    if "idealista" in c:
        return None
    for k, v in CONDITION_FROM_LISTING.items():
        if k in c:
            return v
    return None


def cmd_report(args) -> None:
    res = load_results()
    rows = {str(l["id"]): l for l in load_listings()}
    pairs, deal, flags, conf = [], {}, {}, {}
    for lid, r in res.items():
        l = rows.get(lid)
        if not l:
            continue
        deal[r.get("deal_check")] = deal.get(r.get("deal_check"), 0) + 1
        conf[r.get("confidence")] = conf.get(r.get("confidence"), 0) + 1
        for f in r.get("red_flags") or []:
            flags[f] = flags.get(f, 0) + 1
        s = _stated(l.get("condition"))
        if s is not None and isinstance(r.get("condition"), (int, float)):
            pairs.append((s, r["condition"], lid))
    print(f"read: {len(res)} listings")
    print("deal check:", deal)
    print("confidence:", conf)
    print("red flags:", dict(sorted(flags.items(), key=lambda x: -x[1])))
    if pairs:
        exact = sum(1 for s, p, _ in pairs if s == p)
        near = sum(1 for s, p, _ in pairs if abs(s - p) <= 1)
        mean = sum(p - s for s, p, _ in pairs) / len(pairs)
        print(f"vs stated condition ({len(pairs)} listings): exact {exact} ({exact / len(pairs):.0%}), "
              f"within 1 {near} ({near / len(pairs):.0%}), photo − stated {mean:+.2f} on average")
        worse = [lid for s, p, lid in pairs if p <= s - 2]
        if worse:
            print("photos clearly worse than stated:", ", ".join(worse))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select"); s.add_argument("--pilot", type=int); s.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("sheets"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("run"); s.add_argument("--model", default="haiku"); s.add_argument("--batch", type=int, default=8)
    s.add_argument("--limit", type=int)
    s = sub.add_parser("ingest"); s.add_argument("file"); s.add_argument("--model", default="session")
    sub.add_parser("report")
    args = ap.parse_args()
    {"select": cmd_select, "sheets": cmd_sheets, "run": cmd_run, "ingest": cmd_ingest,
     "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
