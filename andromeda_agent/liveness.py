"""Proving that a specific process is still alive.

Two callers need this and they must answer it the same way: the execution
ledger, deciding whether an interrupted job attempt was abandoned, and the
live-session registry, deciding whether a session another terminal claims to
be holding is really still open.

**The proof is pid plus process start time, never the pid alone.** Pids wrap.
A sweep that sees "pid 4213 is gone" on a machine that has since handed 4213
to a text editor will call a live thing abandoned — or, worse, call a dead one
live and never clean it up. The start time is the discriminator: same pid,
different start time, different process.

Being unable to *prove* death never rewrites state. Where the start time is
unknowable the answer is "alive", because the failure direction that costs
something is reaping a row whose owner is still working.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive and owned by somebody else. Alive is what the caller asked.
        return True
    except OSError:
        return True
    return True


def process_start_time(pid: int) -> int | None:
    """A stamp that changes when a pid is reused, or None if unknowable."""
    proc = Path("/proc") / str(pid) / "stat"
    if proc.exists():  # Linux
        try:
            fields = proc.read_text(encoding="utf-8").rsplit(") ", 1)[-1].split()
            return int(fields[19])  # starttime, in clock ticks since boot
        except (OSError, IndexError, ValueError):
            return None
    try:  # macOS and the BSDs
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stamp = (completed.stdout or "").strip()
    return hash(stamp) if stamp else None


def owner_is_live(pid: int, started_at: int | None) -> bool:
    if not pid_exists(pid):
        return False
    if started_at is None:
        # We cannot prove it is a different process, so we do not claim it is.
        return True
    current = process_start_time(pid)
    return current is not None and current == started_at
