"""When the scanners may open browser pages: at night only.

The user, 2026-10-24: "I can't work with the PC opening pages all day, it
has to run only at night." Everything that opens a page — zone slots, the
new-listing search, listing pages, sales sweeps, the Subito pass — runs
inside the window; outside it both daemons sleep (photo reading, which
opens nothing, may still run: fetch_rentals._sleep_outside_window).

Edit scan_window.json — {"start": "23:30", "end": "07:30"} — to move it;
it is read on every check, no restart needed. A window may cross midnight.
Inside the window the daemons keep the Mac from idle-sleeping (caffeinate
-i); it still has to be on power with the lid open.

A site that blocks us pauses as a whole — zone slots, discover, listing
pages — not just the page that saw the block (2026-10-25, Idealista: the
listing-page drip paused itself but the zone and discover searches kept
going). .source_pause.json holds {source: {"until", "why"}}; pause() never
shortens one.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "scan_window.json"
PAUSE_PATH = Path(__file__).resolve().parent / ".source_pause.json"
DEFAULT = ("23:30", "07:30")


def _hm(s: str) -> dtime:
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(h % 24, m % 60)


def window() -> tuple[dtime, dtime]:
    try:
        d = json.loads(CONFIG_PATH.read_text())
        return _hm(d["start"]), _hm(d["end"])
    except (OSError, ValueError, KeyError, TypeError):
        return _hm(DEFAULT[0]), _hm(DEFAULT[1])


def in_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start, end = window()
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end            # crosses midnight


def _at(day: datetime, t: dtime) -> datetime:
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def closes_at(now: datetime | None = None) -> datetime:
    """End of the window `now` is in (or of the next one)."""
    now = now or datetime.now()
    start, end = window()
    close = _at(now, end)
    if close <= now:
        close += timedelta(days=1)
    return close


def next_open(now: datetime | None = None) -> datetime:
    """`now` when inside the window, else when it next opens."""
    now = now or datetime.now()
    if in_window(now):
        return now
    start = _at(now, window()[0])
    return start if start > now else start + timedelta(days=1)


def hours() -> float:
    start, end = window()
    a, b = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    return ((b - a) % (24 * 60) or 24 * 60) / 60


def keep_awake(log=print) -> None:
    """No idle sleep until the window closes (macOS caffeinate; a process
    assertion that ends with it — no system setting is changed)."""
    secs = int((closes_at() - datetime.now()).total_seconds())
    if secs <= 0:
        return
    try:
        subprocess.Popen(["caffeinate", "-i", "-t", str(secs)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"[window] caffeinate unavailable: {exc}")


def describe() -> str:
    start, end = window()
    return f"{start:%H:%M}–{end:%H:%M}"


def paused_until(source: str) -> float:
    """Epoch seconds until which `source` opens no page (0 when not paused)."""
    try:
        d = json.loads(PAUSE_PATH.read_text()).get(source) or {}
        return datetime.fromisoformat(d["until"]).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def pause(source: str, until: datetime, why: str, log=print) -> None:
    if paused_until(source) >= until.timestamp():
        return
    try:
        d = json.loads(PAUSE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    d[source] = {"until": until.isoformat(timespec="minutes"), "why": why,
                 "at": datetime.now().isoformat(timespec="minutes")}
    PAUSE_PATH.write_text(json.dumps(d, indent=1))
    log(f"[window] {source} paused until {until:%d %b %H:%M} — {why}")
