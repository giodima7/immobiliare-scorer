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

# A fresh daemon is ~150 MB; a rescore of the ~20k-listing file peaks
# around 1.1 GB. Two daemons at the limit still leave a 16 GB Mac room.
LIMIT_MB = float(os.environ.get("LUME_DAEMON_MAX_MB") or 2500)

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
