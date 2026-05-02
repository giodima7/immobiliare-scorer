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
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "email_config.json"

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


# ── Email builders ─────────────────────────────────────────────────────────────

def _score_colour(score) -> str:
    try:
        s = int(score)
        return "#22c55e" if s >= 70 else "#f59e0b" if s >= 50 else "#ef4444"
    except (TypeError, ValueError):
        return "#8892aa"

def _vs_colour(pct) -> str:
    try:
        return "#22c55e" if float(pct) <= 0 else "#ef4444"
    except (TypeError, ValueError):
        return "#8892aa"

def _stat_row(label: str, value: str, value_colour: str = "#e2e8f0") -> str:
    return (
        f'<tr>'
        f'<td style="color:#8892aa;font-size:11px;padding:3px 8px 3px 0;'
        f'white-space:nowrap;vertical-align:top">{label}</td>'
        f'<td style="color:{value_colour};font-size:11px;font-weight:600;'
        f'padding:3px 0;vertical-align:top">{value}</td>'
        f'</tr>'
    )

def _listing_tile(l: dict) -> str:
    """Render one card tile for use inside a 2-column table cell."""
    rent      = l.get("rent_mo")
    sqm       = l.get("sqm")
    spese     = l.get("spese_condominiali")
    neigh     = l.get("neighbourhood") or "?"
    score     = l.get("score_total", "–")
    vs_pct    = l.get("vs_omi_rent_pct")
    vs_lbl    = l.get("vs_omi_label", "")
    url       = l.get("url", "#")
    fascia    = l.get("omi_fascia", "?")
    rooms     = l.get("rooms")
    floor_v   = l.get("floor")
    thumbnail = l.get("thumbnail") or ""
    condition = l.get("condition") or ""
    omi_mid   = l.get("omi_rent_mid")
    omi_rmin  = l.get("omi_rmin")
    omi_rmax  = l.get("omi_rmax")
    ask_psqm  = l.get("ask_psqm")
    address   = l.get("address") or ""

    rent_str  = f"€{rent:,}/mo" if rent else "–"
    spese_str = f"+ €{spese:,}/mo fees" if spese else ""
    total     = (rent + spese) if rent and spese else None
    total_str = f"→ Total €{total:,}/mo" if total else ""
    psqm_str  = f"€{ask_psqm:.1f}/m²/mo" if ask_psqm else ""
    omi_str   = f"€{omi_rmin}–{omi_rmax}/m²/mo" if omi_rmin and omi_rmax else ""
    omi_mid_s = f"€{omi_mid}/m²/mo" if omi_mid else ""

    sc  = _score_colour(score)
    vc  = _vs_colour(vs_pct)

    img_block = (
        f'<img src="{thumbnail}" alt="" width="100%"'
        f' style="display:block;width:100%;height:180px;object-fit:cover">'
    ) if thumbnail else (
        '<div style="height:6px;background:#2e3250"></div>'
    )

    # Stats table rows
    stats = ""
    if sqm:
        stats += _stat_row("Size", f"{sqm} m²" + (f" · {psqm_str}" if psqm_str else ""))
    if rooms:
        stats += _stat_row("Rooms", f"{rooms} room{'s' if rooms != 1 else ''}" +
                           (f" · Floor {floor_v}" if floor_v else ""))
    if vs_lbl:
        stats += _stat_row("vs OMI", vs_lbl, vc)
    if omi_mid_s:
        stats += _stat_row("OMI mid", omi_mid_s +
                           (f" (range {omi_str})" if omi_str else ""), "#4f8ef7")
    if condition:
        stats += _stat_row("Condition", condition)
    if address:
        stats += _stat_row("Address", address)

    spese_block = ""
    if spese_str:
        spese_block = (
            f'<div style="color:#8892aa;font-size:11px;margin:1px 0">{spese_str}'
            f'{(" &nbsp;"+total_str) if total_str else ""}</div>'
        )

    return f"""<div style="background:#1a1d27;border:1px solid #2e3250;
                           border-radius:8px;overflow:hidden;height:100%">
  {img_block}
  <div style="padding:12px">

    <!-- score + neighbourhood -->
    <table style="border-collapse:collapse;width:100%;margin-bottom:10px"><tr>
      <td style="vertical-align:middle;padding-right:8px;width:1%">
        <div style="background:{sc};color:#fff;font-weight:800;font-size:17px;
                    border-radius:6px;padding:4px 10px;white-space:nowrap;
                    text-align:center">{score}</div>
      </td>
      <td style="vertical-align:middle">
        <div style="color:#e2e8f0;font-weight:700;font-size:13px;
                    line-height:1.3">{neigh}</div>
        <div style="color:#8892aa;font-size:10px;margin-top:1px">
          Fascia {fascia}
          <span style="background:rgba(79,142,247,0.2);color:#7eb5ff;
                       border-radius:3px;padding:1px 5px;margin-left:4px;
                       font-size:10px">Milano · Affitto</span>
        </div>
      </td>
    </tr></table>

    <!-- price -->
    <div style="color:#e2e8f0;font-size:17px;font-weight:800;
                margin-bottom:2px;line-height:1.2">{rent_str}</div>
    {spese_block}

    <!-- divider -->
    <div style="border-top:1px solid #2e3250;margin:10px 0 8px"></div>

    <!-- stats -->
    <table style="border-collapse:collapse;width:100%;margin-bottom:12px">
      {stats}
    </table>

    <!-- CTA -->
    <a href="{url}"
       style="display:inline-block;background:#4f8ef7;color:#fff;
              text-decoration:none;border-radius:5px;padding:7px 16px;
              font-size:12px;font-weight:700">View listing →</a>
  </div>
</div>"""


def _build_html(listings: list) -> str:
    date_str = datetime.now().strftime("%d %b %Y %H:%M")
    n        = len(listings)
    plural   = "s" if n != 1 else ""

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
<body style="background:#0f1117;color:#e2e8f0;margin:0;padding:0">
<div style="max-width:680px;margin:0 auto;padding:24px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">

  <!-- Header -->
  <table style="border-collapse:collapse;width:100%;margin-bottom:20px"><tr>
    <td style="vertical-align:middle">
      <h2 style="margin:0 0 3px;color:#4f8ef7;font-size:22px">
        🏠 {n} new Milano rental{plural}
      </h2>
      <p style="margin:0;color:#8892aa;font-size:13px">
        {date_str} ·
        <a href="http://localhost:8000/#rentals"
           style="color:#4f8ef7;text-decoration:none">Open dashboard ↗</a>
      </p>
    </td>
  </tr></table>

  <!-- 2-column grid -->
  <table style="border-collapse:collapse;width:100%">
    {rows}
  </table>

  <p style="color:#4a5568;font-size:11px;margin:8px 0 0;text-align:center">
    Immobiliare Scorer · adjust digest filters in the dashboard
  </p>
</div>
</body>
</html>"""


def _build_text(listings: list) -> str:
    date_str = datetime.now().strftime("%d %b %Y %H:%M")
    n        = len(listings)
    lines    = [
        f"{n} new Milano rental{'s' if n != 1 else ''} — {date_str}",
        "Dashboard: http://localhost:8000/#rentals",
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
    subject  = f"🏠 {n} new Milano rental{'s' if n != 1 else ''} — {date_str}"

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
