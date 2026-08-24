"""Asking one question, on a terminal, without a form.

The wizard used to print a numbered list and read a line. That works
everywhere, which is why it was chosen, but it reads as a form: every option
on screen at once, an answer typed rather than pointed at, and — because each
screen scrolled onto the last — question two visible while you are still
answering question one. People answer forms without reading them.

So there are two readers here and the terminal decides which one it gets.

**Keys.** On a real terminal the cursor is a chevron on the selected row, up
and down move it, and enter commits. Nothing is typed, so nothing can be
mistyped, and the selected row is the only one that is emphasised — which is
the whole reason to point rather than number.

The terminal is put in *non-canonical* mode, not raw mode: `ICANON` and `ECHO`
come off so a keystroke arrives immediately and does not echo, and everything
else is left alone. Two consequences matter. `ISIG` stays on, so ctrl-C is
still an interrupt rather than a byte nobody handles. And `OPOST` stays on, so
a newline written to the screen still returns the carriage — a chooser drawn
in full raw mode stair-steps down and to the right, which looks like a bug in
the product before you have used it.

**A line.** No terminal, no termios, or a stdin that is a pipe: the numbered
list comes back, unchanged. That is the `curl … | bash` case, and a cursor UI
there would be unrecoverable — you cannot see what you are selecting.

Both read from a handle on `/dev/tty` where there is one, never `sys.stdin`.
Under a piped installer stdin carries the installer's own source, and reading
it answers the first question with a line of shell.
"""

from __future__ import annotations

import os
import select
import sys
from contextlib import contextmanager
from typing import Sequence

from rich.live import Live
from rich.text import Text

from .render import console

#: One option: what it is called, the line under it, and the value it sets.
Option = tuple[str, str, str]

#: The cursor. A chevron rather than a bullet or a highlight bar: it points at
#: one row without painting a block of colour across the screen, and it
#: survives a terminal with no colour at all.
CURSOR = "›"


def interactive_input():
    """A reader bound to the terminal, not to stdin.

    The installer runs as `curl … | bash`, which makes the script's stdin the
    *pipe carrying the script itself*. A wizard that reads stdin there gets
    either EOF immediately or, worse, the remaining bytes of its own source as
    answers. Re-opening `/dev/tty` reaches the actual terminal regardless of
    what stdin was redirected to, and it is the reason the wizard can be
    launched from inside a piped installer at all.

    Returns None when there is no controlling terminal — cron, CI, Docker
    without `-t` — and every caller treats that as "do not prompt" rather than
    blocking forever on a read that will never return.
    """
    if sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r", encoding="utf-8")  # noqa: SIM115 - lifetime is the session
    except OSError:
        return None


def supports_keys(reader) -> bool:
    """Whether this handle can be read a keystroke at a time."""
    if reader is None or not console.is_terminal:
        return False
    try:
        import termios  # noqa: F401
    except ImportError:  # pragma: no cover - Windows
        return False
    import termios

    try:
        if not reader.isatty():
            return False
        termios.tcgetattr(reader.fileno())
    except (OSError, ValueError, AttributeError, termios.error):
        return False
    return True


@contextmanager
def _unbuffered(reader):
    """Keystrokes as they are pressed, with the rest of the terminal intact.

    Only `ICANON` and `ECHO` are cleared. Clearing more — which is what
    `tty.setraw` does — takes `ISIG` and `OPOST` with it, and the cost of
    those two is a chooser you cannot ctrl-C out of, printing text that walks
    diagonally across the screen.
    """
    import termios

    fd = reader.fileno()
    try:
        saved = termios.tcgetattr(fd)
        altered = termios.tcgetattr(fd)
    except termios.error as exc:
        # Linux reports a pty whose controlling end disappeared as
        # termios.error(EIO), while macOS reports the same event as OSError.
        # Normalize it so the chooser's existing "terminal went away" path
        # returns no answer on both platforms.
        raise OSError("terminal is no longer available") from exc
    altered[3] &= ~(termios.ICANON | termios.ECHO)  # lflag
    altered[6][termios.VMIN] = 1
    altered[6][termios.VTIME] = 0
    try:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, altered)
        except termios.error as exc:
            raise OSError("terminal is no longer available") from exc
        yield
    finally:
        # TCSADRAIN, not TCSANOW: anything already written must reach the
        # screen before the mode changes under it.
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:
            # A dropped SSH session cannot have its terminal mode restored,
            # and must not turn a graceful no-answer into a traceback.
            pass


#: What a key press means. Escape sequences are matched before single bytes.
UP = "up"
DOWN = "down"
ENTER = "enter"
SKIP = "skip"
QUIT = "quit"


def _read_key(reader) -> tuple[str, str]:
    """One key, as (meaning, literal). Meaning is "" for an ordinary character.

    Read straight off the file descriptor rather than through the file object,
    and this is not a micro-optimisation — it is the difference between arrow
    keys working and not. An arrow key is three bytes, `ESC [ B`, and telling
    it apart from somebody pressing escape means asking whether anything
    followed the escape. A buffered reader has already pulled all three into
    Python's own buffer by the time the first character is returned, so the
    kernel has nothing left to report and every arrow key reads as a bare
    escape — which this chooser treats as "skip this question".

    `os.read` leaves the remaining bytes where `select` can still see them.
    """
    fd = reader.fileno()

    def read_one() -> str:
        try:
            raw = os.read(fd, 1)
        except OSError:
            # The terminal went away mid-question — a closed pty, a dropped
            # ssh session. Not an answer.
            return ""
        return raw.decode("utf-8", "replace") if raw else ""

    first = read_one()
    if not first:
        return QUIT, ""
    if first in ("\r", "\n"):
        return ENTER, first
    if first in ("\x03", "\x04"):  # ctrl-C, ctrl-D
        return QUIT, first
    if first == "\x1b":
        # After a real escape press nothing follows; after an arrow key two
        # more bytes are already waiting.
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return SKIP, first
        if read_one() != "[":
            return SKIP, first
        final = read_one()
        return {"A": UP, "B": DOWN}.get(final, ""), final
    lowered = first.lower()
    if lowered == "k":
        return UP, first
    if lowered == "j":
        return DOWN, first
    if lowered == "s":
        return SKIP, first
    return "", first


def _render(options: Sequence[Option], selected: int, hint: str) -> Text:
    out = Text()
    for index, (label, detail, _value) in enumerate(options):
        current = index == selected
        out.append(f"    {CURSOR if current else ' '}  ", style="accent" if current else "muted")
        out.append(f"{label}\n", style="bold" if current else "muted")
        if detail:
            out.append(f"       {detail}\n", style="muted")
    out.append(f"\n  {hint}", style="muted")
    return out


def choose(
    reader,
    options: Sequence[Option],
    *,
    default: int = 0,
    skippable: bool = True,
) -> int | None:
    """Ask one question. Returns the chosen index, or None for skipped.

    None also covers ctrl-C and end-of-input: a wizard is not a place to
    refuse to take no for an answer, and every caller treats None as "leave
    the current value alone and carry on".
    """
    if reader is None:
        return default
    if supports_keys(reader):
        return _choose_by_key(reader, options, default=default, skippable=skippable)
    return _choose_by_number(reader, options, default=default, skippable=skippable)


def _choose_by_key(
    reader, options: Sequence[Option], *, default: int, skippable: bool
) -> int | None:
    selected = max(0, min(default, len(options) - 1))
    hint = "↑↓ to move · enter to choose" + ("  ·  s to skip" if skippable else "")

    with Live(
        _render(options, selected, hint),
        console=console,
        auto_refresh=False,
        transient=False,
    ) as live:
        try:
            with _unbuffered(reader):
                while True:
                    meaning, literal = _read_key(reader)
                    if meaning == ENTER:
                        break
                    if meaning == QUIT:
                        return None
                    if meaning == SKIP and skippable:
                        return None
                    if meaning == UP:
                        selected = (selected - 1) % len(options)
                    elif meaning == DOWN:
                        selected = (selected + 1) % len(options)
                    elif literal.isdigit() and 1 <= int(literal) <= len(options):
                        # A number moves the cursor; it does not commit. Two
                        # keystrokes for one answer is the point — a digit that
                        # submits makes a typo unrecoverable, and it is also
                        # what lets a queued "1\n" mean the same thing here as
                        # it did on the numbered reader.
                        selected = int(literal) - 1
                    else:
                        continue
                    live.update(_render(options, selected, hint), refresh=True)
        except (OSError, ValueError, KeyboardInterrupt):
            return None
        finally:
            live.update(_render(options, selected, hint), refresh=True)
    return selected


def _choose_by_number(
    reader, options: Sequence[Option], *, default: int, skippable: bool
) -> int | None:
    """The fallback: a numbered list and one typed line.

    Numbers work on every terminal, over ssh, and in a transcript somebody
    pastes into a bug report.
    """
    for index, (label, detail, _value) in enumerate(options, start=1):
        marker = CURSOR if index - 1 == default else " "
        console.print(f"    [accent]{marker}[/accent]  [bold]{index}[/bold]  {label}")
        if detail:
            console.print(f"           [muted]{detail}[/muted]")
    console.print()
    tail = " · s to skip" if skippable else ""
    console.print(f"  [muted]number to choose · enter for the default{tail}[/muted]")
    console.print()

    try:
        # Read from `reader`, never `input()`. `input()` reads `sys.stdin`, and
        # under `curl … | bash` that is the pipe carrying the installer script —
        # so the answer to the first question would be the next line of shell
        # source. The whole reason `interactive_input()` re-opens /dev/tty is to
        # get a handle on the actual terminal; calling `input()` throws that
        # away and silently reintroduces the bug it exists to prevent.
        console.file.write("  › ")
        console.file.flush()
        line = reader.readline()
        if not line:  # EOF
            return None
        raw = line.strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        return None
    if skippable and raw in {"s", "skip"}:
        return None
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    return default


def clear(reader) -> None:
    """Take the previous question off the screen.

    One question at a time is the whole reason: a wizard that scrolls leaves
    step one visible while you answer step two, and the person cannot tell
    which of the two things on screen is asking them something.

    Never on a pipe — clearing a redirected stream writes escape codes into
    whatever is capturing it.
    """
    if reader is None or not console.is_terminal:
        return
    console.clear()


def wait_for_enter(reader) -> None:
    """Hold a screen that has nothing to decide until the person is done with it.

    Without this, a screen with no question on it is cleared by the next step a
    fraction of a second after it is drawn, and nobody reads the one line it
    existed to show them.
    """
    if reader is None:
        return
    console.print("  [muted]enter to continue[/muted]")
    if not supports_keys(reader):
        try:
            reader.readline()
        except (EOFError, OSError):
            pass
        return
    try:
        with _unbuffered(reader):
            while True:
                meaning, _ = _read_key(reader)
                if meaning in (ENTER, QUIT, SKIP):
                    return
    except (OSError, ValueError, KeyboardInterrupt):
        return
