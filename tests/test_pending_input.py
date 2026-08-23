"""Input that arrived before the prompt was not addressed to the prompt.

Editors and shell integrations type into new terminals. VS Code's Python
extension writes `source .../.venv/bin/activate` into any terminal where it
detects a venv; direnv and conda hooks do similar things; a multiplexer replays
its buffer on attach. If that lands while the REPL is starting, prompt_toolkit
reads it as a line and submits it — so the session opens by sending a shell
command to the model as a prompt, and then asks the user to approve running the
thing they never typed.

Observed on a real machine, in the history file: `/tools` at 11:37:20.875 and
`source .../activate` at 11:37:21.347. Nobody types that in 0.47 seconds.

Driven through a real pseudo-terminal because that is the only place the bug
exists: it is entirely about what is sitting in a tty's input buffer, and a
fake stdin has no buffer to sit in.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from andromeda_cli import bootstrap
from andromeda_cli.repl import (
    GUARD_SECONDS,
    MIN_GUARDED_LENGTH,
    MIN_TYPING_SECONDS,
    looks_injected,
)

def _answer_cursor_report(descriptor: int, chunk: bytes) -> None:
    """Reply to `ESC[6n` the way a real terminal does.

    `prompt_toolkit` asks the terminal where the cursor is and waits for the
    answer. A bare pty never answers, so the client blocks for about a second
    on every prompt — and a test that measures typing speed would then be
    measuring its own harness. This cost an hour to find; it is here so nobody
    pays it twice.
    """
    if b"\x1b[6n" in chunk:
        try:
            os.write(descriptor, b"\x1b[10;1R")
        except OSError:
            pass


# Either layer refusing the line is a pass. The startup flush and the timing
# guard cover different arrival windows and both are correct answers.
REFUSALS = (b"ignored input that arrived", b"too fast to have been typed")


def _refused(output: bytes) -> bool:
    return any(marker in output for marker in REFUSALS)


REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / ".venv" / "bin" / "andromeda"
INJECTED = b" source /somewhere/.venv/bin/activate\r"


class TestDrainDirectly:
    def test_a_pipe_is_left_alone(self):
        """Nothing to drain, and nothing that could be typed at it."""
        assert bootstrap.drain_pending_input() == -1

    def test_it_reports_when_it_discarded_something(self):
        """So the surface can say so. A session that silently eats what you
        typed is its own small mystery."""
        primary, secondary = pty.openpty()
        try:
            with open(secondary, "rb", buffering=0, closefd=False) as stream:
                os.write(primary, b"typed ahead\r")
                time.sleep(0.1)
                assert bootstrap.drain_pending_input(stream) == 1
                # And the buffer really is empty afterwards.
                ready, _, _ = select.select([secondary], [], [], 0.1)
                assert not ready
        finally:
            os.close(primary)
            os.close(secondary)

    def test_a_quiet_terminal_reports_nothing_discarded(self):
        primary, secondary = pty.openpty()
        try:
            with open(secondary, "rb", buffering=0, closefd=False) as stream:
                assert bootstrap.drain_pending_input(stream) == 0
        finally:
            os.close(primary)
            os.close(secondary)


@pytest.mark.skipif(not BINARY.exists(), reason="needs the installed entry point")
@pytest.mark.skipif(sys.platform == "win32", reason="no pty on Windows")
def test_injected_input_never_becomes_a_prompt(tmp_path):
    """The whole bug, end to end, against the real binary on a real pty."""
    environment = dict(
        os.environ,
        ANDROMEDA_HOME=str(tmp_path / "home"),
        # The BYOK lane with a fake key: `build_provider` only needs the
        # variable to be set, and this test never completes a turn, so no
        # request is ever made. The alternative — the default relay lane —
        # exits on "not paired" before the REPL starts, which would make this
        # pass for the wrong reason.
        ANDROMEDA_PROVIDER="direct",
        OPENROUTER_API_KEY="test-key-never-used",
        # `deny` so that even a total failure of this fix cannot run a command:
        # a test that guards against executing an injected shell line must not
        # be the thing that executes it.
        ANDROMEDA_APPROVAL_MODE="deny",
        TERM="xterm-256color",
        COLUMNS="100",
        LINES="30",
    )

    # `openpty` + `subprocess`, not `pty.fork()`. The suite runs threads (lanes,
    # background processes), and `forkpty()` in a multi-threaded process can
    # deadlock the child between fork and exec — Python warns about exactly
    # this. `subprocess` does the fork-and-exec safely.
    primary, secondary = pty.openpty()
    child = subprocess.Popen(
        [str(BINARY)],
        stdin=secondary,
        stdout=secondary,
        stderr=secondary,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    os.close(secondary)

    try:
        # Exactly what an editor's venv auto-activation does: type into the
        # terminal a fraction of a second in, before anything has prompted.
        time.sleep(0.35)
        os.write(primary, INJECTED)

        output = b""
        deadline = time.time() + 20
        while time.time() < deadline:
            ready, _, _ = select.select([primary], [], [], 0.3)
            if not ready:
                continue
            try:
                chunk = os.read(primary, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            _answer_cursor_report(primary, chunk)
            if _refused(output):
                break
    finally:
        child.kill()
        child.wait(timeout=10)
        os.close(primary)

    text = output.decode("utf-8", "replace")
    # *Which* layer caught it is a race and not the point: injecting at a fixed
    # delay lands either side of the startup flush depending on how fast the
    # machine got there. Both refusals are correct, so the assertion is on the
    # outcome.
    assert _refused(output), text[-1500:]
    # The thing that must not have happened. Deliberately not asserted on the
    # raw string: a tty echoes what is written to it, so the injected text
    # appears in this capture whether or not the REPL ever read it. The gate
    # is the honest signal — reaching it means the line became a prompt, went
    # to the model, and came back as a tool call.
    assert "⚠ terminal" not in text



class TestTheTimingGuard:
    """The second layer: input that arrives *after* the prompt is drawn.

    The startup flush cannot help there — by then `prompt_toolkit` is reading,
    and an injected line is indistinguishable from a typed one except by how
    fast it arrived. Observed on a real machine: the banner printed, the prompt
    appeared, and the activate line landed about a second later.
    """

    ACTIVATE = " source /Users/zekevoigt/Desktop/harmonized/cli/.venv/bin/activate"

    def test_the_real_injected_line_is_caught(self):
        assert looks_injected(self.ACTIVATE, 0.02, 1.0)

    def test_something_actually_typed_is_not(self):
        assert not looks_injected("summarise this repository for me", 3.5, 1.0)

    def test_the_guard_disarms_after_a_few_seconds(self):
        """A paste later in a session is never touched — that is the whole
        reason this is armed for a window rather than always."""
        assert not looks_injected(self.ACTIVATE, 0.01, GUARD_SECONDS + 0.1)

    def test_short_answers_are_never_guarded(self):
        """`y`, `n` and `/help` are plausible at any speed and trivial to
        retype; eating one would be worse than the bug."""
        for text in ("y", "n", "/help", "/tools", "!"):
            assert not looks_injected(text, 0.001, 0.1), text

    def test_the_length_floor_is_where_it_says(self):
        just_under = "x" * (MIN_GUARDED_LENGTH - 1)
        just_over = "x" * (MIN_GUARDED_LENGTH + 1)
        assert not looks_injected(just_under, 0.001, 0.1)
        assert looks_injected(just_over, 0.001, 0.1)

    def test_a_slow_typist_is_safe_at_the_boundary(self):
        assert not looks_injected(self.ACTIVATE, MIN_TYPING_SECONDS + 0.01, 0.1)


@pytest.mark.skipif(not BINARY.exists(), reason="needs the installed entry point")
@pytest.mark.skipif(sys.platform == "win32", reason="no pty on Windows")
def test_injection_after_the_prompt_is_drawn_is_also_caught(tmp_path):
    """The case the startup flush cannot reach.

    Reported from a real session: the banner printed, the prompt appeared, and
    the editor's activate line landed about a second later. By then
    `prompt_toolkit` is reading, so there is no buffer left to flush — only the
    timing tells them apart.
    """
    environment = dict(
        os.environ,
        ANDROMEDA_HOME=str(tmp_path / "home"),
        ANDROMEDA_PROVIDER="direct",
        OPENROUTER_API_KEY="test-key-never-used",
        ANDROMEDA_APPROVAL_MODE="deny",
        TERM="xterm-256color",
        COLUMNS="120",
        LINES="30",
    )

    primary, secondary = pty.openpty()
    child = subprocess.Popen(
        [str(BINARY)],
        stdin=secondary,
        stdout=secondary,
        stderr=secondary,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    os.close(secondary)

    output = b""
    injected = False
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            ready, _, _ = select.select([primary], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(primary, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                _answer_cursor_report(primary, chunk)
            if not injected and b"approval:" in output:
                # The prompt is up and waiting — which is exactly when the
                # editor writes into the terminal.
                time.sleep(0.5)
                os.write(primary, INJECTED)
                injected = True
            if b"too fast to have been typed" in output or b"\xe2\x9a\xa0 terminal" in output:
                break
    finally:
        child.kill()
        child.wait(timeout=10)
        os.close(primary)

    text = output.decode("utf-8", "replace")
    assert "too fast to have been typed" in text, text[-1500:]
    assert "⚠ terminal" not in text
