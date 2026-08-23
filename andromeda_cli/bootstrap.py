"""Process-level setup that must run before anything else imports.

Imported first by ``__main__``. On Windows the default stdio encoding is the
active code page, which mangles every non-ASCII byte the agent streams; the
reconfigure below is a no-op on POSIX. Keep this module dependency-free so it
cannot itself fail on a half-installed venv.
"""

from __future__ import annotations

import sys


def drain_pending_input(stream=None) -> int:
    """Discard anything sitting in the terminal's input buffer.

    Called once, immediately before the first prompt is drawn. Input that
    arrived *before* we asked for any was not addressed to us.

    This is not hypothetical tidiness. Editors and shell integrations type into
    new terminals: VS Code's Python extension writes
    `source .../.venv/bin/activate` into any terminal where it detects a venv,
    direnv and conda hooks do similar things, and a terminal multiplexer can
    replay a buffer on attach. If that lands while the REPL is starting,
    `prompt_toolkit` reads it as a line and submits it — so a session opens by
    sending a shell command to the model as a prompt and then asking the user
    to approve running it. Observed on this machine: `/tools` at 11:37:20.875
    and `source .../activate` at 11:37:21.347, 0.47s apart, which is not typing.

    The cost is real and small: genuine type-ahead in the first moments is
    discarded too. That is the right trade — losing a keystroke somebody meant
    is a nuisance, and running a command they never typed is not.

    Returns 1 if anything was discarded, 0 if not, -1 where it cannot be done
    (Windows, or a stdin that is not a terminal). Best-effort by construction:
    failing to flush must never stop the CLI from starting.
    """
    try:
        import select
        import termios
    except ImportError:  # pragma: no cover - Windows
        return -1

    # `stream` is an argument only so a test can hand this a real pty. Under
    # pytest `sys.stdin` is a capture object with no terminal behind it, and a
    # flush that silently does nothing would let this regress unnoticed.
    stream = sys.stdin if stream is None else stream

    try:
        if not stream.isatty():
            return -1
        descriptor = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return -1

    try:
        # Asked before flushing purely so the caller can say it happened. A
        # session that silently eats what you typed is its own small mystery.
        ready, _, _ = select.select([descriptor], [], [], 0)
        termios.tcflush(descriptor, termios.TCIFLUSH)
        return 1 if ready else 0
    except (OSError, ValueError, termios.error):
        return -1


def install() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A redirected or closed stream is not a reason to refuse to start.
            pass
