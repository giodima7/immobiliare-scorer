"""When the scanners may open browser pages.

Since 2026-10-30: weekdays 00:00–18:30, never on Saturday or Sunday (the
user: "the hours should be 00:00–18:30 every day except Saturday and
Sunday"). 2026-10-26 to 10-30: weekdays 07:30–18:00 — the Mac spent whole
days asleep with its lid shut. Before, at night only (23:30–07:30).

The user, 2026-10-24: "I can't work with the PC opening pages all day, it
has to run only at night." Everything that opens a page — zone slots, the
new-listing search, listing pages, sales sweeps, the Subito pass — runs
inside the window; outside it both daemons sleep (photo reading, which
opens nothing, may still run: fetch_rentals._sleep_outside_window).

Edit scan_window.json — {"start": "23:30", "end": "07:30"} — to move it;
it is read on every check, no restart needed. A window may cross midnight.
One-off extra windows go in "extra": [{"from": "2026-10-27T23:25", "to":
"2026-10-28T06:30"}] — 2026-10-27, the user: catch up on a day the Mac
spent asleep with the lid closed. Past ones are simply ignored.
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


def days() -> set[int]:
    """Weekdays the window opens on (Monday 0 … Sunday 6); all when unset."""
    try:
        return {int(x) % 7 for x in json.loads(CONFIG_PATH.read_text()).get("days", range(7))}
    except (OSError, ValueError, TypeError):
        return set(range(7))


def extra() -> list[tuple[datetime, datetime]]:
    """One-off windows on top of the weekly one (see the module doc)."""
    try:
        return [(datetime.fromisoformat(e["from"]), datetime.fromisoformat(e["to"]))
                for e in json.loads(CONFIG_PATH.read_text()).get("extra") or []]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def in_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return _in_weekly(now) or any(a <= now < b for a, b in extra())


def _in_weekly(now: datetime) -> bool:
    start, end = window()
    t, on = now.time(), days()
    if start <= end:
        return now.weekday() in on and start <= t < end
    # crosses midnight: the window belongs to the day it opened
    return (t >= start and now.weekday() in on) or (t < end and (now - timedelta(days=1)).weekday() in on)


def _at(day: datetime, t: dtime) -> datetime:
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def next_open(now: datetime | None = None) -> datetime:
    """`now` when inside the window, else when it next opens."""
    now = now or datetime.now()
    if in_window(now):
        return now
    start, on = window()[0], days()
    weekly = _at(now + timedelta(days=1), start)
    for k in range(8):
        cand = _at(now + timedelta(days=k), start)
        if cand > now and cand.weekday() in on:
            weekly = cand
            break
    return min([weekly] + [a for a, _ in extra() if a > now])


def closes_at(now: datetime | None = None) -> datetime:
    """End of the window `now` is in (or of the next one)."""
    now = now or datetime.now()
    t = now if in_window(now) else next_open(now)
    for _ in range(10):                  # an extra window may run into the weekly one
        ends = [b for a, b in extra() if a <= t < b]
        if _in_weekly(t):
            close = _at(t, window()[1])
            ends.append(close + timedelta(days=1) if close <= t else close)
        if not ends or not in_window(t := max(ends)):
            return t
    return t


def _extra_at(now: datetime) -> tuple[datetime, datetime] | None:
    """The one-off window `now` is in, outside the weekly one."""
    if _in_weekly(now):
        return None
    return next(((a, b) for a, b in extra() if a <= now < b), None)


def in_extra(now: datetime | None = None) -> bool:
    return _extra_at(now or datetime.now()) is not None


def opened_at(now: datetime | None = None) -> datetime | None:
    """When the window `now` is in opened (None outside every window)."""
    now = now or datetime.now()
    ex = _extra_at(now)
    if ex:
        return ex[0]
    if not _in_weekly(now):
        return None
    s = _at(now, window()[0])
    return s if s <= now else s - timedelta(days=1)    # a window across midnight


def slot_hours(now: datetime | None = None) -> float:
    """The hours the zone slots are spread over: the weekly window's — or,
    inside a one-off catch-up window, that window's, so every zone comes
    round before it closes (2026-10-30, the user: "catch up, working them
    flat out until midnight")."""
    ex = _extra_at(now or datetime.now())
    return (ex[1] - ex[0]).total_seconds() / 3600 if ex else hours()


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
    on = days()
    d = "" if len(on) == 7 else " Mon–Fri" if on == {0, 1, 2, 3, 4} else " days " + ",".join(map(str, sorted(on)))
    return f"{start:%H:%M}–{end:%H:%M}{d}"


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
