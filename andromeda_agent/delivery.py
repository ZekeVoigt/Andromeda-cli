"""Telling someone a job ran.

The failure mode this exists to prevent is the quiet one: a scheduler that
works perfectly and that nobody ever hears from. A job fires at 6am, produces
a good answer, writes it into a state file, and the person it was for finds out
weeks later that it has been running.

Two halves, and keeping them apart is the point:

- **The output file is always written.** It is the record, and no setting turns
  it off. Losing a job's work to a delivery preference is a bug nobody would
  ever think to look for.
- **`deliver` is only about being *told*.** `none` (look when you want),
  `notify` (an OS notification), `stdout` (the daemon prints it, which is what
  you want when the daemon is in a terminal you are watching).

Notification is best-effort by construction. A desktop notification that fails
must not fail the job — the work is done and recorded, and turning "I could not
find `notify-send`" into a failed run would make the history lie.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

MAX_NOTIFICATION = 240
HEADER = "# {name}\n\n*{when} · {status}*\n\n"

# The sentinel a job emits when it has nothing worth waking anyone for. Output
# is still saved — this suppresses *delivery*, never the record.
#
# The matcher is deliberately looser than "the whole response equals the
# token", because the prompt asks for the marker and real models bracket it
# with a newline or a short note. It is deliberately tighter than "the token
# appears anywhere", because "I considered replying [SILENT] but here is the
# summary" is a genuine report and must be delivered. Whole response, first
# line, or last line — nothing else.
SILENCE_TOKENS = ("[silent]", "silent", "[no reply]", "no reply", "no_reply")


def is_silence(text: str) -> bool:
    lines = [line.strip().lower() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1 and lines[0] in SILENCE_TOKENS:
        return True
    return lines[0] in SILENCE_TOKENS or lines[-1] in SILENCE_TOKENS


def write_output(path: Path, name: str, status: str, body: str, when: float) -> Path:
    """The durable record of one run.

    Markdown with a header, because the body already is markdown — it is what
    the model wrote — and because a directory of these is something you can
    read, grep and diff without the CLI.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(
        HEADER.format(name=name, when=stamp, status=status) + (body or "(no output)\n"),
        encoding="utf-8",
    )
    return path


def deliver(
    mode: str,
    name: str,
    body: str,
    ok: bool = True,
    target: str = "",
    session: str = "",
) -> str:
    """Announce a run. Returns what was done, for the run record."""
    if mode == "webhook":
        return "posted" if _webhook(target, name, body, ok) else "webhook failed"

    if mode == "stdout":
        # Straight to the real stdout, not through the render layer: the daemon
        # may be running under launchd with its output going to a log file, and
        # a rendered panel in a log file is a screenshot of a panel.
        sys.stdout.write(f"\n=== {name} ===\n{body}\n")
        sys.stdout.flush()
        return "stdout"

    if mode == "notify":
        return "notified" if _notify(name, body, ok, session) else "notify unavailable"

    sender = _plugin_modes().get(mode)
    if sender is not None:
        # Best-effort, exactly like `notify`. The output file is already
        # written by the time this runs, so a plugin that cannot reach its
        # service costs the announcement and never the work — and a raising
        # sender must not turn a successful run into a failed one in the
        # history.
        try:
            delivered = bool(sender(name=name, body=body, ok=ok, target=target))
        except Exception as exc:  # noqa: BLE001 - see above
            return f"{mode} failed: {exc}"
        return mode if delivered else f"{mode} failed"

    return ""


def _plugin_modes() -> dict[str, "Callable[..., bool]"]:
    """Delivery modes a plugin registered, or nothing.

    Consulted after the built-ins so `webhook`, `stdout` and `notify` cannot be
    shadowed — a plugin quietly taking over `notify` would be a plugin reading
    the output of every scheduled job the user thought was going to the desktop.
    """
    try:
        from . import plugins as plugins_module
    except ImportError:  # pragma: no cover - half-installed package
        return {}
    return plugins_module.delivery_modes()


def resume_command(session: str) -> str:
    """The command that opens a run's own conversation.

    One definition, used by the clickable notification, by the fallback text
    inside an unclickable one, and by anything else that needs to hand somebody
    the way back. A notification that says a job finished and gives no route to
    what it said is a notification people learn to swipe away.
    """
    return f"andromeda --resume {session}" if session else ""


def _notify(title: str, body: str, ok: bool, session: str = "") -> bool:
    summary = " ".join((body or "").split())[:MAX_NOTIFICATION] or "(no output)"
    prefix = "" if ok else "failed — "
    resume = resume_command(session)

    if sys.platform == "darwin":
        # `terminal-notifier` first, and only for its one irreplaceable
        # property: `-execute` makes the notification *clickable*, opening the
        # conversation the run actually happened in. `osascript`'s
        # `display notification` cannot carry an action at all — clicking it
        # does nothing, which is the complaint.
        notifier = shutil.which("terminal-notifier")
        if notifier and resume:
            # Through Terminal rather than executed directly: the point is to
            # land the person in an interactive session they can type into, and
            # a bare `-execute` would run it with no terminal attached.
            script = (
                'tell application "Terminal" to do script '
                f'{_applescript(resume)}\ntell application "Terminal" to activate'
            )
            return _run(
                [
                    notifier,
                    "-title",
                    f"Andromeda · {title}",
                    "-message",
                    prefix + summary,
                    "-execute",
                    f"osascript -e {shlex.quote(script)}",
                ]
            )

        # No notifier, or no session to open. Say the command in the body so it
        # can at least be read and typed.
        message = prefix + summary
        if resume:
            message = f"{message}  ·  {resume}"
        script = (
            f'display notification {_applescript(message)} '
            f'with title {_applescript("Andromeda · " + title)}'
        )
        return _run(["osascript", "-e", script])

    if shutil.which("notify-send"):
        message = prefix + summary
        if resume:
            message = f"{message}\n{resume}"
        return _run(["notify-send", f"Andromeda · {title}", message])

    return False


def _applescript(text: str) -> str:
    """Quote for AppleScript.

    Backslash first, or escaping the quotes would then have their backslashes
    escaped again. A job name is arbitrary text the user typed, so this is a
    real quoting problem and not a formality.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _run(command: list[str]) -> bool:
    try:
        completed = subprocess.run(command, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _webhook(url: str, name: str, body: str, ok: bool) -> bool:
    """POST the run to a URL. The generic answer to "tell something else".

    Reaching Signal, Telegram, Discord and the rest natively would mean a
    messaging gateway. There is no gateway here, and building nine adapters to
    avoid one HTTP request would be building one. A webhook reaches all of them
    through whatever the person already runs.

    JSON, and best-effort: a delivery failure must not fail a run that worked.
    """
    if not url:
        return False
    payload = json.dumps(
        {"job": name, "ok": ok, "text": body or "", "source": "andromeda-cli"}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Andromeda-CLI/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False
