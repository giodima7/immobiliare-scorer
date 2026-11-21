"""
memory_guard.py
───────────────
The scanner daemons run for weeks. After each slot they log their memory,
and one that has grown past LIMIT_MB restarts itself in place — os.execv,
same PID, so api.py keeps tracking it and its stop button still works —
instead of dragging the Mac into swap.

Why (2026-11-21): nodriver kept every browser session alive (fixed in
browser_session.stop), so the daemons grew by GBs a day: the Idealista one
reached a 47 GB footprint on a 16 GB Mac, swap filled up, everything
slowed down and the Immobiliare one was killed on 2026-11-16 without
anybody noticing for 4.5 days. This catches the next leak, whatever it is.

The measure is phys_footprint (Activity Monitor's "Memory"), not RSS: a
swapped-out process looks small by RSS — 1.4 GB RSS for that 47 GB one.
LUME_DAEMON_MAX_MB overrides the limit.
"""

from __future__ import annotations

import ctypes
import os
import sys

# Measured after the 2026-11-21 restart: a fresh daemon ~150 MB; the
# Immobiliare one peaked at 2.2–2.4 GB catching up 4.5 days (sales sweep,
# rescores) and settled around 1.1 GB; Idealista ~0.6 GB. 4 GB leaves those
# peaks alone and still stops a leak (3.5–6 GB a day) within a day; two
# daemons at the limit plus api.py still fit a 16 GB Mac. The [memory] log
# lines are there to retune it.
LIMIT_MB = float(os.environ.get("LUME_DAEMON_MAX_MB") or 4000)

_RUSAGE_INFO_V2 = 2
_PHYS_FOOTPRINT_OFFSET = 72   # rusage_info_v2: uuid[16], then 7 uint64 before ri_phys_footprint


def footprint_mb(pid: int | None = None) -> float | None:
    """A process's phys_footprint in MB (macOS); None when unavailable."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(512)
        if libproc.proc_pid_rusage(pid or os.getpid(), _RUSAGE_INFO_V2, buf) != 0:
            return None
        raw = buf.raw[_PHYS_FOOTPRINT_OFFSET:_PHYS_FOOTPRINT_OFFSET + 8]
        return int.from_bytes(raw, "little") / 2**20
    except Exception:
        return None


def check(name: str, log=print) -> None:
    """Call between slots (no browser open): log the footprint, and re-exec
    this daemon when it is over the limit. Its schedule lives in files
    (last slot, discover, drip state), so the new process carries on."""
    mb = footprint_mb()
    if mb is None:
        return
    log(f"[memory] {name} daemon {mb:.0f} MB")
    if mb < LIMIT_MB:
        return
    log(f"[memory] over {LIMIT_MB:.0f} MB — restarting the {name} daemon in place")
    try:
        from browser_session import _stop_all
        _stop_all()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-u", *sys.argv])


# macOS's malloc keeps freed large blocks (> ~1 MB) in a per-process cache
# for reuse, and phys_footprint counts them. Every json.loads of a big file
# frees a few: the bytes read, then the str — 269 MB for the 70 MB detail
# cache, which holds 78 emoji, so the whole str is 4 bytes a character.
# Measured 2026-11-21 (Python 3.14.0, macOS 26.5.1), in a fresh process:
#   10 loads of the detail cache: +2,562 MB kept → +7 MB with the cache off
#   20 detail-cache saves:        +2,261 MB kept → +0 MB, 687 vs 761 ms a save
#   api.py replay (autosync push + one tab polling /scanner-status for
#   150 s): 3,780 MB at the end → 424 MB
# That cache is how api.py reached a 13 GB footprint after the restart that
# night. malloc_zone_pressure_relief() frees none of it; PYTHONMALLOC=malloc
# is worse. libmalloc reads MallocLargeCache only when a process starts,
# hence the re-exec. Children (the daemons api.py starts, and their Chrome)
# inherit it, so they don't re-exec.
MALLOC_ENV = {"MallocLargeCache": "0"}


def malloc_env() -> None:
    """Re-exec this process once with MALLOC_ENV set (macOS only). Call it
    first thing in a long-running entry point; LUME_MALLOC_CACHE=1 skips it."""
    if sys.platform != "darwin" or os.environ.get("LUME_MALLOC_CACHE") == "1":
        return
    if all(os.environ.get(k) == v for k, v in MALLOC_ENV.items()):
        return
    os.environ.update(MALLOC_ENV)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
