"""The stop button for work nobody is watching.

`andromeda pause` writes a file. While it exists, the scheduler stops firing
jobs. `andromeda resume` deletes it and the next tick picks up — no restart,
no editing a config, nothing to remember to undo somewhere else.

Three decisions, and each is the difference between a stop button and a
liability:

**It pauses new work, never running work.** A job that is half-way through a
deployment is not made safer by being killed mid-write. `pause` is a hold on
starting, and it says so.

**It does not touch your own terminal.** A REPL, a one-shot, an editor session
— all of those are a person asking for something while watching the answer.
Pausing those would be a stop button that takes away the tool you need to
find out what is wrong.

**An unreadable sentinel counts as engaged.** If the file cannot be read —
a permission problem, a filesystem going strange — the answer is *paused*.
Failing open here would lift somebody's emergency stop at exactly the moment
the machine is misbehaving, which is the moment they engaged it.

A file rather than a flag in the config, because it has to be settable by
anything: another terminal, a script, a `touch` over SSH from a phone. An
empty file made by `touch` still pauses; the JSON inside is a courtesy, not
the mechanism.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENTINEL_NAME = "PAUSED"

logger = logging.getLogger(__name__)

# Components that have already said they are paused, so a tick loop logs once
# per engagement rather than once per tick.
_announced: set[str] = set()


def sentinel(home: Path) -> Path:
    return Path(home) / SENTINEL_NAME


def engaged(home: Path) -> bool:
    """One stat. Cheap enough to call every tick, which is the point."""
    try:
        return sentinel(home).exists()
    except OSError:
        # Unreadable is paused. See the module docstring — this is the one
        # place a fail-open would be actively dangerous.
        return True


def engage(home: Path, reason: str = "") -> Path:
    """Write the sentinel. Idempotent; re-engaging refreshes the reason."""
    path = sentinel(home)
    payload = {
        "engaged_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "reason": reason or None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # A partial or empty sentinel still pauses. The file existing is the
        # mechanism; what is in it is only for the person reading it later.
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
    return path


def disengage(home: Path) -> bool:
    """Remove the sentinel. Returns whether a pause was actually lifted."""
    try:
        sentinel(home).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def state(home: Path) -> dict[str, Any] | None:
    """`{"reason", "engaged_at"}` when paused, or None.

    A sentinel with an unreadable body still reports paused, with both fields
    empty: the pause is authoritative and the metadata is not.
    """
    path = sentinel(home)
    try:
        if not path.exists():
            return None
    except OSError:
        return {"reason": "", "engaged_at": ""}

    reason = ""
    engaged_at = ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            reason = str(raw.get("reason") or "")
            engaged_at = str(raw.get("engaged_at") or "")
    except (OSError, ValueError):
        pass
    return {"reason": reason, "engaged_at": engaged_at}


def describe(home: Path) -> str:
    """One line for a banner or a doctor report, or "" when running."""
    current = state(home)
    if current is None:
        return ""
    reason = current.get("reason")
    detail = f" ({reason})" if reason else ""
    return f"paused{detail} — scheduled jobs are on hold · andromeda resume"


def check(home: Path, component: str) -> bool:
    """Whether `component` should hold, saying so once per engagement.

    Dispatch loops call this every tick. The line fires on the transition into
    paused and re-arms after a resume, so a pause over a weekend is one line
    rather than fifty thousand.
    """
    if not engaged(home):
        _announced.discard(component)
        return False

    if component not in _announced:
        _announced.add(component)
        current = state(home) or {}
        reason = current.get("reason")
        logger.info(
            "%s is paused%s — andromeda resume (%s)",
            component,
            f" ({reason})" if reason else "",
            sentinel(home),
        )
    return True


def reset_for_tests() -> None:
    _announced.clear()
