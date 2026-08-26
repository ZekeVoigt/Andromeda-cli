"""The full-screen surface.

`andromeda --tui`. The REPL in `andromeda_cli/repl.py` is unchanged and still
the default; see `app.py` for why this is Python rather than Ink over an IPC
gateway.

Two things are enforced at this boundary rather than deeper in, because they
are decisions about *which surface to use* and not about how it draws:

**A tty is not a pipe.** A full-screen app writes cursor moves and an alternate
screen buffer. `andromeda --tui "..." > notes.md` must not produce that, and
neither must a run with no terminal at all. The surface refuses instead of
degrading, because degrading silently is how someone ends up with escape codes
committed to a file.

**Textual is imported lazily.** It is a real dependency, declared in
`pyproject.toml`, but an install that predates it — an older checkout, an
interrupted `andromeda update` — should say what is missing rather than dying
with a traceback on `import textual` before the CLI can print anything.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Any

from andromeda_agent import AgentError, build_provider
from andromeda_cli import config as config_module
from andromeda_cli import output
from andromeda_cli.session import build_conversation, ended as session_ended

__all__ = ["available", "run"]


def available() -> tuple[bool, str]:
    """Whether the TUI can run here, and why not if it cannot."""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False, (
            "The full-screen interface needs `textual`, which is not installed "
            "in this environment."
        )
    return True, ""


def _is_screen() -> bool:
    """Both ends. Output alone is not enough — the TUI reads keys too, and a
    run with piped stdin has nobody to press them."""
    try:
        return sys.stdout.isatty() and sys.stdin.isatty()
    except (OSError, ValueError):
        # stdin can be closed, detached or replaced by something that refuses
        # to be asked. None of those are a terminal.
        return False


def run(
    config: dict[str, Any],
    workspace_root: str | None = None,
    resume=None,
) -> int:
    if not _is_screen():
        output.fail(
            "The full-screen interface needs a terminal on both stdin and stdout.",
            'Use the REPL, or pass a prompt: andromeda "your question"',
        )
        return 2

    ok, reason = available()
    if not ok:
        output.fail(reason, "Run `andromeda update`, or use the REPL without --tui.")
        return 2

    try:
        provider = build_provider(config)
    except AgentError as exc:
        output.agent_error(exc)
        return 1

    conversation, record = build_conversation(
        config,
        provider,
        interactive=True,
        workspace_root=workspace_root,
        session=resume,
        surface="tui",
    )

    # Same reason as the REPL: an editor that types into new terminals would
    # otherwise have its `source .../activate` delivered to the composer as
    # the session's first message.
    from andromeda_cli import bootstrap

    bootstrap.drain_pending_input()

    # Imported here, after the guards. `app` pulls in Textual, and the whole
    # point of `available()` is to report a missing dependency rather than
    # raise on the way to reporting it.
    from .app import AndromedaApp

    # A resumed session replaces the transcript wholesale, including its
    # original system message — `build_conversation` has already done that.
    app = AndromedaApp(config, conversation, record, resumed=resume)
    started = time.time()
    try:
        app.run()
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        session_ended(conversation, completed=False)
        _record_exit(started, f"{type(exc).__name__}: {exc}")
        raise
    session_ended(conversation)
    _record_exit(started, "clean")
    return 0


def _record_exit(started: float, reason: str) -> None:
    """Append one line saying how this session ended.

    Because "it opened and then closed a second later" is a report nobody can
    act on, and a full-screen app takes the terminal with it when it goes —
    whatever it printed on the way out is wiped by the screen restore. A line
    on disk survives that.

    Deliberately tiny and deliberately best-effort: this runs on the way out of
    a session that may already be failing, and a logger that can fail is one
    more thing to debug at the exact moment nobody wants another one.
    """
    try:
        path = config_module.home() / "tui.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  after {time.time() - started:.1f}s  {reason}\n")
    except OSError:
        pass
