#!/usr/bin/env python3
from __future__ import annotations  # X | Y syntax works on Python 3.9+
"""
email_digest.py
───────────────
Daily digest of new Milano rental listings.
Configuration is stored in email_config.json at project root.

Usage (standalone test):
    python3 email_digest.py
"""

import json
import re as _re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "email_config.json"

# Public dashboard URL (Cloudflare Pages). Override via the LUME_DASHBOARD_URL
# env var when running against a custom domain or the Pages preview branch.
import os as _os
DASHBOARD_URL = _os.environ.get("LUME_DASHBOARD_URL", "https://lume.pages.dev")

DEFAULT_CONFIG: dict = {
    "enabled":   False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_pass": "",
    "to_addrs":  [],
    "digest_hour": 8,
    "filters": {
        "min_score": 0,
        "max_rent":  0,
        "min_rooms": 0,
        "areas":     [],
    },
}


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved  = json.loads(CONFIG_PATH.read_text())
            result = {**DEFAULT_CONFIG, **saved}
            result["filters"] = {**DEFAULT_CONFIG["filters"],
                                  **saved.get("filters", {})}
            return result
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ── Filter helpers ─────────────────────────────────────────────────────────────

def apply_digest_filters(listings: list, filters: dict) -> list:
    """Apply email-specific filters (separate from scanner filters)."""
    result     = listings
    min_score  = int(filters.get("min_score") or 0)
    max_rent   = int(filters.get("max_rent")  or 0)
    min_rooms  = int(filters.get("min_rooms") or 0)
    areas      = [a.lower().strip() for a in (filters.get("areas") or []) if a]

    if min_score:
        result = [l for l in result if (l.get("score_total") or 0) >= min_score]
    if max_rent:
        result = [l for l in result if (l.get("rent_mo") or 0) <= max_rent]
    if min_rooms:
        result = [l for l in result if (l.get("rooms") or 0) >= min_rooms]
    if areas:
        result = [l for l in result
                  if any(a in (l.get("neighbourhood") or "").lower() for a in areas)]
    return result


# ── Design helpers (matches dashboard CSS tokens) ─────────────────────────────

def _score_style(score) -> tuple[str, str, str]:
    """(bg, text_color, border_color) for the score circle badge."""
    try:
        s = int(score)
        if s >= 70: return "rgba(22,163,74,0.12)", "#15803D", "#16A34A"
        if s >= 50: return "rgba(217,119,6,0.12)",  "#B45309", "#D97706"
        return       "rgba(220,38,38,0.12)",  "#B91C1C", "#DC2626"
    except (TypeError, ValueError):
        return "rgba(107,114,128,0.12)", "#6B7280", "#9CA3AF"


def _fascia_style(fascia: str) -> tuple[str, str]:
    """(bg, color) for fascia badge — mirrors .fascia-B/C/D/E/R in CSS."""
    m = {
        "A": ("rgba(45,106,79,0.12)",  "#1E5038"),
        "B": ("rgba(45,106,79,0.12)",  "#1E5038"),
        "C": ("rgba(22,163,74,0.12)",  "#15803D"),
        "D": ("rgba(217,119,6,0.12)",  "#92400E"),
        "E": ("rgba(220,38,38,0.12)",  "#991B1B"),
        "R": ("rgba(107,114,128,0.12)","#6B7280"),
    }
    return m.get(str(fascia or "").upper(), ("rgba(107,114,128,0.12)", "#6B7280"))


def _vs_omi_style(pct) -> tuple[str, str]:
    """(bg, color) for vs-OMI pill — mirrors .vs-below/.vs-at/.vs-above."""
    try:
        v = float(pct)
        if v <= 0:  return "rgba(22,163,74,0.12)",  "#15803D"
        if v < 10:  return "rgba(217,119,6,0.12)",  "#92400E"
        return             "rgba(220,38,38,0.12)",  "#991B1B"
    except (TypeError, ValueError):
        return "rgba(217,119,6,0.12)", "#92400E"


def _fmt(n) -> str:
    """Thousands-separated integer string."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "–"


def _floor_display(l: dict) -> str | None:
    """
    Return a short floor label for the facts strip cell.
    Full strings like "6° piano con ascensore" are abbreviated to "Floor 6"
    so they don't overflow the narrow fact cell.
    """
    fn    = l.get("floor_n")
    floor = (l.get("floor") or "").strip()
    if fn is not None:
        if fn == 0:  return "Ground"
        if fn  < 0:  return "Basement"
        return f"Floor {fn}"
    if floor:
        m = _re.search(r'(\d+)', floor)
        if m:   return f"Floor {m.group(1)}"
        if _re.search(r'terra|ground', floor, _re.I):
            return "Ground"
        return floor[:14]  # last-resort truncation
    return None


def _why_sentence(l: dict, omi_mid) -> tuple[str, bool] | tuple[None, None]:
    """
    Returns (html_text, is_positive) for the insight sentence.
    is_positive=True  → green .why-sentence background
    is_positive=False → neutral grey background
    """
    dp   = l.get("comps_delta_pct")
    n    = l.get("comps_n") or 0
    med  = l.get("comps_median")
    conf = (l.get("comps_conf_label") or "").lower()

    if dp is not None and med is not None:
        abs_dp = abs(dp)
        conf_str = (
            "Lume is confident"  if conf == "high" else
            "Lume is fairly sure" if conf == "medium" else
            "Lume isn't sure — thin data" if conf == "low" else
            ""
        )
        cpart  = f"€{med}/m²/mo · {conf_str}" if conf_str else f"€{med}/m²/mo"
        if l.get("hidden_gem"):
            return (f"<strong>Hidden Gem</strong> — {abs_dp:.1f}% below similar flats nearby ({cpart}).", True)
        if l.get("good_value"):
            return (f"<strong>Great Value</strong> — {abs_dp:.1f}% below similar flats nearby ({cpart}).", True)
        if dp <= -3:
            return (f"{abs_dp:.1f}% below similar flats nearby ({cpart}).", True)
        if dp >= 5:
            return (f"{dp:.1f}% above similar flats nearby ({cpart}) — above market rate.", False)
        return (f"In line with similar flats nearby, {n} similar flats nearby ({cpart}).", False)

    if omi_mid:
        zona = l.get("omi_zona") or "zone"
        return (f"No local flats to compare — Lume benchmarked against OMI {zona} (€{omi_mid}/m²/mo).", False)

    return None, None


def _lume_verdict(l: dict) -> str:
    """One-line Lume verdict displayed under each card in the email."""
    score = l.get("score_total") or 0
    if l.get("hidden_gem"):  return "Lume says: hidden gem — go look."
    if l.get("good_value"):  return "Lume says: reasonable for the area."
    if score >= 70:          return "Lume says: solid choice."
    return "Lume says: take a look but check the details."


# ── Card tile builder ──────────────────────────────────────────────────────────

def _listing_tile(l: dict) -> str:
    """
    Render one card as a full-width table block.
    Designed to match the dashboard 'Variant A' card layout.
    """
    score     = l.get("score_total", "–")
    neigh     = l.get("neighbourhood") or "?"
    address   = (l.get("address") or "").strip()
    fascia    = l.get("omi_fascia") or "?"
    rent      = l.get("rent_mo")
    spese     = l.get("spese_condominiali")
    sqm       = l.get("sqm")
    ask_psqm  = l.get("ask_psqm")
    rooms     = l.get("rooms")
    vs_pct    = l.get("vs_omi_rent_pct")
    vs_lbl    = (l.get("vs_omi_label") or "").strip()
    url       = l.get("url", "#")
    thumbnail = l.get("thumbnail") or ""
    source    = l.get("source", "immobiliare")
    omi_mid   = l.get("omi_rent_mid")
    omi_rmin  = l.get("omi_rmin")
    omi_rmax  = l.get("omi_rmax")
    omi_zona  = l.get("omi_zona") or ""
    hidden_gem = bool(l.get("hidden_gem"))
    good_value = bool(l.get("good_value"))
    metro_name = l.get("metro_nearest_name") or ""
    metro_min  = l.get("metro_walk_min")
    metro_dist = l.get("metro_nearest_dist_m")
    comps_med  = l.get("comps_median")
    comps_n    = l.get("comps_n") or 0

    # Style tokens
    sc_bg, sc_col, sc_border = _score_style(score)
    fb_bg, fb_col            = _fascia_style(fascia)
    source_lbl = "Idealista" if source == "idealista" else "Immobiliare.it"

    # ── Photo section ─────────────────────────────────────────────────────────
    gem_chip = ""
    if hidden_gem:
        gem_chip = (
            '<div style="position:absolute;top:12px;left:12px;display:inline-block;'
            'background:rgba(255,255,255,0.96);border-radius:999px;'
            'padding:5px 11px 5px 8px;font-size:11px;font-weight:700;color:#2A7A5A;'
            'box-shadow:0 2px 8px rgba(0,0,0,0.18);white-space:nowrap">'
            '✦ Hidden Gem</div>'
        )
    elif good_value:
        gem_chip = (
            '<div style="position:absolute;top:12px;left:12px;display:inline-block;'
            'background:rgba(255,255,255,0.96);border-radius:999px;'
            'padding:5px 11px 5px 8px;font-size:11px;font-weight:700;color:#B85C00;'
            'box-shadow:0 2px 8px rgba(0,0,0,0.18);white-space:nowrap">'
            'Great Value</div>'
        )

    if thumbnail:
        photo_section = (
            f'<div style="position:relative;overflow:hidden">'
            f'<img src="{thumbnail}" alt="" width="100%"'
            f' style="display:block;width:100%;height:190px;object-fit:cover">'
            f'{gem_chip}'
            f'</div>'
        )
    else:
        # No photo: show gem chip as a pill in the header area instead
        photo_section = '<div style="height:4px;background:#F5F5F3"></div>'

    # ── Score circle  ─────────────────────────────────────────────────────────
    # box-sizing:border-box → content height = 48 - 2*3(border) = 42px.
    # line-height:42px centers the single line of text in that 42px content area.
    score_circle = (
        f'<div style="width:48px;height:48px;border-radius:50%;'
        f'border:3px solid {sc_border};background:{sc_bg};'
        f'text-align:center;line-height:42px;box-sizing:border-box;'
        f'font-weight:800;font-size:15px;color:{sc_col};letter-spacing:-0.3px">'
        f'{score}</div>'
    )

    # ── Card header: score circle + neighbourhood title ────────────────────────
    # Layout uses an HTML <table> inside a padded <div> so width:100% is
    # scoped WITHIN the padding (no box-sizing juggling needed).
    # Line 1: neighbourhood  [Fascia X]  [Milano · Affitto]
    # Line 2: address · source
    subline_text = (address if address and address.lower() != neigh.lower() else "") or ""
    if subline_text:
        parts = [p.strip() for p in subline_text.split(",")]
        while parts and parts[-1].lower() in {"milano", "mi", "milan", "italy", "italia"}:
            parts.pop()
        subline_text = ", ".join(parts[:2])   # max 2 segments (street + neighbourhood)

    subline_html = (
        f'<div style="font-size:11px;color:#6B7280;margin-top:4px">'
        f'{subline_text + " · " if subline_text else ""}{source_lbl}</div>'
    ) if (subline_text or source_lbl) else ""

    city_tag = (
        '<span style="display:inline-block;background:rgba(109,40,217,0.12);color:#6D28D9;'
        'border-radius:10px;padding:1px 6px;font-size:10px;font-weight:600;'
        'margin-left:6px;white-space:nowrap;vertical-align:middle">'
        'Milano · Affitto</span>'
    )
    fascia_tag = (
        f'<span style="display:inline-block;font-size:10px;font-weight:700;'
        f'background:{fb_bg};color:{fb_col};border-radius:5px;padding:2px 7px;'
        f'margin-left:6px;white-space:nowrap;vertical-align:middle">'
        f'Fascia {fascia}</span>'
    )

    header = (
        f'<div style="padding:14px 16px 8px">'
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">'
        f'<tr>'
        f'<td style="width:52px;padding-right:12px;vertical-align:middle">{score_circle}</td>'
        f'<td style="vertical-align:middle">'
        f'<div style="font-size:16px;font-weight:700;color:#111827;line-height:1.4">'
        f'{neigh}{fascia_tag}{city_tag}</div>'
        f'{subline_html}'
        f'</td>'
        f'</tr>'
        f'</table>'
        f'</div>'
    )

    # ── Price row ─────────────────────────────────────────────────────────────
    # Line 1: big price + vs-OMI badge (right-aligned) — same <table> trick
    # Line 2: size + €/m²/mo  (grey, smaller)
    rent_str  = f"€{_fmt(rent)}" if rent else "–"
    rent_unit = "/mo" if rent else ""

    vs_html = ""
    if vs_lbl:
        v_bg, v_col = _vs_omi_style(vs_pct)
        vs_html = (
            f'<span style="display:inline-block;font-size:12px;font-weight:600;'
            f'background:{v_bg};color:{v_col};border-radius:6px;'
            f'padding:3px 9px;white-space:nowrap">{vs_lbl}</span>'
        )

    sub_parts = []
    if sqm:       sub_parts.append(f"{_fmt(sqm)} m²")
    if ask_psqm:  sub_parts.append(f"€{ask_psqm:.1f}/m²/mo")
    if spese:
        total = (rent or 0) + spese
        sub_parts.append(f"+ €{_fmt(spese)} fees → €{_fmt(total)}/mo total")
    sub_line = " · ".join(sub_parts)

    price_row = (
        f'<div style="padding:0 16px 4px">'
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">'
        f'<tr>'
        f'<td style="vertical-align:baseline">'
        f'<span style="font-size:24px;font-weight:800;color:#111827;letter-spacing:-0.5px">{rent_str}</span>'
        f'<span style="font-size:13px;color:#6B7280;font-weight:500;margin-left:3px">{rent_unit}</span>'
        f'</td>'
        f'<td style="vertical-align:middle;text-align:right">{vs_html}</td>'
        f'</tr>'
        f'</table>'
        f'</div>'
        + (f'<div style="padding:2px 16px 8px;font-size:12px;color:#6B7280">{sub_line}</div>'
           if sub_line else '')
    )

    # ── Facts strip (4 cells matching dashboard) ───────────────────────────────
    floor_disp = _floor_display(l)
    elev = l.get("elevator")
    floor_val  = (("🛗 " if elev is True else "") + floor_disp) if floor_disp else "–"
    floor_sub  = ("with lift" if elev is True else "no lift" if elev is False else "lift unknown") if floor_disp else "–"

    metro_val  = f"🚇 {metro_name}" if metro_name else "–"
    metro_sub  = (f"{metro_min} min walk" if metro_min is not None
                  else f"{int(metro_dist)}m away" if metro_dist is not None
                  else "not enriched")

    comps_val  = f"€{comps_med}/m²" if comps_med else (f"€{omi_mid}/m²" if omi_mid else "–")
    comps_sub  = (f"{comps_n} comps median" if comps_med else "OMI median" if omi_mid else "no data")

    def _fact_cell(val, sub, right_border=True):
        border = f"border-right:1px solid #E8E8E3;" if right_border else ""
        return (
            f'<td style="padding:10px 8px;text-align:center;{border}width:25%">'
            f'<div style="font-size:12px;font-weight:700;color:#111827;line-height:1.2">{val}</div>'
            f'<div style="font-size:10px;color:#6B7280;margin-top:2px">{sub}</div>'
            f'</td>'
        )

    rooms_val = f"{rooms} rooms" if rooms else (f"{_fmt(sqm)} m²" if sqm else "–")
    cond = (l.get("condition") or "").split(" / ")[0]
    rooms_sub = cond or (f"{_fmt(sqm)} m²" if sqm and rooms else "–")

    facts_strip = (
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;'
        f'background:#F5F5F3;border-top:1px solid #E8E8E3;border-bottom:1px solid #E8E8E3;margin-top:10px">'
        f'<tr>'
        f'{_fact_cell(rooms_val, rooms_sub)}'
        f'{_fact_cell(floor_val, floor_sub)}'
        f'{_fact_cell(metro_val, metro_sub)}'
        f'{_fact_cell(comps_val, comps_sub, right_border=False)}'
        f'</tr></table>'
    )

    # ── Why sentence ──────────────────────────────────────────────────────────
    why_text, why_pos = _why_sentence(l, omi_mid)
    if why_text:
        why_bg  = "#F0FDF4" if why_pos else "#F5F5F3"
        why_col = "#15803D" if why_pos else "#6B7280"
        why_block = (
            f'<div style="padding:0 16px 4px">'
            f'<div style="font-size:11px;color:{why_col};background:{why_bg};'
            f'padding:8px 10px;border-radius:8px;line-height:1.5">{why_text}</div>'
            f'</div>'
        )
    else:
        why_block = ""

    # ── Footer ────────────────────────────────────────────────────────────────
    omi_note = ""
    if omi_zona:
        omi_range = f" · €{omi_rmin}–{omi_rmax}/m²/mo" if omi_rmin and omi_rmax else ""
        omi_note  = f"OMI {omi_zona}{omi_range}"
    footer = (
        f'<div style="padding:8px 16px 16px">'
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">'
        f'<tr>'
        f'<td style="vertical-align:middle">'
        f'<span style="font-size:11px;color:#6B7280">{omi_note}</span>'
        f'</td>'
        f'<td style="vertical-align:middle;text-align:right">'
        f'<a href="{url}"'
        f' style="display:inline-block;background:#2D6A4F;color:#fff;text-decoration:none;'
        f'border-radius:6px;padding:7px 18px;font-size:12px;font-weight:700">↗ View listing</a>'
        f'</td>'
        f'</tr>'
        f'</table>'
        f'</div>'
    )

    return (
        f'<div style="background:#FFFFFF;border:1px solid #E8E8E3;border-radius:12px;'
        f'overflow:hidden;height:100%">'
        f'{photo_section}'
        f'{header}'
        f'{price_row}'
        f'{facts_strip}'
        f'{why_block}'
        f'{footer}'
        f'</div>'
    )


def _build_html(listings: list) -> str:
    date_str      = datetime.now().strftime("%d %b %Y %H:%M")
    n             = len(listings)
    plural        = "s" if n != 1 else ""
    dashboard_url = DASHBOARD_URL

    # Build 2-column table rows
    rows = ""
    for i in range(0, n, 2):
        left  = _listing_tile(listings[i])
        right = _listing_tile(listings[i + 1]) if i + 1 < n else ""
        right_cell = (
            f'<td style="width:50%;padding:0 0 16px 8px;vertical-align:top">{right}</td>'
            if right else
            '<td style="width:50%;padding:0 0 16px 8px;vertical-align:top"></td>'
        )
        rows += (
            f'<tr>'
            f'<td style="width:50%;padding:0 8px 16px 0;vertical-align:top">{left}</td>'
            f'{right_cell}'
            f'</tr>\n'
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="background:#F5F5F3;color:#111827;margin:0;padding:0">
<div style="max-width:700px;margin:0 auto;padding:24px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">

  <!-- Lume header — glowing white dot on green panel -->
  <div style="background:#2A7A5A;border-radius:16px;padding:20px 24px;margin-bottom:20px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      <svg width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg" style="display:block" aria-hidden="true">
        <defs>
          <radialGradient id="lume-glow-email" cx="50%" cy="50%" r="50%">
            <stop offset="40%" stop-color="white"/>
            <stop offset="100%" stop-color="white" stop-opacity="0.15"/>
          </radialGradient>
        </defs>
        <circle cx="14" cy="14" r="12" fill="url(#lume-glow-email)"/>
        <circle cx="14" cy="14" r="6"  fill="white"/>
      </svg>
      <div style="font-size:24px;font-weight:700;color:white;letter-spacing:-0.5px">lume</div>
    </div>
    <div style="font-size:14px;color:rgba(255,255,255,0.88);line-height:1.5">
      Lume scanned {n} listing{plural} overnight. Here's what's worth your time today.
    </div>
    <div style="margin-top:10px;font-size:12px">
      <a href="{dashboard_url}/#rentals"
         style="color:white;text-decoration:underline;opacity:0.85">Open Lume →</a>
    </div>
  </div>

  <!-- 2-column grid -->
  <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">
    {rows}
  </table>

  <p style="color:#A09990;font-size:12px;margin:24px 0 0;text-align:center;line-height:1.7">
    <a href="{dashboard_url}/#rentals" style="color:#2A7A5A;text-decoration:none;font-weight:600">Open Lume →</a>
    <br>Lume · made for honest renters
  </p>
</div>
</body>
</html>"""


def _build_text(listings: list) -> str:
    date_str = datetime.now().strftime("%d %b %Y %H:%M")
    n        = len(listings)
    lines    = [
        f"Lume — what's worth a look today ({date_str})",
        f"Lume scanned {n} listing{'s' if n != 1 else ''} overnight.",
        f"Open Lume: {DASHBOARD_URL}/#rentals",
        "",
    ]
    for l in listings:
        rent   = l.get("rent_mo")
        spese  = l.get("spese_condominiali")
        neigh  = l.get("neighbourhood") or "?"
        score  = l.get("score_total", "–")
        vs     = l.get("vs_omi_label", "")
        url    = l.get("url", "")
        sqm    = l.get("sqm")
        rent_s = f"€{rent:,}/mo" if rent else "–"
        fee_s  = f" + €{spese:,} fees" if spese else ""
        lines += [
            f"  [{score}/100]  {neigh}  —  {rent_s}{fee_s}  ({sqm} m²)",
            f"           {vs}",
            f"           {url}",
            "",
        ]
    return "\n".join(lines)


# ── Send ───────────────────────────────────────────────────────────────────────

def send_digest(listings: list, config: dict | None = None):
    """
    Send the daily digest for `listings`.
    - Loads config from email_config.json if not supplied.
    - Applies digest-specific filters (may differ from scanner filters).
    - If SMTP is not configured, prints to stdout.
    """
    if not listings:
        return

    if config is None:
        config = load_config()

    # Apply digest-specific filters
    filtered = apply_digest_filters(listings, config.get("filters", {}))
    if not filtered:
        print("  (digest: 0 listings pass digest filters — nothing sent)")
        return

    n        = len(filtered)
    date_str = datetime.now().strftime("%d %b %Y")
    subject  = f"Lume — what's worth a look today ({datetime.now().strftime('%d %b')})"

    smtp_user = (config.get("smtp_user") or "").strip()
    to_addrs  = [a for a in (config.get("to_addrs") or []) if a]
    enabled   = config.get("enabled", False)

    if not enabled or not smtp_user or not to_addrs:
        # Not configured → stdout fallback
        print("\n" + "═" * 62)
        print(f"  DIGEST  {subject}")
        print("═" * 62)
        print(_build_text(filtered))
        print("═" * 62 + "\n")
        return

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(_build_text(filtered), "plain",  "utf-8"))
    msg.attach(MIMEText(_build_html(filtered), "html",   "utf-8"))

    try:
        with smtplib.SMTP(
            config.get("smtp_host", "smtp.gmail.com"),
            int(config.get("smtp_port", 587)),
        ) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(smtp_user, config.get("smtp_pass", ""))
            smtp.sendmail(smtp_user, to_addrs, msg.as_string())
        print(f"  ✓ Digest sent → {', '.join(to_addrs)}  ({n} listings)")
    except Exception as exc:
        print(f"  ✗ Email failed: {exc}", file=sys.stderr)


# ── CLI self-test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_config()
    print(f"Email config: enabled={cfg['enabled']}  user={cfg['smtp_user'] or '(not set)'}")
    print(f"To: {cfg['to_addrs'] or '(not set)'}")
    print(f"Filters: {cfg['filters']}")
    print("Pass a rentals list to send_digest() to test sending.")
