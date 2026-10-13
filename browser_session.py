"""
browser_session.py
──────────────────
One place that owns the scrapers' browser lifecycle. Replaces bare
`uc.start(...)` calls, fixing three failure modes seen in the logs:

  • nodriver launches the browser itself and gives it ~2.75 s to open its
    debug port. A cold start — or two daemons starting together — misses
    that: "Failed to connect to browser", the scan is lost and the browser
    is left running (2026-10-13). Here we launch it ourselves, wait up to
    30 s for the port, then attach.
  • nodriver waits for every browser reply forever. One stuck page froze
    the Immobiliare daemon from 2026-08-29 until 2026-10-13. `timed()` puts
    a deadline on every navigation / evaluate; callers restart the session
    on PageTimeout.
  • A killed daemon left its browser orphaned. Sessions clean up on exit
    and SIGTERM, and kill leftovers of their own profile before launching.

Profiles persist under ~/.lume-scanner/profiles/<name>/ so the sites'
anti-bot clearance cookies survive between runs — fresh temp profiles had
to pass the challenge on every scan.

Browser: $BROWSER_EXECUTABLE_PATH, else Chrome (current, auto-updating),
else Edge. The installed Edge is v122 (March 2024) — an outdated browser
is itself a bot signal.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import nodriver as uc

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDGE_PATH   = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

PROFILE_ROOT = Path(os.environ.get("LUME_PROFILE_ROOT")
                    or Path.home() / ".lume-scanner" / "profiles")

NAV_TIMEOUT    = 45    # s — page navigation
EVAL_TIMEOUT   = 20    # s — JS evaluate / small CDP calls
LAUNCH_TIMEOUT = 30    # s — browser debug port must answer within this


def browser_executable() -> str:
    env = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if env:
        return env
    return CHROME_PATH if Path(CHROME_PATH).exists() else EDGE_PATH


class PageTimeout(Exception):
    """A browser call missed its deadline — the session is probably wedged."""


async def timed(aw, seconds: float = EVAL_TIMEOUT, what: str = "browser call"):
    """Await a nodriver call with a deadline. Raises PageTimeout."""
    try:
        return await asyncio.wait_for(aw, seconds)
    except asyncio.TimeoutError:
        raise PageTimeout(f"{what} timed out after {seconds:.0f}s") from None


# ── Cleanup on exit / SIGTERM ────────────────────────────────────────────────
_ACTIVE: set["BrowserSession"] = set()


def _stop_all() -> None:
    for s in list(_ACTIVE):
        s.stop()


def _on_sigterm(signum, frame):
    # api.py's Stop button / orphan sweep sends SIGTERM. Python's default
    # handler exits without running finally/atexit → the browser outlived
    # the daemon. Stop browsers first, then exit just as hard as before:
    # os._exit, not SystemExit — the daemon loops catch BaseException to
    # survive crashes and would swallow a SystemExit, ignoring Stop.
    _stop_all()
    os._exit(128 + signum)


atexit.register(_stop_all)
try:
    signal.signal(signal.SIGTERM, _on_sigterm)
except ValueError:
    pass   # not the main thread (e.g. imported by api.py) — atexit still applies


def _kill_profile_leftovers(profile_dir: Path) -> int:
    """Kill browser processes still holding `profile_dir` (a previous run
    killed hard). Chrome refuses a second instance on a locked profile — it
    hands the URL to the old process and exits, so our port never opens."""
    marker = f" --user-data-dir={profile_dir} "   # exact: "immobiliare" ≠ "immobiliare-sales"
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if marker in f" {cmd} " and "--type=" not in cmd:   # main process only
            try:
                os.kill(int(pid_s), signal.SIGTERM)
                killed += 1
            except (ProcessLookupError, ValueError, PermissionError):
                pass
    if killed:
        time.sleep(2)
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile_dir / lock).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return killed


def _port_answers(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=timeout) as r:
            return bool(json.loads(r.read() or b"{}").get("Browser"))
    except Exception:
        return False


class BrowserSession:
    """
    usage:
        session = BrowserSession("immobiliare")
        await session.start()
        tab = await session.get(url)
        raw = await timed(tab.evaluate("..."), what="read __NEXT_DATA__")
        ...
        await session.restart()   # after PageTimeout / dead connection
        session.stop()
    """

    def __init__(self, name: str, lang: str = "it-IT"):
        self.name    = name
        self.lang    = lang
        self.profile = PROFILE_ROOT / name
        self.browser: uc.Browser | None = None
        self._proc:   subprocess.Popen | None = None
        self._port:   int | None = None

    async def start(self, tries: int = 3) -> uc.Browser:
        exe = browser_executable()
        self.profile.mkdir(parents=True, exist_ok=True)
        last_err = None
        for attempt in range(1, tries + 1):
            n = _kill_profile_leftovers(self.profile)
            if n:
                print(f"  [browser] killed {n} leftover {self.name} browser process(es)", flush=True)
            self._port = uc.util.free_port()
            cfg = uc.Config(user_data_dir=str(self.profile), browser_executable_path=exe,
                            headless=False, lang=self.lang)
            cfg.host, cfg.port = "127.0.0.1", self._port
            args = cfg() + [f"--lang={self.lang}"]
            self._proc = subprocess.Popen([exe, *args], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + LAUNCH_TIMEOUT
            while time.monotonic() < deadline and self._proc.poll() is None:
                if _port_answers(self._port):
                    break
                await asyncio.sleep(0.5)
            if self._proc.poll() is None and _port_answers(self._port):
                try:
                    self.browser = await timed(uc.start(config=cfg), EVAL_TIMEOUT,
                                               "attach to browser")
                    _ACTIVE.add(self)
                    print(f"  [browser] {Path(exe).name} ready ({self.name}, "
                          f"attempt {attempt})", flush=True)
                    return self.browser
                except Exception as exc:
                    last_err = exc
            else:
                last_err = RuntimeError(
                    f"debug port {self._port} not answering after {LAUNCH_TIMEOUT}s"
                    + (f" (browser exited {self._proc.returncode})"
                       if self._proc.poll() is not None else ""))
            print(f"  [browser] start attempt {attempt}/{tries} failed: {last_err}", flush=True)
            self.stop()
            await asyncio.sleep(3 * attempt)
        raise RuntimeError(f"browser failed to start after {tries} attempts: {last_err}")

    async def restart(self) -> uc.Browser:
        print(f"  [browser] restarting {self.name} session", flush=True)
        self.stop()
        await asyncio.sleep(2)
        return await self.start()

    async def alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and self._port is not None
                and await asyncio.to_thread(_port_answers, self._port))

    async def minimize(self) -> None:
        """Minimize the window (it stays a real, headed browser — headless
        fails the sites' bot checks). Best effort."""
        try:
            from nodriver import cdp
            tab = self.browser.main_tab
            wid, _ = await timed(tab.send(cdp.browser.get_window_for_target()),
                                 what="get window")
            await timed(tab.send(cdp.browser.set_window_bounds(
                wid, cdp.browser.Bounds(window_state=cdp.browser.WindowState.MINIMIZED))),
                what="minimize window")
        except Exception as exc:
            print(f"  [browser] could not minimize {self.name}: {exc}", flush=True)

    async def get(self, url: str, timeout: float = NAV_TIMEOUT):
        return await timed(self.browser.get(url), timeout, f"GET {url}")

    def stop(self) -> None:
        _ACTIVE.discard(self)
        if self.browser is not None:
            try:
                asyncio.get_running_loop()
                self.browser.stop()   # closes the CDP connection (we own the process)
            except Exception:
                pass   # no loop (atexit / SIGTERM): terminating the process closes it
            self.browser = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
