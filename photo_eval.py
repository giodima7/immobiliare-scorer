"""Read a listing's photos with Claude and record what they show.

Only the best listings are read (score ≥ 90; --gems adds every Hidden
Gem): the point is to check whether a top listing holds up once you look
at the flat — dated kitchen,
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
    python3 photo_eval.py run                   # claude -p (Sonnet, low effort), batches of 8
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
# The user's own verdicts on photos Sonnet read (Details → "Tu cosa ne
# pensi?", api.py /api/photo-feedback). They override Sonnet's for that
# listing (scoring.load_photo_evals) and teach the next readings
# (calibration_text, appended to the rubric). Tracked in git: it's the
# user's labelled data, not a cache.
FEEDBACK = BASE / "photo_feedback_milano.json"
CALIBRATION_MAX = 15

TILE_W, TILE_H = 512, 384
# Every photo the listing has (9 until 2026-10-25 — the user: "look at all
# of them"; 3,169 read listings had more, and a "bilocale" with no bed in
# any photo passed as having a bedroom), 9 to a sheet, numbered across.
MAX_PHOTOS = 45
PER_SHEET = 9
MIN_PHOTOS = 4      # fewer can't show the flat — Idealista cards carry one
                    # photo until its listing page is read (listing_details.py)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")

CONDITION_FROM_LISTING = {   # stated condition → the 1–5 scale of the prompt
    "da ristrutturare": 1, "buono": 3, "abitabile": 3, "ottimo": 4,
    "ristrutturato": 4, "nuovo": 5, "in costruzione": 5,
}


# ── Data ─────────────────────────────────────────────────────────────────────
SALES = BASE / "dashboard" / "milano_sales_latest.json"     # sales_scan.py


def load_listings() -> list[dict]:
    """Rentals and, since 2026-10-18, the sales of the chosen sale areas."""
    for _ in range(5):          # the scanners may be rewriting the file
        try:
            rows = json.loads(DATA.read_text())
            break
        except json.JSONDecodeError:
            time.sleep(2)
    else:
        raise SystemExit("could not read " + str(DATA))
    try:
        rows += json.loads(SALES.read_text())
    except (OSError, ValueError):
        pass
    return rows


def load_results() -> dict:
    try:
        return json.loads(RESULTS.read_text())
    except (OSError, ValueError):
        return {}


def save_results(res: dict) -> None:
    tmp = RESULTS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    tmp.replace(RESULTS)


# ── The user's verdicts ──────────────────────────────────────────────────────
DEALS = ("confirms", "doubt", "contradicts")


def load_feedback() -> dict:
    try:
        return json.loads(FEEDBACK.read_text())
    except (OSError, ValueError):
        return {}


def save_feedback(listing: dict, deal: str | None, note: str = "", grade=None) -> dict | None:
    """Store (or, with no deal and no note, remove) the user's verdict on a
    listing's photos, next to what Sonnet said about the same photos."""
    lid = str(listing["id"])
    fb = load_feedback()
    note = (note or "").strip()[:500]
    if deal not in DEALS and not note:
        fb.pop(lid, None)
        entry = None
    else:
        r = load_results().get(lid) or {}
        try:
            grade = min(10.0, max(1.0, round(float(grade) * 2) / 2)) if grade not in (None, "") else None
        except (TypeError, ValueError):
            grade = None
        entry = {"at": datetime.now().isoformat(timespec="seconds"),
                 "deal": deal if deal in DEALS else None, "note": note, "grade": grade,
                 "fingerprint": fingerprint(photo_urls(listing)),
                 "sonnet": {k: r.get(k) for k in ("deal_check", "confidence", "grade", "note_it", "red_flags")},
                 "context": context(listing)}
        fb[lid] = entry
    tmp = FEEDBACK.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fb, ensure_ascii=False, indent=1))
    tmp.replace(FEEDBACK)
    return entry


def calibration_text(limit: int = CALIBRATION_MAX) -> str:
    """The user's corrections, for the rubric: disagreements first, then
    agreements that say why, newest first."""
    fb = [e for e in load_feedback().values() if (e.get("sonnet") or {}).get("deal_check")]
    if not fb:
        return ""
    fb.sort(key=lambda e: e.get("at") or "", reverse=True)
    fb.sort(key=lambda e: e.get("deal") == e["sonnet"].get("deal_check"))   # stable: newest first within
    lines = []
    for e in fb[:limit]:
        c, s = e.get("context") or {}, e["sonnet"]
        what = ", ".join(f"{k} {v}" for k, v in c.items() if k in ("rent_eur_month", "for_sale_price_eur", "sqm", "rooms", "area") and v)
        said = (f'you said "{s.get("deal_check")}" ({s.get("confidence")})'
                + (f', grade {s["grade"]}' if s.get("grade") is not None else "") + f': {s.get("note_it") or ""}').strip()
        theirs = (f'they said "{e.get("deal") or s.get("deal_check")}"'
                  + (f', grade {e["grade"]}' if e.get("grade") is not None else "")
                  + (f': {e["note"]}' if e.get("note") else ""))
        lines.append(f"- {what} — {said} → {theirs}")
    return ("\n\nCalibration — verdicts of the person these readings are for, who looked at the same "
            "photos (newest first, disagreements first). Judge the way they do:\n" + "\n".join(lines))


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


MIN_SCORE = 70      # photos are read for the best listings only (90, 80, 75 until 2026-10-19; the user: read more)


def is_top(l: dict, min_score: int = MIN_SCORE, gems: bool = False) -> bool:
    """Live, scored ≥ min_score (or a Hidden Gem when gems=True). Idealista
    rows never seen since its scans started stamping dates are left out —
    they are almost always gone."""
    if l.get("is_stale") or l.get("excluded") or l.get("_excluded"):
        return False
    if l.get("source") == "idealista" and not l.get("last_seen_date"):
        return False
    return (l.get("score_total") or 0) >= min_score or (gems and bool(l.get("hidden_gem")))


def context(l: dict) -> dict:
    """What the reader is told about the listing — not its stated condition,
    so the photo judgement stays blind and can be checked against it."""
    floor = l.get("floor_n")
    return {
        "id": str(l["id"]),
        "label": ("Hidden Gem" if l.get("hidden_gem") else
                  "Great Value" if l.get("good_value") else "High score"),
        "area": l.get("neighbourhood"),
        **({"for_sale_price_eur": l.get("price")} if _is_sale(l)
           else {"rent_eur_month": l.get("rent_mo") or l.get("price")}),
        "sqm": l.get("sqm"),
        "rooms": l.get("rooms"),
        "bedrooms": l.get("bedrooms"),
        "floor": floor,
        "lift": l.get("elevator"),
        "discount_vs_nearby_pct": l.get("comps_sale_delta_pct") if _is_sale(l) else l.get("comps_delta_pct"),
        "photos_on_site": l.get("photo_count"),
        # The listing's own words, when a page or Subito gave them: they say
        # what photos don't ("divano letto", "soppalco", "monolocale").
        **({"description": l["description"][:1500]} if l.get("description") else {}),
    }


def _is_sale(l: dict) -> bool:
    return l.get("source") in ("sale", "idealista_sale")


# ── select ───────────────────────────────────────────────────────────────────
def cmd_select(args) -> None:
    rows = load_listings()
    done = load_results()
    tops = [l for l in rows if is_top(l, args.min_score, args.gems)]
    top = [l for l in tops if len(photo_urls(l)) >= MIN_PHOTOS]
    few = [l for l in tops if len(photo_urls(l)) < MIN_PHOTOS]
    print(f"top listings: {len(tops)} · too few photos (< {MIN_PHOTOS}) for now: {len(few)} "
          f"({sum(1 for l in few if l.get('source') == 'idealista')} Idealista)")
    todo = [l for l in top
            if done.get(str(l["id"]), {}).get("fingerprint") != fingerprint(photo_urls(l))]
    if args.pilot:
        rnd = random.Random(args.seed)
        todo = rnd.sample(todo, min(args.pilot, len(todo)))
    try:
        from focus_areas import in_focus     # where the user is searching: first
    except Exception:
        in_focus = lambda l: False
    todo.sort(key=lambda l: (not in_focus(l), -(l.get("score_total") or 0)))
    # Then readings of an older rubric: every one since 2026-10-19 (the user
    # wants the grade everywhere; ~776 then, ~1.5M tokens) — where the user
    # searches and the labelled deals first.
    seen_ids = {str(l["id"]) for l in todo}
    again = [l for l in top if str(l["id"]) not in seen_ids
             and (outdated(done.get(str(l["id"]))) or missed_words(done.get(str(l["id"])), l)
                  or needs_bedrooms(done.get(str(l["id"])), l))]
    # Where the user searches, then labelled deals and the ones a photo
    # verdict capped (they may get their label back), then the rest.
    again.sort(key=lambda l: (not in_focus(l),
                              not (l.get("good_value") or l.get("hidden_gem") or l.get("photo_deal") == "contradicts"),
                              -(l.get("score_total") or 0)))
    todo += again
    CACHE.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps([{**context(l), "photos": photo_urls(l),
                                  "floorplans": (l.get("floorplans") or [])[:4]} for l in todo],
                                ensure_ascii=False, indent=1))
    print(f"readable: {len(top)} · to read: {len(todo)} ({len(again)} again, older rubric) → {QUEUE}")


# ── sheets ───────────────────────────────────────────────────────────────────
def _fetch(url: str) -> bytes | None:
    # Some Immobiliare photos lack the 640 px variant: fall back to larger ones.
    tries = [url] + ([url.replace("/xs-c.jpg", v) for v in ("/s-c.jpg", "/m-c.jpg")]
                     if url.endswith("/xs-c.jpg") else [])
    referer = ("https://www.idealista.it/" if "idealista" in url else
               "https://www.subito.it/" if "sbito.it" in url else "https://www.immobiliare.it/")
    for u in tries:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": referer})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception:
            time.sleep(0.5)
    return None


def sheet_paths(item: dict) -> list[Path]:
    """The listing's sheets, 9 photos each — named after the photos they
    show, so a changed gallery gets new ones."""
    fp, n = fingerprint(item["photos"]), -(-len(item["photos"]) // PER_SHEET)
    return [SHEETS / f"{item['id']}-{fp}-{k + 1}.jpg" for k in range(n)]


def plan_path(item: dict) -> Path | None:
    """The floor plan sheet, when the listing page gave plans."""
    plans = item.get("floorplans") or []
    return SHEETS / f"{item['id']}-{fingerprint(plans)}-plan.jpg" if plans else None


def _sheets_ready(item: dict) -> bool:
    paths = sheet_paths(item)
    return bool(paths) and all(p.exists() for p in paths)


def build_sheet(urls: list[str], dest: Path, first: int = 1) -> int:
    """Numbered grid of photos (numbers from `first`): 2×2, 3×2 or 3×3 tiles."""
    from PIL import Image, ImageDraw, ImageFont
    imgs = []
    for u in urls:
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
        label = str(first + i)
        draw.rectangle([x + 6, y + 6, x + 22 + 16 * len(label), y + 42], fill=(0, 0, 0))
        draw.text((x + 15, y + 9), label, fill=(255, 255, 255), font=font)
    sheet.save(dest, "JPEG", quality=80)
    return n


def _build_sheets(item: dict, force: bool = False) -> int:
    """Every sheet of the listing (and its floor plan); returns how many
    were built now. item["n_photos"]: photos on its sheets."""
    made, shown = 0, 0
    for k, dest in enumerate(sheet_paths(item)):
        chunk = item["photos"][k * PER_SHEET:(k + 1) * PER_SHEET]
        if dest.exists() and not force:
            shown += len(chunk)
            continue
        got = build_sheet(chunk, dest, k * PER_SHEET + 1)
        if not got:                      # nothing downloaded: leave no empty sheet behind
            continue
        shown += got
        made += 1
    plan = plan_path(item)
    if plan and (force or not plan.exists()):
        build_sheet(item["floorplans"][:4], plan)
        made += 1
    item["n_photos"] = shown
    return made


def _drop_sheets(item: dict) -> None:
    """Sheets are only needed for the reading: ~200 KB each, ~3 a listing."""
    for p in sheet_paths(item) + [plan_path(item)]:
        if p:
            p.unlink(missing_ok=True)


def cmd_sheets(args) -> None:
    queue = json.loads(QUEUE.read_text())
    SHEETS.mkdir(parents=True, exist_ok=True)
    made = 0
    for i, item in enumerate(queue, 1):
        made += _build_sheets(item, args.force)
        if i % 10 == 0:
            print(f"  {i}/{len(queue)}")
    QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    ok = sum(1 for it in queue if _sheets_ready(it))
    print(f"sheets: {ok}/{len(queue)} ready ({made} built now) in {SHEETS}")


# ── run (claude -p) ──────────────────────────────────────────────────────────
def _clean_env() -> dict:
    """claude -p with the CLI's own login — not the environment of a Claude
    Code session it might be started from."""
    import os
    keep = ("HOME", "PATH", "USER", "LANG", "TMPDIR", "SHELL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    local_bin = str(Path.home() / ".local" / "bin")     # where the CLI installs itself
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = env.get("PATH", "/usr/bin:/bin") + ":" + local_bin
    return env


def _ask_claude(msg: dict, system_prompt: str, model: str, effort: str = "low",
                timeout: float = 900) -> dict | None:
    """One user message (text + images) to `claude -p`, no tools, our own
    system prompt. stdin must stay open until the result arrives — the CLI
    stops reading as soon as it is closed."""
    p = subprocess.Popen(
        ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
         "--model", model, "--effort", effort, "--system-prompt", system_prompt, "--tools", "",
         # Extended thinking roughly doubles the tokens for a fill-in-the-form
         # answer; the rubric does the reasoning.
         "--settings", json.dumps({"disableAllHooks": True, "alwaysThinkingEnabled": False}),
         "--no-session-persistence"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_clean_env(), cwd=str(CACHE))
    result = None
    deadline = time.time() + timeout
    try:
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        for line in p.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result":
                result = d
                break
            if time.time() > deadline:
                break
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    if result is None:
        result = {"is_error": True, "result": (p.stderr.read() if p.stderr else "")[-500:] or "no result"}
    return result


def parse_answer(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in the answer")
    # strict=False: a note quoting the listing's description can carry a raw
    # line break inside a string. Then the slips seen in answers: a stray
    # ',"' before a closing brace (129900526, 2026-10-25), trailing commas.
    raw = m.group(0)
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        fixed = re.sub(r',\s*"\s*([}\]])', r'\1', raw)
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        return json.loads(fixed, strict=False)


def _content(it: dict) -> list:
    """One listing in a message: its facts, every sheet, its floor plan."""
    ctx = {k: v for k, v in it.items() if k not in ("photos", "floorplans")}
    out = [{"type": "text", "text": "Listing: " + json.dumps(ctx, ensure_ascii=False)}]
    for p in sheet_paths(it):
        out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(p.read_bytes()).decode()}})
    plan = plan_path(it)
    if plan and plan.exists():
        out.append({"type": "text", "text": "Floor plan of the same listing:"})
        out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(plan.read_bytes()).decode()}})
    return out


def _batches(queue: list, max_listings: int, max_images: int = 18) -> list[list]:
    """Up to max_listings a call, fewer when they carry many sheets."""
    out, cur, imgs = [], [], 0
    for it in queue:
        n = len(sheet_paths(it)) + (1 if plan_path(it) else 0)
        if cur and (len(cur) >= max_listings or imgs + n > max_images):
            out.append(cur)
            cur, imgs = [], 0
        cur.append(it)
        imgs += n
    return out + ([cur] if cur else [])


def cmd_run(args) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    queue = json.loads(QUEUE.read_text())
    queue = [it for it in queue if _sheets_ready(it)]
    res = load_results()
    queue = [it for it in queue if res.get(it["id"], {}).get("fingerprint") != fingerprint(it["photos"])
             or outdated(res.get(it["id"])) or missed_words(res.get(it["id"]), it)
             or needs_bedrooms(res.get(it["id"]), it)]
    if args.limit:
        queue = queue[: args.limit]
    rubric = PROMPT.read_text() + calibration_text()
    log = CACHE / "runs.jsonl"
    store_lock = threading.Lock()
    failed = []

    def one(n: int, batch: list) -> None:
        content = [c for it in batch for c in _content(it)]
        content.append({"type": "text", "text": f"Answer for these {len(batch)} listings, in order, as one JSON array."})
        msg = {"type": "user", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None, "session_id": ""}
        t0 = time.time()
        result = _ask_claude(msg, rubric, args.model, args.effort)
        if not result or result.get("is_error"):
            print("claude -p failed:", (result or {}).get("result"))
            failed.append(n)
            return
        try:
            answers = parse_answer(result.get("result") or "")
        except ValueError as exc:
            print(f"batch {n}: {exc}")
            failed.append(n)
            return
        with store_lock:
            stored = _store(answers, batch, args.model)
            u = result.get("usage") or {}
            with log.open("a") as f:
                f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "model": args.model,
                                    "listings": len(batch), "stored": stored, "seconds": round(time.time() - t0),
                                    "usage": {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                                                     "cache_read_input_tokens", "output_tokens")},
                                    "cost_equiv_usd": result.get("total_cost_usd")}) + "\n")
        for it in batch:
            _drop_sheets(it)
        print(f"batch {n}: {stored}/{len(batch)} stored · {round(time.time() - t0)} s · "
              f"in {u.get('input_tokens')}+{u.get('cache_creation_input_tokens')}+{u.get('cache_read_input_tokens')} "
              f"out {u.get('output_tokens')}", flush=True)

    batches = _batches(queue, args.batch)
    # Calls in parallel (`workers`): the one-session re-read of 2026-10-25.
    with ThreadPoolExecutor(max_workers=max(1, getattr(args, "workers", 1))) as pool:
        list(pool.map(lambda nb: one(*nb), enumerate(batches, 1)))
    if failed and len(failed) == len(batches):
        raise SystemExit("claude -p failed")


# ── ingest / store ───────────────────────────────────────────────────────────
FIELDS = ("photos_usable", "rooms_seen", "bedrooms", "features", "tags", "condition", "kitchen", "bathroom", "renovation_era",
          "furnishing", "furniture_quality", "light", "space", "grade", "red_flags", "deal_check",
          "confidence", "note_it", "note_en")
# The rubric's version: 2 (2026-10-18) added the 1–10 grade and the attic /
# no-bedroom / tiny-kitchen / rooms-not-shown flags, after the user's
# examples. Readings of an older version are re-read (focus areas and
# labelled listings first, after the unread ones — cmd_select).
RUBRIC_VERSION = 5
# 3 (2026-10-19): unseen rooms lower the confidence, not the grade (the user:
# "not shown is missing information, why penalise it"). Only v2 readings
# that flagged rooms_not_shown / few_photos are read again (1,374 of 1,803).
INFO_FLAGS = {"rooms_not_shown", "few_photos"}


# 4 (2026-10-25): every photo (was the first 9), the floor plan and the
# description when known, and "no bed in any photo" counts as no bedroom
# (Immobiliare 129426444: sofa in the living room, a desk in the "bedroom",
# read as "separate bedroom visible"). Every older reading is read again.


# 5 (2026-10-25): bedrooms counted only where a bed or the plan shows one,
# plus features the listing doesn't state (bathrooms, balcony, AC, kitchen
# type…) and up to 8 tags from a fixed list — what an agent or a surveyor
# points out (big living room, old windows, windowless bathroom, damp, plan
# not matching the photos…). Every older reading is read again.


def missed_words(r: dict | None, l: dict) -> bool:
    """Read before the listing's description or floor plan was known — they
    arrive with the night's page reads (129900526: "letto King" in the
    ingresso, written only in the description; read from photos alone as a
    real bedroom)."""
    if not r or r.get("by_user"):
        return False
    return bool((l.get("description") and not r.get("saw_text"))
                or (l.get("floorplans") and not r.get("saw_plan")))


def needs_bedrooms(r: dict | None, l: dict) -> bool:
    """Read before Sonnet counted bedrooms, for a flat of 3+ rooms whose site
    doesn't say how many (Idealista: 1,424 trilocali and up; 8 % of the
    trilocali that do say have one bedroom — id_37182070 was proposed to
    share as two)."""
    return bool(r) and not r.get("by_user") and "bedrooms" not in r \
        and (l.get("rooms") or 0) >= 3 and l.get("bedrooms") is None


def outdated(r: dict | None) -> bool:
    """A reading the current rubric would answer differently."""
    if not r:
        return False
    return r.get("rubric", 1) < RUBRIC_VERSION


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
                    "n_photos": it.get("n_photos"), "model": model, "rubric": RUBRIC_VERSION,
                    "saw_text": bool(it.get("description")), "saw_plan": bool(it.get("floorplans")),
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


# ── Nightly ──────────────────────────────────────────────────────────────────
NIGHTLY_MAX = 40        # listings per call (5 claude calls; ~2k tokens each)
# Listings read per day, all callers together: claude -p draws on the user's
# weekly plan (2026-10-25: 40 % of the week used, "split it up"). New and
# changed listings come first (cmd_select), re-reads after.
DAILY_MAX = 150


def read_today() -> int:
    today = datetime.now().date().isoformat()
    return sum(1 for r in load_results().values()
               if not r.get("by_user") and str(r.get("read_at") or "").startswith(today))


def nightly(log=print, limit: int = NIGHTLY_MAX) -> int:
    """Queue score ≥ MIN_SCORE + Hidden Gems not read yet (or whose photos
    changed), best first, and read up to `limit`. Only those get a sheet:
    at 80 the queue held ~630 listings (~5.7k photo downloads). Returns how
    many answers were stored. Never raises — a failed login or a network
    hiccup just skips this call."""
    import fcntl
    import types
    # One reading at a time (the Idealista daemon after each slot, a manual
    # run): two would pick the same unread listings and pay for both.
    CACHE.mkdir(exist_ok=True)
    lock = open(CACHE / ".reading.lock", "w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("[photos] another reading is running — skipped")
        lock.close()
        return 0
    try:
        left = DAILY_MAX - read_today()
        if left <= 0:
            log(f"[photos] daily budget used ({DAILY_MAX}) — next reading tomorrow")
            return 0
        limit = min(limit, left)
        before = len(load_results())
        cmd_select(types.SimpleNamespace(pilot=None, seed=7, min_score=MIN_SCORE, gems=True))
        queue = json.loads(QUEUE.read_text())
        if not queue:
            return 0
        QUEUE.write_text(json.dumps(queue[:limit], ensure_ascii=False, indent=1))
        cmd_sheets(types.SimpleNamespace(force=False))
        cmd_run(types.SimpleNamespace(model="sonnet", batch=8, limit=limit, effort="low"))
        stored = len(load_results()) - before
        log(f"[photos] read {stored} listing(s)")
        return max(stored, 0)
    except SystemExit as exc:          # cmd_run exits on a failed claude -p call
        log(f"[photos] skipped: {exc}")
    except Exception as exc:
        log(f"[photos] skipped: {type(exc).__name__}: {exc}")
    finally:
        lock.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select"); s.add_argument("--pilot", type=int); s.add_argument("--seed", type=int, default=7)
    s.add_argument("--min-score", type=int, default=MIN_SCORE); s.add_argument("--gems", action="store_true",
                   help="also every Hidden Gem below the score bar")
    s = sub.add_parser("sheets"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("run"); s.add_argument("--model", default="sonnet"); s.add_argument("--batch", type=int, default=8)
    s.add_argument("--effort", default="low")
    s.add_argument("--limit", type=int); s.add_argument("--workers", type=int, default=1)
    s = sub.add_parser("ingest"); s.add_argument("file"); s.add_argument("--model", default="session")
    sub.add_parser("report")
    args = ap.parse_args()
    {"select": cmd_select, "sheets": cmd_sheets, "run": cmd_run, "ingest": cmd_ingest,
     "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
