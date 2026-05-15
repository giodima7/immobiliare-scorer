#!/usr/bin/env python3
"""
email_digest_supabase.py
─────────────────────────
Sends a personalised daily digest to every user who has saved digest
filters in Supabase (digest_filters table). Reads listings from Supabase
directly — no local JSON snapshots needed — so the script runs on a
GitHub-hosted ubuntu-latest runner with zero non-stdlib dependencies.

For each user:
  1. Fetch saved filters from digest_filters
  2. Query listings WHERE is_stale=false AND listing_type=rental AND
     (first_seen_date = today OR price_changed_date = today)
     AND <user filter constraints>
  3. If matches >= min_new_listings: send HTML email
  4. Otherwise: skip (no email)

The old email_digest.py is kept untouched as a fallback for the
self-hosted-Mac workflow path.

Required env vars (set as GitHub Secrets):
  SUPABASE_URL          project URL
  SUPABASE_SERVICE_KEY  service-role key (read access to all tables)
  GMAIL_USER            sender Gmail address
  GMAIL_APP_PASSWORD    Gmail app password
  DASHBOARD_URL         public dashboard URL — used in CTA links

Optional env vars:
  DIGEST_DRY_RUN=1      build emails but don't actually send (debug)
"""
from __future__ import annotations

import datetime
import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Required env ─────────────────────────────────────────────────────────────
def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[digest] {name} not set — aborting", file=sys.stderr)
        sys.exit(1)
    return v


SUPABASE_URL  = _require("SUPABASE_URL").rstrip("/")
SUPABASE_KEY  = _require("SUPABASE_SERVICE_KEY")
GMAIL_USER    = _require("GMAIL_USER")
GMAIL_PASS    = _require("GMAIL_APP_PASSWORD")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://immobiliare-scorer.pages.dev").rstrip("/")
DRY_RUN       = os.environ.get("DIGEST_DRY_RUN") == "1"

TODAY = datetime.date.today().isoformat()

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Accept":        "application/json",
}


# ── Supabase REST helpers ────────────────────────────────────────────────────
def supabase_get(path: str) -> list:
    """GET /rest/v1/<path> — paginate transparently if results hit the 1000 cap."""
    rows: list = []
    offset = 0
    PAGE = 1000
    while True:
        sep = "&" if "?" in path else "?"
        url = f"{SUPABASE_URL}/rest/v1/{path}{sep}limit={PAGE}&offset={offset}"
        req = urllib.request.Request(url, headers={**HEADERS, "Prefer": "count=none"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                page = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"[digest] HTTP {e.code} at {url}: {e.read().decode()[:200]}", file=sys.stderr)
            return rows
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return rows


def fetch_all_digest_users() -> list[dict]:
    """All users who've saved filters AND haven't paused their alert."""
    return supabase_get("digest_filters?active=eq.true&select=*")


def fetch_new_listings_for_user(filters: dict) -> list[dict]:
    """
    Listings matching the user's filters that either appeared today for
    the first time OR had a price drop today. PostgREST's logical-OR
    syntax: ?or=(first_seen_date.eq.YYYY-MM-DD,price_changed_date.eq.YYYY-MM-DD)
    """
    params: list[tuple[str, str]] = [
        ("listing_type", "eq.rental"),
        ("is_stale",     "eq.false"),
        ("order",        "score_total.desc"),
        ("select",       "*"),
        ("or",           f"(first_seen_date.eq.{TODAY},price_changed_date.eq.{TODAY})"),
    ]

    if filters.get("max_rent"):
        params.append(("price", f'lte.{int(filters["max_rent"])}'))
    if filters.get("min_sqm"):
        params.append(("sqm", f'gte.{int(filters["min_sqm"])}'))
    if filters.get("min_rooms"):
        params.append(("rooms", f'gte.{filters["min_rooms"]}'))
    if filters.get("min_score"):
        params.append(("score_total", f'gte.{int(filters["min_score"])}'))
    if filters.get("min_floor") is not None:
        params.append(("floor_n", f'gte.{int(filters["min_floor"])}'))
    if filters.get("require_elevator"):
        params.append(("elevator", "eq.true"))
    if filters.get("max_metro_min"):
        params.append(("metro_walk_min", f'lte.{int(filters["max_metro_min"])}'))
    gems = filters.get("gems_filter") or "all"
    if gems == "hidden":
        params.append(("hidden_gem", "eq.true"))
    elif gems == "great_value":
        params.append(("good_value", "eq.true"))
    src = filters.get("source_filter") or "all"
    if src and src != "all":
        params.append(("source", f"eq.{src}"))
    if filters.get("fascia"):
        f_in = ",".join(filters["fascia"])
        params.append(("omi_fascia", f"in.({f_in})"))
    if filters.get("omi_zona"):
        z_in = ",".join(filters["omi_zona"])
        params.append(("omi_zona", f"in.({z_in})"))

    query = urllib.parse.urlencode(params, safe="(),.*")
    return supabase_get(f"listings?{query}")[:50]   # hard cap; build_email further trims to 20


# ── HTML email builders ──────────────────────────────────────────────────────
def _euro(n) -> str:
    try:
        return f"€{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def listing_card_html(l: dict) -> str:
    price      = l.get("price") or 0
    sqm        = l.get("sqm") or "?"
    rooms      = l.get("rooms")
    score      = l.get("score_total") or 0
    nbhd       = l.get("neighbourhood") or ""
    addr       = l.get("address") or ""
    url        = l.get("url") or "#"
    photo      = l.get("thumbnail") or ""
    metro_min  = l.get("metro_walk_min")
    metro_name = l.get("metro_nearest_name") or ""
    metro_line = l.get("metro_nearest_line") or ""
    floor_lbl  = l.get("floor_label") or ""
    delta      = l.get("comps_delta_pct")
    is_gem     = bool(l.get("hidden_gem"))
    is_good    = bool(l.get("good_value"))
    prev_price = l.get("previous_price")
    is_new     = l.get("first_seen_date") == TODAY
    is_drop    = l.get("price_changed_date") == TODAY and prev_price

    # Score colour matches the card-ring colour palette in dashboard CSS.
    score_color = "#2A7A5A" if score >= 80 else "#E8922A" if score >= 60 else "#E05C4B"

    # Hidden Gem / Great Value badge
    if is_gem:
        badge = ('<span style="background:#E6F4ED;color:#2A7A5A;padding:3px 10px;'
                 'border-radius:20px;font-size:11px;font-weight:700">✦ Hidden Gem</span>')
    elif is_good:
        badge = ('<span style="background:#FFF3E0;color:#B85C00;padding:3px 10px;'
                 'border-radius:20px;font-size:11px;font-weight:700">💰 Great Value</span>')
    else:
        badge = ""

    # New / price-drop tag
    if is_drop and prev_price:
        try:
            drop_pct = round((prev_price - price) / prev_price * 100)
        except ZeroDivisionError:
            drop_pct = 0
        status_tag = (f'<span style="background:#FDEEEC;color:#C0392B;padding:3px 10px;'
                      f'border-radius:20px;font-size:11px;font-weight:700">'
                      f'↓ Price dropped {drop_pct}% (was {_euro(prev_price)})</span>')
    elif is_new:
        status_tag = ('<span style="background:#EBF4FF;color:#1A5FA8;padding:3px 10px;'
                      'border-radius:20px;font-size:11px;font-weight:700">New today</span>')
    else:
        status_tag = ""

    # Comps delta
    delta_html = ""
    if delta is not None:
        delta_color = "#2A7A5A" if delta < 0 else "#E05C4B"
        delta_html = (f'<span style="background:#F7F5F2;padding:4px 10px;border-radius:20px;'
                      f'font-size:12px;color:{delta_color};font-weight:600">'
                      f'{delta:+.1f}% vs comps</span>')

    metro_str = ""
    if metro_min is not None and metro_name:
        line = f" {metro_line}" if metro_line else ""
        metro_str = (f'<span style="background:#F7F5F2;padding:4px 10px;border-radius:20px;'
                     f'font-size:12px;color:#6B6560;margin-right:6px">'
                     f'🚇 {metro_min} min · {metro_name}{line}</span>')
    floor_str = (f'<span style="background:#F7F5F2;padding:4px 10px;border-radius:20px;'
                 f'font-size:12px;color:#6B6560;margin-right:6px">🏢 {floor_lbl}</span>'
                 if floor_lbl else "")

    photo_html = (
        f'<img src="{photo}" width="100%" height="180" '
        f'style="object-fit:cover;display:block;border-radius:10px 10px 0 0" alt="">'
        if photo else
        '<div style="height:120px;background:#F7F5F2;border-radius:10px 10px 0 0;'
        'display:flex;align-items:center;justify-content:center;color:#C5BFB9;font-size:32px">🏠</div>'
    )

    rooms_str = f"{rooms} loc." if rooms else ""
    sub_specs = " · ".join(p for p in (f"{sqm}m²", rooms_str) if p)

    return f'''
    <table width="100%" cellpadding="0" cellspacing="0" style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);margin-bottom:16px">
      <tr><td>{photo_html}</td></tr>
      <tr><td style="padding:16px">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>{badge}{"&nbsp;" if badge and status_tag else ""}{status_tag}</td>
            <td align="right">
              <span style="background:{score_color};color:white;width:36px;height:36px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-weight:800;font-size:14px">{score}</span>
            </td>
          </tr>
          <tr><td colspan="2" style="padding-top:10px">
            <div style="font-weight:700;font-size:16px;color:#1A1A1A">{nbhd}</div>
            <div style="color:#6B6560;font-size:12px;margin-top:2px">{addr}</div>
          </td></tr>
          <tr><td colspan="2" style="padding-top:10px">
            <span style="font-size:24px;font-weight:800;color:#1A1A1A">{_euro(price)}/mo</span>
            <span style="color:#6B6560;font-size:13px;margin-left:8px">{sub_specs}</span>
          </td></tr>
          <tr><td colspan="2" style="padding-top:6px">{metro_str}{floor_str}{delta_html}</td></tr>
          <tr><td colspan="2" style="padding-top:14px">
            <a href="{url}" style="display:block;background:#2A7A5A;color:white;text-align:center;padding:12px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">View listing →</a>
          </td></tr>
        </table>
      </td></tr>
    </table>'''


def build_email_html(user_filters: dict, listings: list[dict]) -> str:
    name = (user_filters.get("display_name") or "").strip()
    first_name = name.split()[0] if name else "there"

    # Dedup (a listing showing up in both the new + price-drop arms of the
    # OR query would otherwise render twice).
    seen: set[str] = set()
    unique: list[dict] = []
    for l in listings:
        lid = l.get("id")
        if lid and lid not in seen:
            seen.add(lid)
            unique.append(l)

    new_count  = sum(1 for l in unique if l.get("first_seen_date") == TODAY)
    drop_count = sum(1 for l in unique if l.get("price_changed_date") == TODAY
                     and l.get("previous_price"))

    summary_parts = []
    if new_count:
        summary_parts.append(f'<strong>{new_count} new listing{"s" if new_count != 1 else ""}</strong>')
    if drop_count:
        summary_parts.append(f'<strong>{drop_count} price drop{"s" if drop_count != 1 else ""}</strong>')
    summary = " and ".join(summary_parts) + " matching your filters"

    cards_html = "\n".join(listing_card_html(l) for l in unique[:20])
    today_str  = datetime.date.today().strftime("%-d %b %Y")

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F7F5F2;font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:24px 16px">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">

        <!-- Header -->
        <tr><td style="padding-bottom:24px">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td><span style="font-size:22px;font-weight:800;color:#1A1A1A">🟢 lume</span></td>
              <td align="right"><span style="color:#6B6560;font-size:13px">{today_str}</span></td>
            </tr>
          </table>
        </td></tr>

        <!-- Intro card -->
        <tr><td style="background:white;border-radius:12px;padding:20px 24px">
          <div style="font-size:20px;font-weight:700;color:#1A1A1A;margin-bottom:8px">
            Good morning, {first_name} 👋
          </div>
          <div style="color:#6B6560;font-size:14px;line-height:1.6">
            Today Lume found {summary}.
          </div>
          <div style="margin-top:14px">
            <a href="{DASHBOARD_URL}" style="display:inline-block;background:#2A7A5A;color:white;padding:10px 20px;border-radius:20px;text-decoration:none;font-weight:600;font-size:13px">
              Open dashboard →
            </a>
            <a href="{DASHBOARD_URL}/#settings" style="display:inline-block;margin-left:10px;color:#6B6560;font-size:12px;text-decoration:underline">
              Edit alert filters
            </a>
          </div>
        </td></tr>

        <tr><td style="height:16px"></td></tr>

        <!-- Listing cards -->
        <tr><td>{cards_html}</td></tr>

        <!-- Footer -->
        <tr><td style="padding-top:24px;text-align:center">
          <p style="color:#9E9791;font-size:12px;line-height:1.6">
            You're receiving this because you saved alert filters on Lume.<br>
            <a href="{DASHBOARD_URL}/#settings" style="color:#9E9791">Manage alerts</a> ·
            <a href="{DASHBOARD_URL}" style="color:#9E9791">Open dashboard</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>'''


# ── Email send ───────────────────────────────────────────────────────────────
def send_email(to_email: str, subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Lume <{GMAIL_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))

    if DRY_RUN:
        print(f"  [DRY RUN] would send to {to_email!r}: {subject!r}")
        return

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, [to_email], msg.as_string())


# ── Orchestration ────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[digest] Running for {TODAY}{' (DRY RUN)' if DRY_RUN else ''}")
    users = fetch_all_digest_users()
    print(f"[digest] {len(users)} active user(s) with saved filters")

    sent = 0
    skipped_below_threshold = 0
    skipped_no_email = 0
    failed = 0

    for user in users:
        email = (user.get("email") or "").strip()
        min_n = int(user.get("min_new_listings") or 1)
        if not email:
            print(f"  [skip] user {user.get('clerk_user_id')} has no email")
            skipped_no_email += 1
            continue

        listings = fetch_new_listings_for_user(user)
        # Dedup happens inside build_email_html as well, but count from the
        # raw set so the threshold check matches what build_email shows.
        unique = {l.get("id"): l for l in listings if l.get("id")}
        match_count = len(unique)
        print(f"  [user] {email} — {match_count} match(es)")

        if match_count < min_n:
            skipped_below_threshold += 1
            continue

        new_count  = sum(1 for l in unique.values() if l.get("first_seen_date") == TODAY)
        drop_count = sum(1 for l in unique.values()
                         if l.get("price_changed_date") == TODAY and l.get("previous_price"))

        subj_parts = []
        if new_count:  subj_parts.append(f"{new_count} new")
        if drop_count: subj_parts.append(f"{drop_count} price drop{'s' if drop_count != 1 else ''}")
        subject = "🏠 Lume — " + " · ".join(subj_parts) + " matching your filters"

        try:
            html = build_email_html(user, list(unique.values()))
            send_email(email, subject, html)
            print(f"  [sent] → {email}")
            sent += 1
        except Exception as exc:
            print(f"  [fail] {email}: {exc}")
            failed += 1

    print(f"[digest] Done — sent {sent}, "
          f"skipped {skipped_below_threshold} below threshold, "
          f"{skipped_no_email} missing email, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
