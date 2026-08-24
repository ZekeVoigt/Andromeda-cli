"""First-run setup, the standing-instructions file, and the startup study.

The pty tests here are the ones that matter. Setup's whole reason for opening
`/dev/tty` is a case that cannot be reproduced by calling a function: the
installer runs as `curl … | bash`, so the process's stdin is the pipe carrying
the shell script, and anything reading stdin gets shell source as its answer.
That is only visible by actually running the binary with a piped stdin and a
separate terminal, which is what these do.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from andromeda_agent import soul
from andromeda_cli import art
from andromeda_cli.commands import setup as setup_cmd

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CLI_ROOT = Path(__file__).resolve().parents[1]


def strip(text: str) -> str:
    return ANSI.sub("", text)


# ---------------------------------------------------------------------------
# SOUL.md
# ---------------------------------------------------------------------------


class TestSoul:
    def test_it_is_created_with_a_template(self, tmp_path):
        assert soul.scaffold(tmp_path) is True
        assert soul.path(tmp_path).is_file()

    def test_it_is_never_overwritten(self, tmp_path):
        """The one file the program must not touch after creating it.

        Its entire value is that it says what the person left in it. A re-run
        of the installer replacing it would be silent data loss of the only
        hand-authored thing in the config directory.
        """
        soul.scaffold(tmp_path)
        soul.path(tmp_path).write_text("# SOUL\n\nAlways run the tests.\n", encoding="utf-8")

        assert soul.scaffold(tmp_path) is False
        assert "Always run the tests." in soul.path(tmp_path).read_text(encoding="utf-8")

    def test_an_untouched_template_costs_nothing(self, tmp_path):
        """An unedited template must not reach the prompt.

        It is prepended to every request forever, so shipping boilerplate that
        nobody wrote is a bill every user pays for nothing.
        """
        soul.scaffold(tmp_path)
        assert soul.load(tmp_path) == ""
        assert soul.block(tmp_path) == ""

    def test_real_content_reaches_the_prompt(self, tmp_path):
        soul.path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        soul.path(tmp_path).write_text("- British English, always.\n", encoding="utf-8")

        assert "British English" in soul.load(tmp_path)
        assert "British English" in soul.block(tmp_path)

    def test_the_templates_own_hints_are_stripped(self, tmp_path):
        """The template's `<!-- … -->` prompts are addressed to the user.

        A model reading an instruction meant for the person is confusing, and
        paying for it every turn is worse.
        """
        soul.path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        soul.path(tmp_path).write_text(
            "- Be brief.\n<!-- fill this in with your stack -->\n", encoding="utf-8"
        )
        loaded = soul.load(tmp_path)
        assert "Be brief" in loaded
        assert "fill this in" not in loaded

    def test_it_is_capped(self, tmp_path):
        soul.path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        soul.path(tmp_path).write_text("- padding line\n" * 2000, encoding="utf-8")

        loaded = soul.load(tmp_path)
        assert len(loaded) <= soul.MAX_CHARS + 100
        assert "truncated" in loaded

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert soul.load(tmp_path / "nope") == ""

    def test_it_says_it_grants_nothing(self, tmp_path):
        """The block must frame itself as instructions, never as authority.

        This file is the most attractive prompt-injection target in the
        product: it is prose, it is loaded every turn, and a user could paste
        anything into it. It shapes *how* work is done and can never widen
        *what may be done* — the gate is the authority, not this.
        """
        soul.path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        soul.path(tmp_path).write_text("- Be brief.\n", encoding="utf-8")

        block = soul.block(tmp_path)
        assert "never grant permissions" in block
        assert "never widen" in block


# ---------------------------------------------------------------------------
# The startup study
# ---------------------------------------------------------------------------


class TestTheStudy:
    def test_the_figure_ships_with_the_package(self):
        assert art.figure(), "the pre-rendered art is missing from the package"

    def test_the_composition_carries_the_sky_coordinates(self):
        rows = art.study(72)
        text = "\n".join(row for row, _ in rows)
        # M31's actual position, the same pair the landing page prints.
        assert "00h 42m 44s" in text
        assert "+41" in text
        assert "HUMAN / SYSTEM / ORBIT" in text

    def test_a_pipe_gets_no_art(self):
        """Redirected output must be text, not a picture.

        `andromeda "…" > notes.md` should produce markdown. Braille in a file
        is corruption, not decoration.
        """

        class NotATty:
            encoding = "utf-8"

            def isatty(self):
                return False

        assert art.supported(NotATty()) is False

    def test_a_terminal_that_cannot_encode_braille_gets_no_art(self, monkeypatch):
        class Ascii:
            encoding = "ascii"

            def isatty(self):
                return True

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("ANDROMEDA_NO_ART", raising=False)
        assert art.supported(Ascii()) is False

    def test_it_can_be_turned_off(self, monkeypatch):
        class Tty:
            encoding = "utf-8"

            def isatty(self):
                return True

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("ANDROMEDA_NO_ART", "1")
        assert art.supported(Tty()) is False

    def test_missing_art_degrades_rather_than_crashes(self, monkeypatch, tmp_path):
        """A decoration must never be why a session fails to start."""
        monkeypatch.setattr(art, "FIGURE_PATH", tmp_path / "absent.txt")
        assert art.figure() == []
        assert art.study(72) == []


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


class TestTheCapabilityReport:
    def test_every_gap_names_its_own_fix(self):
        """A gap that says only "not configured" leaves the person to search.

        The command that closes it is the entire value of the line.
        """
        for label, state, fix in setup_cmd.capability_report({"provider": "relay"}):
            if not state:
                assert fix, f"{label} reports a gap with no way to close it"

    def test_the_fix_matches_the_lane(self):
        paired = setup_cmd.capability_report({"provider": "relay"})[0]
        byok = setup_cmd.capability_report({"provider": "direct"})[0]
        assert "auth login" in paired[2]
        assert "OPENROUTER_API_KEY" in byok[2]


class TestWithoutATerminal:
    def test_it_declines_rather_than_hanging(self, monkeypatch, capsys):
        """CI, cron and `docker run` without `-t` have no terminal.

        Blocking on a read that will never return is the worst outcome — it
        looks like a hang, and it happens inside an installer.
        """
        monkeypatch.setattr(setup_cmd, "interactive_input", lambda: None)
        assert setup_cmd.run() == 0
        assert "andromeda setup" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The pty tests — the reason this file exists
# ---------------------------------------------------------------------------


def drive(argv: list[str], *, home: Path, keystrokes: list[bytes],
          stdin_pipe: bool = False, timeout: float = 20.0) -> str:
    """Run the real binary against a real terminal and return what it drew.

    `openpty` plus `subprocess` rather than `pty.fork()`: this suite runs
    threads, and forking a multi-threaded process can deadlock the child
    between fork and exec.
    """
    import fcntl
    import pty
    import termios

    def take_controlling_tty():
        """Make the pty this child's controlling terminal.

        Required for the piped-stdin case and easy to miss: `/dev/tty` resolves
        to a process's *controlling* terminal, which it does not have merely by
        writing to a pty. Without `setsid` + `TIOCSCTTY` the child inherits the
        test runner's terminal, or none at all under pytest, and the run proves
        nothing about the case it was written for.
        """
        os.setsid()
        fcntl.ioctl(follower, termios.TIOCSCTTY, 0)

    env = dict(
        os.environ,
        ANDROMEDA_HOME=str(home),
        COLUMNS="76",
        LINES="40",
        TERM="xterm-256color",
        ANDROMEDA_NO_ART="1",  # the study is tested separately; keep output readable
    )
    controller, follower = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "andromeda_cli", *argv],
        stdin=subprocess.PIPE if stdin_pipe else follower,
        stdout=follower,
        stderr=follower,
        env=env,
        cwd=CLI_ROOT,
        close_fds=True,
        preexec_fn=take_controlling_tty,
    )
    os.close(follower)

    if stdin_pipe and process.stdin:
        # Exactly the hazard: under `curl | bash` the process's stdin carries
        # shell source. If setup reads stdin, this line becomes its answer.
        process.stdin.write(b"echo THIS LINE IS SHELL SOURCE\n")
        process.stdin.flush()

    collected = bytearray()
    os.set_blocking(controller, False)
    deadline = time.time() + timeout
    pending = list(keystrokes)
    next_key = time.time() + 1.0

    while time.time() < deadline:
        try:
            chunk = os.read(controller, 65536)
            if chunk:
                collected.extend(chunk)
        except (BlockingIOError, OSError):
            pass
        if pending and time.time() >= next_key:
            try:
                os.write(controller, pending.pop(0))
            except OSError:
                # The child closed the pty first. Not a failure on its own —
                # what the run drew is still what gets asserted on.
                pending.clear()
            next_key = time.time() + 0.7
        if process.poll() is not None and not pending:
            # Drain whatever is still buffered in the pty.
            time.sleep(0.3)
            try:
                collected.extend(os.read(controller, 65536))
            except (BlockingIOError, OSError):
                pass
            break
        time.sleep(0.05)

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    os.close(controller)
    return strip(collected.decode("utf-8", "replace"))


class TestTheWizardOnARealTerminal:
    def test_it_walks_all_four_steps(self, tmp_path):
        home = tmp_path / "home"
        out = drive(["setup"], home=home, keystrokes=[b"1\n", b"1\n"])

        assert "1 of 4" in out
        assert "2 of 4" in out
        assert "4 of 4" in out
        assert soul.path(home).is_file(), "setup did not scaffold SOUL.md"

    def test_it_reads_the_terminal_and_not_a_piped_stdin(self, tmp_path):
        """The bug this whole design exists to prevent.

        With stdin a pipe — which is what `curl … | bash` produces — a wizard
        calling `input()` reads the installer's own source as the user's
        answer. Here stdin carries a line of shell and the *terminal* carries
        the real keystroke; the shell line must never be treated as input.
        """
        home = tmp_path / "home"
        out = drive(["setup"], home=home, keystrokes=[b"2\n", b"1\n"], stdin_pipe=True)

        assert "THIS LINE IS SHELL SOURCE" not in out.replace("\r", "")
        assert "1 of 4" in out
        assert soul.path(home).is_file()
