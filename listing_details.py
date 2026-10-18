"""Listing-page details — the fields search results don't carry.

Search pages give price, size, rooms and (Immobiliare) the feature list.
Condition, heating, energy class, year built, bathrooms and condo fees for
Idealista, and energy class / year / condo fees / contract type for
Immobiliare, only exist on each listing's own page. Fetching ~10k pages
at once would get the scanners blocked, so the scanner daemons drip them
in while they wait for the next daily scan: a few pages every ~20 min
during the day, the listings that could earn a label first.

Results live in their own file (DETAIL_CACHE_PATH, keyed by listing id)
instead of being written into the shared listings file: three processes
already rewrite that file, and a re-scan replaces whole rows. score_all()
applies the cache on every scoring run (apply_details), so whichever path
rescored last, the details are there.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import random
import re
import time
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DETAIL_CACHE_PATH = BASE_DIR / "detail_cache_milano.json"
RENTALS_PATH = BASE_DIR / "dashboard" / "milano_rentals_latest.json"
SALES_PATH = BASE_DIR / "dashboard" / "milano_sales_latest.json"     # sales_scan.py
SALE_SOURCE = {"immobiliare": "sale", "idealista": "idealista_sale"}

# Fields a listing page can supply. apply_details() only fills gaps, with
# two exceptions below — fresh search data wins otherwise.
DETAIL_KEYS = ("condition", "heating_type", "energy_class", "year_built",
               "bathrooms", "has_balcony", "has_parking", "furnished",
               "is_external", "spese_condominiali", "photo_count",
               "contract_type", "availability", "published_date",
               "air_conditioning", "heating_mo", "condo_included",
               "deposit_months", "deposit_eur")

# Idealista search cards only ever set these to True (keyword found in the
# snippet) — never False. A listing page's yes/no answer replaces them.
CARD_YES_ONLY = ("has_balcony", "has_parking", "furnished")

# ── Drip pacing (per source) ──────────────────────────────────────────────────
# Pace. 150 pages a day until 2026-10-17 (~40 days to read the visible
# listings once), then a fixed 10 every 8–15 min — a guess, never measured.
# Now the drip finds its own pace: it starts at DRIP_START_LEVEL, moves up a
# level after DRIP_STEP_UP_SEC without trouble, steps down when a third of
# a session fails, and on a block page halves its level and pauses (1 h,
# 6 h for a second block within 6 h) — just below the sites' limit instead
# of at it (a block also hits the user's own browsing from this IP).
DRIP_LEVELS = [          # (pages per browser session, minutes between sessions)
    (10, (8, 15)), (20, (4, 8)), (30, (3, 5)), (40, (2, 3)), (50, (1, 2)),
]
DRIP_START_LEVEL  = 1
DRIP_STEP_UP_SEC  = 2 * 3600
DRIP_PER_DAY      = 3000         # safety cap per source per day
DRIP_HOURS        = (0, 24)      # all day — the window stays minimized
DRIP_PAGE_GAP_SEC = (5, 10)      # seconds between pages in a session
DRIP_QUEUE_TTL    = 30 * 60      # rebuild the queue: new top listings jump in
PRIORITY_Z        = -0.8         # price_z at or below this → fetched first
NEW_DAYS          = 3            # first seen within this many days → next


# ── Cache I/O ─────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    try:
        return json.loads(DETAIL_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_entries(entries: dict) -> None:
    """Merge {id: entry} into the cache under a lock, atomically."""
    if not entries:
        return
    lock_path = DETAIL_CACHE_PATH.with_suffix(".json.lock")
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        cache = load_cache()
        cache.update(entries)
        tmp = DETAIL_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, DETAIL_CACHE_PATH)


# ── Applying details to listings (called from scoring.score_all) ─────────────

def _condition_unknown(value) -> bool:
    from comps_engine import condition_group
    return condition_group({"condition": value}) == "unknown"


def apply_details(listings: list, cache: dict | None = None) -> int:
    """Fill listing fields from the detail cache. Returns rows touched."""
    cache = load_cache() if cache is None else cache
    if not cache:
        return 0
    n = 0
    for l in listings:
        e = cache.get(str(l.get("id")))
        if not e:
            continue
        if e.get("gone"):
            # The site's own "no longer available" page — unless a scan saw
            # the listing again afterwards (relisted / search lag).
            if (e.get("fetched_at") or "") >= (l.get("last_seen_date") or ""):
                l["is_stale"] = True
                l["gone_checked"] = e.get("fetched_at")
            continue
        for k, v in (e.get("fields") or {}).items():
            if v is None:
                continue
            cur = l.get(k)
            if (cur is None
                    or (k in CARD_YES_ONLY and l.get("source") == "idealista")
                    or (k == "condition" and _condition_unknown(cur))
                    or (k == "photos" and len(v) > len(cur or []))):
                l[k] = v
        l["detail_fetched_at"] = e.get("fetched_at")
        if l.get("days_on_market") is None and l.get("published_date"):
            try:
                l["days_on_market"] = (date.today() - date.fromisoformat(l["published_date"])).days
            except ValueError:
                pass
        n += 1
    return n


# Same flat on both sites (comps_engine duplicate groups): copy what one copy
# knows into the other. Idealista card flags are not real answers.
TWIN_KEYS = ("condition", "heating_type", "energy_class", "year_built",
             "bathrooms", "has_balcony", "has_parking", "furnished",
             "is_external", "spese_condominiali")


def _has_real_value(l: dict, k: str) -> bool:
    v = l.get(k)
    if v is None:
        return False
    if k == "condition" and _condition_unknown(v):
        return False
    if k in CARD_YES_ONLY and l.get("source") == "idealista" and not l.get("detail_fetched_at"):
        return False
    return True


def fill_from_twins(pool: list, comps_index: dict) -> int:
    """Fill gaps from the listing's duplicates. Returns rows touched."""
    groups, group_of = comps_index.get("groups") or [], comps_index.get("group_of") or {}
    by_obj = {id(l): l for l in pool}
    n = 0
    from comps_engine import relist_chain
    for l in pool:
        g = group_of.get(id(l))
        twins = [by_obj[i] for i in groups[g] if i != id(l) and i in by_obj] if g is not None else []
        twins += relist_chain(l, comps_index)
        if not twins:
            continue
        filled = []
        for k in TWIN_KEYS:
            if _has_real_value(l, k):
                continue
            for t in twins:
                if _has_real_value(t, k):
                    l[k] = t[k]
                    filled.append(k)
                    break
        if filled:
            l["twin_filled"] = filled
            n += 1
    return n


def stamp_relistings(listings: list, pool: list, comps_index: dict) -> int:
    """relisted_from_rent / _date / _id: the same flat was listed before at
    another rent and that listing is gone (comps_engine relisting links:
    same rooms/size/floor/building/street, one after the other). Cleared on
    every run so a stale stamp never survives. Returns the count."""
    prev = comps_index.get("relist_prev") or {}
    n = 0
    for l in listings:
        l["relisted_from_rent"] = l["relisted_from_date"] = l["relisted_from_id"] = None
        if l.get("is_stale") or not l.get("rent_mo"):
            continue
        before = [m for m in prev.get(id(l), []) if m.get("rent_mo") and m["rent_mo"] != l["rent_mo"]]
        if before:
            p = max(before, key=lambda m: m.get("last_seen_date") or "")
            l["relisted_from_rent"] = p["rent_mo"]
            l["relisted_from_date"] = p.get("last_seen_date")
            l["relisted_from_id"] = str(p["id"])
            n += 1
    return n


# ── Parsing listing pages ─────────────────────────────────────────────────────

def _contract_type(value) -> str | None:
    s = str(value or "").lower()
    if not s:
        return None
    for pat, label in ((r"4\s*\+\s*4", "4+4"), (r"3\s*\+\s*2", "3+2"),
                       (r"transitori", "transitorio"), (r"student", "studenti")):
        if re.search(pat, s):
            return label
    return None


def parse_immobiliare_detail(next_data: dict) -> dict | None:
    """Fields from an Immobiliare listing page's __NEXT_DATA__."""
    from fetch_rentals import parse_rental
    try:
        re_data = next_data["props"]["pageProps"]["detailData"]["realEstate"]
    except (KeyError, TypeError):
        return None
    parsed = parse_rental({"realEstate": re_data}) or {}
    props = re_data.get("properties") or [{}]
    prop = (props[0] if isinstance(props, list) else props) or {}
    fields = {k: parsed.get(k) for k in DETAIL_KEYS if parsed.get(k) is not None}
    fields["contract_type"] = _contract_type(re_data.get("contractValue"))
    fields["availability"] = (str(prop.get("availability")).strip().lower()
                              if prop.get("availability") else None)
    # The full gallery (search results carry 8) — photo_eval.py reads it.
    from fetch_rentals import parse_photo_urls
    raw = (prop.get("multimedia") or {}).get("photos") or (re_data.get("multimedia") or {}).get("photos")
    gallery = parse_photo_urls(raw, cap=20)
    if len(gallery) > 8:
        fields["photos"] = gallery
    fields["costs_read"] = 1     # heating / deposit / included parsed since 2026-10-17
    return {k: v for k, v in fields.items() if v is not None}


# Idealista energy icons: icon-energy-c-1 … c-7 = A … G (c-7 seen with
# 272 kWh/m²·year, which is class G territory). Letter suffixes are kept as is.
_IDEALISTA_ENERGY = {str(i): c for i, c in enumerate("ABCDEFG", start=1)}

_IDEALISTA_EXTRACT_JS = r"""JSON.stringify({
  title: document.title || '',
  head: (document.body ? document.body.innerText : '').slice(0, 4000),
  features: Array.from(document.querySelectorAll('.details-property_features li')).map(e => e.innerText.trim()),
  energy: (document.querySelector('.details-property_features [class*="icon-energy-"]') || {}).className || '',
  comment: ((document.querySelector('.comment') || {}).innerText || '').slice(0, 4000),
  // "Spese condominiali: 160 €/mese", "Cauzione di 3 mesi" — on every page.
  priceBox: Array.from(document.querySelectorAll('.price-features__container .flex-feature'))
                 .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()),
  photos: (() => {
    // The whole gallery sits in one inline script (multimediaCarrousel /
    // fullScreenGalleryPics); the DOM holds only the first picture or two.
    const rx = /https?:\/\/img\d?\.idealista\.it\/[^"'\s<>)]+?image\.master\/[^"'\s<>)]+?\.(?:jpg|webp)/g;
    const script = Array.from(document.scripts).map(x => (x.textContent || '').replace(/\\\//g, '/'))
                        .find(t => t.includes('multimediaCarrousel') || t.includes('fullScreenGalleryPics'));
    const dom = Array.from(document.querySelectorAll('.detail-image img, .main-image img, picture img'))
                     .map(i => i.getAttribute('data-ondemand-img') || i.getAttribute('data-src') || i.src)
                     .filter(Boolean);
    const out = [], seen = new Set();
    for (const u of ((script && script.match(rx)) || []).concat(dom)) {
      const k = (u.match(/image\.master\/(.+?)\.(?:jpg|webp)/) || [null, u])[1];
      if (!seen.has(k)) { seen.add(k); out.push(u); }
    }
    return out.slice(0, 40);
  })()
})"""

_RENOVATED_ADVERB = re.compile(
    r"\b(complet|total|interament|integralment|recentement|appena|finement|nuovament)\w*\s+"
    r"(?:e\s+\w+\s+)?ristrutturat[oa]\b", re.I)
_RENOVATED_FLAT = re.compile(
    r"\b(appartamento|immobile|bilocale|trilocale|quadrilocale|monolocale|attico|loft)\b"
    r"[^.]{0,40}\bristrutturat[oa]\b", re.I)


def _says_renovated(text: str) -> bool:
    """The whole flat renovated — not "bagno ristrutturato" or "da ristrutturare"."""
    for m in list(_RENOVATED_ADVERB.finditer(text)) + list(_RENOVATED_FLAT.finditer(text)):
        before = text[max(0, m.start() - 25):m.start()].lower()
        if re.search(r"\b(bagn[oi]|cucina)\b", before + m.group(0).lower().split("ristruttur")[0]):
            continue
        return True
    return False


def parse_idealista_detail(page: dict) -> dict:
    """Fields from the listing page's feature list + description."""
    from fetch_rentals import _parse_condo_from_description
    feats = [f.lower() for f in page.get("features") or []]
    text = " | ".join(feats)
    comment = page.get("comment") or ""
    out: dict = {}

    m = re.search(r"(\d+)\s+bagn", text)
    if m:
        out["bathrooms"] = int(m.group(1))
    out["has_balcony"] = any(re.search(r"\b(balcon|terrazz)", f) for f in feats)
    out["has_parking"] = any(re.search(r"\b(box|garage|posto auto|parcheggio)", f) for f in feats)
    if any(re.search(r"\bnon (ammobiliato|arredato)", f) for f in feats):
        out["furnished"] = False
    elif any(re.search(r"\b(ammobiliato|arredato)", f) for f in feats):
        out["furnished"] = True
    if any(f.strip() in ("esterno", "esposizione esterna") for f in feats):
        out["is_external"] = True
    elif any(f.strip() in ("interno", "esposizione interna") for f in feats):
        out["is_external"] = False
    m = re.search(r"costruito nel (\d{4})", text)
    if m:
        out["year_built"] = int(m.group(1))
    if re.search(r"riscaldamento autonomo", text):
        out["heating_type"] = "autonomous"
    elif re.search(r"riscaldamento centralizzato", text):
        out["heating_type"] = "centralised"
    elif re.search(r"senza riscaldamento|non dispone di riscaldamento", text):
        out["heating_type"] = "none"
    if any("aria condizionata" in f for f in feats):
        out["air_conditioning"] = True

    # Idealista has three states only; "buono stato" also covers renovated
    # flats, so it maps to renovated only when the description says so and
    # otherwise stays undetermined for the benchmark (comps_engine
    # condition_group → 'unknown').
    if "nuova costruzione" in text:
        out["condition"] = "Nuovo / In costruzione"
    elif "da ristrutturare" in text:
        out["condition"] = "Da ristrutturare"
    elif "buono stato" in text:
        out["condition"] = ("Ottimo / Ristrutturato" if _says_renovated(comment)
                            else "Buono stato (Idealista)")

    m = re.search(r"icon-energy-c-(\w+)", page.get("energy") or "")
    if m:
        out["energy_class"] = _IDEALISTA_ENERGY.get(m.group(1), m.group(1).upper())

    box = " | ".join(page.get("priceBox") or [])
    m = re.search(r"spese condominiali:?\s*([\d.]+)\s*€", box, re.I)
    fee = int(m.group(1).replace(".", "")) if m else _parse_condo_from_description(comment)
    if fee:
        out["spese_condominiali"] = fee
    from fetch_rentals import condo_included
    if (re.search(r"spese condominiali:?\s*inclus", box, re.I)
            or (not fee and condo_included(comment))):
        out["condo_included"] = True
    m = re.search(r"cauzione di (\d+) mes", box, re.I)
    if m:
        out["deposit_months"] = int(m.group(1))
    else:
        m = re.search(r"cauzione:?\s*([\d.]+)\s*€", box, re.I)
        if m:
            out["deposit_eur"] = int(m.group(1).replace(".", ""))
    photos = page.get("photos")
    if isinstance(photos, list):
        # The gallery (search cards carry one photo) — photo_eval.py reads it.
        gallery = [u for u in photos if isinstance(u, str) and "image.master" in u]
        out["photo_count"] = len(gallery) or len(photos)
        # Read from the page script since 2026-10-17; before, only the 1–2
        # pictures in the DOM — build_queue fetches those listings again.
        out["gallery_read"] = 1
        if gallery:
            out["photos"] = gallery[:20]
    elif photos:
        out["photo_count"] = int(photos)
    out["costs_read"] = 1        # price box (condo, deposit) read since 2026-10-17
    return out


# ── Priority queue ────────────────────────────────────────────────────────────

def build_queue(source: str, cache: dict | None = None,
                rentals_path: Path = RENTALS_PATH) -> list:
    """[(id, url)] still to fetch for `source`, most useful first: every
    listing in a focus area (focus_areas.json — where the user is searching:
    each needs its real monthly cost), then listings at photo_eval's bar
    (score ≥ MIN_SCORE, or Hidden Gem); both only while their page wasn't
    read since costs are parsed (and, on Idealista, the full gallery), best
    first — with the sales of the chosen sale areas (sales_scan.py: condo
    fees, gallery) right after the focus areas. Then, postponed: possible
    deals (price_z ≤ PRIORITY_Z, lowest first), listings first seen in the
    last NEW_DAYS days, the rest."""
    cache = load_cache() if cache is None else cache
    try:
        rows = json.loads(rentals_path.read_text())
    except (OSError, ValueError):
        return []
    try:
        from photo_eval import MIN_SCORE as top_score
    except Exception:
        top_score = 75
    try:
        from focus_areas import in_focus
    except Exception:
        in_focus = lambda l: False
    today = date.today()
    focus, top, deals, new, rest = [], [], [], [], []
    for l in rows:
        if (l.get("source") or "immobiliare") != source:
            continue
        if l.get("is_stale") or l.get("excluded") or not l.get("url"):
            continue
        lid = str(l.get("id"))
        entry = cache.get(lid) or {}
        if not entry.get("gone") and not (entry.get("fields") or {}).get("costs_read"):
            if in_focus(l):
                focus.append((-(l.get("score_total") or 0), (lid, l["url"])))
                continue
            if (l.get("score_total") or 0) >= top_score or l.get("hidden_gem"):
                top.append((-(l.get("score_total") or 0), (lid, l["url"])))
                continue
        if lid in cache or (source == "immobiliare" and l.get("detail_fetched_at")):
            continue
        z = l.get("price_z")
        try:
            age = (today - date.fromisoformat(l.get("first_seen_date") or "")).days
        except ValueError:
            age = 999
        item = (str(l["id"]), l["url"])
        if z is not None and z <= PRIORITY_Z:
            deals.append((z, item))
        elif age <= NEW_DAYS:
            new.append((age, item))
        else:
            rest.append((z if z is not None else 9, item))
    sales = []
    try:
        sale_rows = json.loads(SALES_PATH.read_text())
    except (OSError, ValueError):
        sale_rows = []
    for l in sale_rows:
        if l.get("source") != SALE_SOURCE.get(source) or l.get("is_stale") or l.get("excluded") or not l.get("url"):
            continue
        entry = cache.get(str(l.get("id"))) or {}
        if not entry.get("gone") and not (entry.get("fields") or {}).get("costs_read"):
            sales.append((-(l.get("score_total") or 0), (str(l["id"]), l["url"])))
    return ([i for _, i in sorted(focus)] + [i for _, i in sorted(sales)]
            + [i for _, i in sorted(top)] + [i for _, i in sorted(deals)]
            + [i for _, i in sorted(new)] + [i for _, i in sorted(rest)])


# ── Fetching ──────────────────────────────────────────────────────────────────

_GONE_RX = re.compile(r"non è più (disponibile|pubblicato)|non è presente sul nostro sito", re.I)
# Idealista's bot check says "Verifica del dispositivo…" (2026-10-17/18:
# not matched, so ~240 blocked pages counted as plain failures overnight).
_BLOCK_RX = re.compile(r"captcha|datadome|accesso .{0,20}limitat|temporaneamente (bloccat|limitat|restrict)"
                       r"|access is temporarily restricted|verifica di essere|verifica del dispositivo"
                       r"|verifying (your )?device|device verification|sei un robot|are you a robot", re.I)

_PAGE_STATE_JS = r"""JSON.stringify({
  title: document.title || '',
  text: (document.body ? document.body.innerText : '').slice(0, 4000),
  next: window.__NEXT_DATA__ || null
})"""


async def _fetch_one(browser, source: str, lid: str, url: str) -> tuple[str, dict | None]:
    """→ ("ok", fields) | ("gone", None) | ("blocked", None) | ("error", None).
    "gone" only on the sites' own removed-listing notice; anything
    unrecognised is an error and the listing is retried another day."""
    from browser_session import timed
    tab = await browser.get(url)
    await asyncio.sleep(random.uniform(2.5, 4.5))
    if source == "immobiliare":
        raw = await timed(tab.evaluate(_PAGE_STATE_JS), what="read detail page")
        st = json.loads(raw) if raw else {}
        if st.get("next"):
            fields = parse_immobiliare_detail(st["next"])
            if fields is not None:
                return "ok", fields
        text = st.get("title", "") + " " + st.get("text", "")
        if _GONE_RX.search(text):
            return "gone", None
        return ("blocked", None) if _BLOCK_RX.search(text) else ("error", None)
    raw = await timed(tab.evaluate(_IDEALISTA_EXTRACT_JS), what="read idealista detail")
    page = json.loads(raw) if raw else {}
    if page.get("features"):
        return "ok", parse_idealista_detail(page)
    text = page.get("title", "") + " " + page.get("head", "")
    if _GONE_RX.search(text):
        return "gone", None
    return ("blocked", None) if _BLOCK_RX.search(text) else ("error", None)


async def fetch_batch(source: str, items: list, log=print) -> dict:
    """Fetch listing pages with one browser session. Returns stats."""
    from browser_session import BrowserSession, PageTimeout
    stats = {"ok": 0, "gone": 0, "error": 0, "blocked": False}
    if not items:
        return stats
    browser = BrowserSession(source, background=True)   # runs all day: keep it out of sight
    await browser.start()
    entries: dict = {}
    try:
        for i, (lid, url) in enumerate(items):
            if i:
                await asyncio.sleep(random.uniform(*DRIP_PAGE_GAP_SEC))
            try:
                status, fields = await _fetch_one(browser, source, lid, url)
            except PageTimeout:
                await browser.restart()
                stats["error"] += 1
                continue
            except Exception:
                stats["error"] += 1
                continue
            if status == "blocked":
                stats["blocked"] = True
                break
            if status == "error":
                stats["error"] += 1
                continue
            today = str(date.today())
            if status == "gone":
                entries[lid] = {"gone": True, "fetched_at": today, "source": source}
                stats["gone"] += 1
            elif status == "ok":
                entries[lid] = {"fields": fields, "fetched_at": today, "source": source}
                stats["ok"] += 1
    finally:
        browser.stop()
        save_entries(entries)
    return stats


# Per source, across drip_until calls: the spread scan calls it between
# slots (~18 times a day), and a fresh count each time would lift the
# DRIP_PER_DAY cap and forget a block. Kept on disk too (the status file's
# "state"): a daemon restart used to reset the day's count, drop the level
# to the start and forget a block pause — on 2026-10-18 Idealista blocked at
# 13:37 (pause 1 h), the 13:44 restart sent it straight back and 17 of 20
# pages failed at 13:49.
_DRIP_STATE: dict = {}


def _drip_status_path(source: str) -> Path:
    return BASE_DIR / f".drip_status_{source}.json"


def _load_drip_state(source: str) -> dict:
    """The drip state a previous process left in the status file."""
    st = {"day": date.today(), "done": 0, "blocked_until": 0.0, "level": DRIP_START_LEVEL,
          "changed": time.time(), "last_block": 0.0, "last": None}
    path = _drip_status_path(source)
    try:
        saved = json.loads(path.read_text())
        written = date.fromtimestamp(path.stat().st_mtime)
    except (OSError, ValueError):
        return st
    s = saved.get("state") or {}
    level = s.get("level", saved.get("level"))
    if isinstance(level, int) and 0 <= level < len(DRIP_LEVELS):
        st["level"] = level
    st["changed"] = float(s.get("changed") or st["changed"])
    st["last_block"] = float(s.get("last_block") or 0)
    st["last"] = saved.get("last")
    if s.get("blocked_until"):
        st["blocked_until"] = float(s["blocked_until"])
    elif saved.get("paused_until"):          # a status file from before "state"
        try:
            st["blocked_until"] = datetime.fromisoformat(saved["paused_until"]).timestamp()
        except ValueError:
            pass
    if (s.get("day") or written.isoformat()) == date.today().isoformat():
        st["done"] = int(s.get("done", saved.get("done_today")) or 0)
    return st


def drip_until(source: str, deadline: float, log=print, after_batch=None) -> None:
    """Drip listing pages for `source` until wall-clock `deadline`, then
    return (the caller's next slot is due). Sleeps like sleep_until in
    between, so a laptop that slept through the night catches up on wake.
    after_batch(): called after each session (photo reading)."""
    if source not in _DRIP_STATE:
        _DRIP_STATE[source] = _load_drip_state(source)
    st = _DRIP_STATE[source]
    queue: list = []
    built_at = 0.0
    next_batch = time.time() + random.uniform(30, 90)
    while deadline - time.time() > 0:
        now = datetime.now()
        if date.today() != st["day"]:
            st["day"], st["done"] = date.today(), 0
        eligible = (DRIP_HOURS[0] <= now.hour < DRIP_HOURS[1]
                    and st["done"] < DRIP_PER_DAY and time.time() >= st["blocked_until"])
        if eligible and time.time() >= next_batch:
            if time.time() - built_at > DRIP_QUEUE_TTL or not queue:
                queue, built_at = build_queue(source), time.time()
            size, gap_min = DRIP_LEVELS[st["level"]]
            take = queue[:min(size, DRIP_PER_DAY - st["done"])]
            queue = queue[len(take):]
            if take:
                try:
                    stats = asyncio.run(fetch_batch(source, take, log))
                except Exception as exc:
                    stats = {"ok": 0, "gone": 0, "error": len(take), "blocked": False}
                    log(f"[details] {source}: batch failed ({type(exc).__name__}: {exc})")
                st["done"] += len(take)
                _pace(source, st, stats, len(take), log)
                st["last"] = {"at": now.isoformat(timespec="seconds"), "pages": len(take),
                              **{k: stats[k] for k in ("ok", "gone", "error", "blocked")}}
                _write_drip_status(source, st, len(queue))
                if after_batch and not stats["blocked"]:
                    try:
                        after_batch()
                    except Exception as exc:
                        log(f"[details] after-batch step failed: {exc}")
            next_batch = time.time() + random.uniform(*DRIP_LEVELS[st["level"]][1]) * 60
        wait = 60.0
        if eligible:
            wait = min(wait, max(1.0, next_batch - time.time()))
        time.sleep(max(1.0, min(wait, deadline - time.time())))


def _pace(source: str, st: dict, stats: dict, n: int, log) -> None:
    """Move the drip's level after a session (see DRIP_LEVELS)."""
    now = time.time()
    # A whole session failing is a block whatever the page said: overnight
    # on 2026-10-18 Idealista failed 10/10 sessions for ~5 h and the drip
    # kept retrying every 5–20 min at its lowest level.
    if stats["blocked"] or (n >= 3 and stats["error"] >= n):
        stats["blocked"] = True
        pause = 6 * 3600 if now - st["last_block"] < 6 * 3600 else 3600
        st["level"], st["changed"], st["last_block"] = st["level"] // 2, now, now
        st["blocked_until"] = now + pause
        log(f"[details] {source}: blocked — pausing {pause // 3600} h, level {st['level']}")
        return
    if n and stats["error"] >= max(2, n / 3):
        st["level"], st["changed"] = max(0, st["level"] - 1), now
        log(f"[details] {source}: {stats['error']}/{n} failed — slowing to level {st['level']}")
    elif now - st["changed"] >= DRIP_STEP_UP_SEC and st["level"] < len(DRIP_LEVELS) - 1:
        st["level"], st["changed"] = st["level"] + 1, now
        log(f"[details] {source}: no trouble for {DRIP_STEP_UP_SEC // 3600} h — up to level {st['level']}")
    log(f"[details] {source}: {stats['ok']} ok, {stats['gone']} gone, {stats['error']} failed · "
        f"{st['done']} today · level {st['level']} ({DRIP_LEVELS[st['level']][0]} per session)")


def _write_drip_status(source: str, st: dict, queued: int) -> None:
    """For the dashboard (api.py _pipeline_status)."""
    size, gap = DRIP_LEVELS[st["level"]]
    per_hour = round(60 / (sum(gap) / 2 + size * sum(DRIP_PAGE_GAP_SEC) / 2 / 60) * size)
    try:
        _drip_status_path(source).write_text(json.dumps({
            "last": st["last"], "done_today": st["done"], "level": st["level"],
            "levels": len(DRIP_LEVELS), "per_session": size, "per_hour": per_hour,
            "paused_until": (datetime.fromtimestamp(st["blocked_until"]).isoformat(timespec="minutes")
                             if st["blocked_until"] > time.time() else None),
            "queued": queued,
            "state": {"day": st["day"].isoformat(), "done": st["done"], "level": st["level"],
                      "changed": st["changed"], "last_block": st["last_block"],
                      "blocked_until": st["blocked_until"]}}))
    except OSError:
        pass
