"""Running commands on the user's own machine.

The single most dangerous thing this harness does, so the constraints are
stated rather than assumed:

  - It runs through a shell, because half of what makes a terminal tool useful
    is pipes and globs. That means the approval prompt must show the command
    *verbatim* — no summarising, no truncation of the part that matters.
  - It is `destructive` tier unconditionally. Not "destructive if it looks
    dangerous": classifying `rm` as risky and `make` as safe is a game the
    classifier loses, because `make` runs whatever the Makefile says.
  - It always has a timeout, and the timeout kills the process group. A shell
    that spawns a child and exits otherwise leaves the child holding the pipe
    and the harness waiting on it forever.

**The workspace boundary does not bind this tool.** `cwd` is confined, but the
command itself is a shell command: `cat ~/.ssh/id_rsa` runs. That is not an
oversight to be patched with a blocklist — a shell that cannot be escaped is
not a shell, and any list of forbidden commands loses to `$(printf ...)`. The
containment is the approval gate: `terminal` is `destructive`, so in the default
`ask` mode a person reads the verbatim command before it runs, and in a
non-interactive run it is not offered at all. Enabling `--approval auto` is
granting the model your shell, and it should read that way.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

from .spec import ToolResult
from .workspace import Workspace

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT = 30_000


def _truncate(stream: str, label: str) -> str:
    if len(stream) <= MAX_OUTPUT:
        return stream
    half = MAX_OUTPUT // 2
    dropped = len(stream) - MAX_OUTPUT
    # Keep both ends: the head has the command's intent, the tail has the error.
    return (
        f"{stream[:half]}\n\n… {dropped:,} characters of {label} omitted …\n\n{stream[-half:]}"
    )


def run_command(
    workspace: Workspace,
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    background: bool = False,
    processes: Any = None,
) -> ToolResult:
    command = (command or "").strip()
    if not command:
        return ToolResult(content="Error: no command given.", ok=False)

    if background:
        if processes is None:
            return ToolResult(
                content="Error: background commands are not available in this session.",
                ok=False,
            )
        try:
            process = processes.start(workspace, command, cwd)
        except Exception as exc:  # noqa: BLE001 - reported to the model
            return ToolResult(content=f"Error: could not start the command: {exc}", ok=False)
        return ToolResult(
            content=(
                f"Started {process.id} in the background. It keeps running while you "
                f"work. Use process(action='poll', session_id='{process.id}') for new "
                "output, and kill it when you are done with it."
            ),
            display=f"$ {command} &",
            metadata={"session_id": process.id, "background": True},
        )

    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))

    try:
        working_dir = workspace.resolve(cwd) if cwd else workspace.root
    except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
        return ToolResult(content=f"Error: {exc}", ok=False)

    try:
        process = subprocess.Popen(  # noqa: S602 - a shell is the point of this tool
            command,
            shell=True,
            cwd=str(working_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            # Its own process group, so the timeout can kill the whole tree
            # rather than just the shell that spawned it.
            start_new_session=True,
            env={**os.environ, "ANDROMEDA_CLI": "1"},
        )
    except OSError as exc:
        return ToolResult(content=f"Error: could not start the command: {exc}", ok=False)

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate()
        timed_out = True

    parts: list[str] = []
    if stdout.strip():
        parts.append(_truncate(stdout.rstrip(), "stdout"))
    if stderr.strip():
        parts.append(f"[stderr]\n{_truncate(stderr.rstrip(), 'stderr')}")

    if timed_out:
        parts.append(f"[timed out after {timeout}s and was killed]")
    elif process.returncode != 0:
        parts.append(f"[exit {process.returncode}]")

    body = "\n\n".join(parts) or "(no output)"
    # The outcome, not the command again. `display` is the one line a surface
    # prints under the call it has already printed — and both surfaces print
    # the *summary* as that call line, which for `terminal` is `$ <command>`
    # verbatim. Repeating it there means every shell call is drawn twice, and
    # the second copy carries no information at all.
    lines = len(body.splitlines())
    outcome = "timed out" if timed_out else f"exit {process.returncode}"
    # A non-zero exit is reported, never raised: the model has to be able to
    # read a failing test run and act on it.
    return ToolResult(
        content=body,
        display=f"{outcome} · {lines} line{'' if lines == 1 else 's'}",
        ok=process.returncode == 0 and not timed_out,
        metadata={"exit_code": process.returncode, "timed_out": timed_out},
    )


def _kill_tree(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or never had its own group. Fall back to the direct kill
        # so a timeout can never leave the harness blocked on communicate().
        try:
            process.kill()
        except OSError:
            pass


def arguments_summary(arguments: dict[str, Any]) -> str:
    # Verbatim, never abbreviated. This string IS the thing being consented to.
    return f"$ {arguments.get('command', '')}"
