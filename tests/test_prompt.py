"""The one-question-at-a-time chooser.

These drive a real pty, because everything interesting here is terminal
behaviour: whether a keystroke arrives before a newline does, whether escape
and an arrow key can be told apart, and whether the terminal is handed back in
the state it was borrowed in. A fake stream proves none of it.
"""

from __future__ import annotations

import os
import pty
import termios
import threading
import time

import pytest

from andromeda_cli import prompt

OPTIONS = [
    ("First", "the default", "one"),
    ("Second", "", "two"),
    ("Third", "", "three"),
]


class Terminal:
    """A pty with a file object on the far end, standing in for /dev/tty."""

    def __init__(self) -> None:
        self.controller, follower = pty.openpty()
        self.reader = os.fdopen(follower, "r", encoding="utf-8")

    def type(self, *keys: bytes, delay: float = 0.05) -> None:
        """Send keystrokes from another thread, as a person would."""

        def send():
            for key in keys:
                time.sleep(delay)
                os.write(self.controller, key)

        threading.Thread(target=send, daemon=True).start()

    def close(self) -> None:
        self.reader.close()
        os.close(self.controller)


@pytest.fixture
def terminal():
    term = Terminal()
    try:
        yield term
    finally:
        term.close()


def choose(terminal, *keys, default=0, skippable=True):
    terminal.type(*keys)
    return prompt._choose_by_key(
        terminal.reader, OPTIONS, default=default, skippable=skippable
    )


class TestChoosing:
    def test_enter_takes_the_default(self, terminal):
        assert choose(terminal, b"\r") == 0

    def test_an_arrow_moves_the_cursor(self, terminal):
        assert choose(terminal, b"\x1b[B", b"\r") == 1

    def test_it_wraps_at_the_top(self, terminal):
        """Three options and one press should not be a dead end at the edge."""
        assert choose(terminal, b"\x1b[A", b"\r") == 2

    def test_a_number_moves_the_cursor_without_committing(self, terminal):
        """A digit that submits makes a typo unrecoverable.

        It is also what lets the same `3` then enter mean the same thing here
        as it does on the numbered fallback.
        """
        assert choose(terminal, b"3", b"\r") == 2

    def test_a_number_can_be_corrected_before_enter(self, terminal):
        assert choose(terminal, b"3", b"1", b"\r") == 0

    def test_s_skips(self, terminal):
        assert choose(terminal, b"s") is None

    def test_escape_skips(self, terminal):
        """A bare escape is a person pressing escape; ESC [ B is an arrow.

        Nothing follows a real escape press, which is the only thing that
        tells them apart.
        """
        assert choose(terminal, b"\x1b") is None

    def test_an_interrupt_leaves_rather_than_choosing(self, terminal, monkeypatch):
        """Ctrl-C is delivered as a signal, not read as a byte.

        `ISIG` stays on (see below), so the terminal turns ctrl-C into SIGINT
        and it arrives as KeyboardInterrupt in the middle of the read. What
        matters is that it ends the question instead of being swallowed by the
        loop.
        """

        def interrupted(_reader):
            raise KeyboardInterrupt

        monkeypatch.setattr(prompt, "_read_key", interrupted)
        assert prompt._choose_by_key(
            terminal.reader, OPTIONS, default=0, skippable=True
        ) is None

    def test_an_unskippable_question_ignores_s(self, terminal):
        assert choose(terminal, b"s", b"\r", skippable=False) == 0

    def test_end_of_input_is_not_an_answer(self, terminal):
        """A closed terminal must never be read as agreeing to the default."""
        os.close(terminal.controller)
        result = prompt._choose_by_key(terminal.reader, OPTIONS, default=0, skippable=True)
        assert result is None
        # Already closed; stop the fixture closing it twice.
        terminal.controller = os.open(os.devnull, os.O_RDWR)


#: The two flags the chooser borrows. Asserted on by name rather than by
#: comparing whole termios structures: macOS sets ECHOKE on any tcsetattr
#: round trip whether you asked for it or not, so a byte-for-byte comparison
#: fails on a quirk of the platform instead of on anything this code did.
BORROWED = termios.ICANON | termios.ECHO


class TestTheTerminalIsGivenBack:
    def test_the_mode_is_restored(self, terminal):
        """A wizard that exits with ECHO off leaves an invisible shell behind.

        The person's next command types nothing on screen, and the only cure
        they know is closing the window.
        """
        fd = terminal.reader.fileno()
        before = termios.tcgetattr(fd)[3] & BORROWED
        assert before == BORROWED, "the pty did not start in the ordinary mode"
        choose(terminal, b"\r")
        assert termios.tcgetattr(fd)[3] & BORROWED == before

    def test_the_mode_is_restored_after_an_interrupt(self, terminal, monkeypatch):
        def interrupted(_reader):
            raise KeyboardInterrupt

        monkeypatch.setattr(prompt, "_read_key", interrupted)
        fd = terminal.reader.fileno()
        prompt._choose_by_key(terminal.reader, OPTIONS, default=0, skippable=True)
        assert termios.tcgetattr(fd)[3] & BORROWED == BORROWED

    def test_the_question_is_asked_with_echo_off(self, terminal, monkeypatch):
        """Otherwise every arrow key press prints `^[[B` across the list."""
        seen = {}

        def look(reader):
            seen["lflag"] = termios.tcgetattr(reader.fileno())[3]
            return prompt.ENTER, "\r"

        monkeypatch.setattr(prompt, "_read_key", look)
        prompt._choose_by_key(terminal.reader, OPTIONS, default=0, skippable=True)
        assert seen["lflag"] & BORROWED == 0

    def test_signals_still_reach_the_process(self, terminal):
        """ISIG stays on, so ctrl-C is an interrupt and not a lost byte.

        `tty.setraw` would have taken it off along with everything else, which
        is how a chooser becomes something you cannot leave.
        """
        with prompt._unbuffered(terminal.reader):
            attributes = termios.tcgetattr(terminal.reader.fileno())
        assert attributes[3] & termios.ISIG
        # OPOST too: without it every line drawn walks down and to the right.
        assert attributes[1] & termios.OPOST


class TestTheFallback:
    def test_a_pipe_gets_the_numbered_list(self):
        """`curl … | bash` has no terminal to point a cursor at."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"2\n")
        os.close(write_fd)
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            assert prompt.supports_keys(reader) is False
            assert prompt.choose(reader, OPTIONS, default=0) == 1

    def test_no_reader_at_all_takes_the_default(self):
        assert prompt.choose(None, OPTIONS, default=1) == 1
