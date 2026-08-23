"""Long-running commands that outlive the tool call that started them.

`terminal` blocks and kills its process tree on timeout, which is correct for
`wc -l` and useless for `npm run dev`. A whole class of work — start the server,
then test against it — needs a process that keeps running while the agent does
something else.

The surface: `terminal(background=true)` returns a session id, and one
`process` tool with an action enum operates on it.
One tool rather than eight because eight near-identical schemas is eight chances
for the model to pick the wrong one.

Output is drained by reader threads from the moment the process starts. Reading
lazily on `poll` looks simpler and deadlocks: a child that fills the 64KB pipe
buffer blocks on write forever while nobody is reading.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .spec import ToolResult, failure
from .workspace import Workspace

# Ring buffer per stream. A dev server left running overnight produces
# unbounded output; keeping the last N lines keeps memory flat and loses only
# what nobody was going to read.
MAX_LINES = 5_000
DEFAULT_LOG_LINES = 200
DEFAULT_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 600
ID_PREFIX = "proc_"


@dataclass
class Process:
    id: str
    command: str
    cwd: str
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    exit_code: int | None = None
    popen: Any = field(default=None, repr=False)
    _lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES), repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # How much of the buffer `poll` has already handed back, so each poll
    # returns only what is new. Counted in lines consumed rather than an index,
    # because the ring buffer discards from the front.
    _read_cursor: int = 0
    _total_lines: int = 0

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._total_lines += 1

    def snapshot(self, offset: int | None = None, limit: int | None = None) -> list[str]:
        with self._lock:
            lines = list(self._lines)
        if offset is not None:
            # Offsets are against the whole run, not the buffer, so a caller
            # paging through a log is not silently renumbered when the ring
            # discards old lines.
            dropped = self._total_lines - len(lines)
            lines = lines[max(0, offset - dropped) :]
        if limit is not None:
            lines = lines[:limit]
        return lines

    def drain_new(self) -> list[str]:
        with self._lock:
            lines = list(self._lines)
            dropped = self._total_lines - len(lines)
            start = max(0, self._read_cursor - dropped)
            new = lines[start:]
            self._read_cursor = self._total_lines
        return new

    def status(self) -> str:
        if self.running:
            return f"running for {int(self.elapsed)}s"
        return f"exited {self.exit_code} after {int(self.elapsed)}s"

    def summary(self) -> str:
        return f"{self.id}  {self.status():<28} {self.command[:60]}"


class ProcessRegistry:
    def __init__(self) -> None:
        self._processes: dict[str, Process] = {}
        self._lock = threading.Lock()

    # ---- starting ---------------------------------------------------------

    def start(self, workspace: Workspace, command: str, cwd: str | None = None) -> Process:
        working_dir = workspace.resolve(cwd) if cwd else workspace.root

        popen = subprocess.Popen(  # noqa: S602 - a shell is the point of this tool
            command,
            shell=True,
            cwd=str(working_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            # Its own group, so killing it takes the whole tree rather than the
            # shell that spawned it.
            start_new_session=True,
            env={**os.environ, "ANDROMEDA_CLI": "1"},
        )

        process = Process(
            id=f"{ID_PREFIX}{uuid.uuid4().hex[:8]}",
            command=command,
            cwd=str(working_dir),
            popen=popen,
        )
        with self._lock:
            self._processes[process.id] = process

        threading.Thread(target=self._pump, args=(process,), daemon=True).start()
        return process

    def _pump(self, process: Process) -> None:
        """Drain stdout continuously.

        stderr is merged into stdout at launch rather than read on a second
        thread: interleaving is what a person sees in a terminal, and two
        buffers means a traceback arrives detached from the line that caused it.
        """
        stream = process.popen.stdout
        if stream is not None:
            try:
                for line in stream:
                    process.append(line.rstrip("\n"))
            except (ValueError, OSError):
                pass
        process.popen.wait()
        process.exit_code = process.popen.returncode
        process.finished_at = time.time()

    # ---- resolving --------------------------------------------------------

    def resolve(self, session_id: str) -> Process | None:
        """Accept a full id or any unambiguous prefix, with or without `proc_`."""
        wanted = (session_id or "").strip()
        if not wanted:
            return None
        with self._lock:
            processes = dict(self._processes)

        if wanted in processes:
            return processes[wanted]

        bare = wanted[len(ID_PREFIX) :] if wanted.startswith(ID_PREFIX) else wanted
        matches = [
            process
            for key, process in processes.items()
            if key.startswith(wanted) or key[len(ID_PREFIX) :].startswith(bare)
        ]
        return matches[0] if len(matches) == 1 else None

    def all(self) -> list[Process]:
        with self._lock:
            processes = list(self._processes.values())
        processes.sort(key=lambda item: item.started_at)
        return processes

    @property
    def running(self) -> list[Process]:
        return [process for process in self.all() if process.running]

    # ---- shutdown ---------------------------------------------------------

    def kill(self, process: Process) -> bool:
        if not process.running:
            return False
        try:
            os.killpg(os.getpgid(process.popen.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.popen.kill()
            except OSError:
                return False
        # SIGTERM first, then insist. A process ignoring TERM is common enough
        # (anything with its own signal handler) that leaving it is not an
        # option — the session would exit with an orphan still holding a port.
        try:
            process.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.popen.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return True

    def shutdown_all(self) -> int:
        killed = 0
        for process in self.running:
            if self.kill(process):
                killed += 1
        return killed


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _missing(session_id: str) -> ToolResult:
    return failure(
        f"No process matching {session_id!r}. Call process(action='list') to see them."
    )


def _render_lines(lines: list[str], empty: str) -> str:
    return "\n".join(lines) if lines else empty


def act(
    registry: ProcessRegistry,
    action: str,
    session_id: str = "",
    data: str = "",
    timeout: int = DEFAULT_WAIT_SECONDS,
    offset: int | None = None,
    limit: int | None = None,
) -> ToolResult:
    action = (action or "").strip().lower()

    if action == "list":
        processes = registry.all()
        if not processes:
            return ToolResult(content="No background processes.", display="none")
        return ToolResult(
            content="\n".join(process.summary() for process in processes),
            display=f"{len(processes)} process(es)",
        )

    process = registry.resolve(session_id)
    if process is None:
        return _missing(session_id)

    if action == "poll":
        new = process.drain_new()
        body = _render_lines(new, "(no new output)")
        return ToolResult(
            content=f"{process.status()}\n\n{body}",
            display=f"{process.id}: {process.status()}",
            metadata={"running": process.running, "exit_code": process.exit_code},
        )

    if action == "log":
        window = process.snapshot(
            offset=offset,
            limit=limit if limit is not None else (None if offset is not None else DEFAULT_LOG_LINES),
        )
        if offset is None and limit is None:
            window = window[-DEFAULT_LOG_LINES:]
        return ToolResult(
            content=f"{process.status()}\n\n{_render_lines(window, '(no output)')}",
            display=f"{process.id}: {len(window)} lines",
        )

    if action == "wait":
        seconds = max(1, min(int(timeout or DEFAULT_WAIT_SECONDS), MAX_WAIT_SECONDS))
        deadline = time.time() + seconds
        while process.running and time.time() < deadline:
            time.sleep(0.1)
        new = process.drain_new()
        note = "" if not process.running else f"\n\n[still running after {seconds}s]"
        return ToolResult(
            content=f"{process.status()}\n\n{_render_lines(new, '(no new output)')}{note}",
            display=f"{process.id}: {process.status()}",
            ok=not process.running,
            metadata={"running": process.running, "exit_code": process.exit_code},
        )

    if action == "kill":
        if registry.kill(process):
            return ToolResult(content=f"Killed {process.id}.", display=f"killed {process.id}")
        return ToolResult(
            content=f"{process.id} had already exited ({process.exit_code}).",
            display="already exited",
        )

    if action in {"write", "submit"}:
        if not process.running:
            return failure(f"{process.id} has exited; there is nothing to write to.")
        stdin = process.popen.stdin
        if stdin is None:
            return failure(f"{process.id} has no stdin.")
        try:
            stdin.write(data + ("\n" if action == "submit" else ""))
            stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            return failure(f"Could not write to {process.id}: {exc}")
        return ToolResult(
            content=f"Sent {len(data)} characters to {process.id}.",
            display=f"wrote to {process.id}",
        )

    if action == "close":
        stdin = process.popen.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        return ToolResult(content=f"Closed stdin on {process.id}.", display="closed stdin")

    return failure(
        f"Unknown action {action!r}. "
        "Use list, poll, log, wait, kill, write, submit or close."
    )
