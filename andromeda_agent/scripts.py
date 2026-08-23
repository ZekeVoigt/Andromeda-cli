"""Scripts a scheduled job is allowed to run.

A job spec is *data*. It lives in a JSON file, it can be written by the agent
itself, and it is executed later with nobody watching. A path in that data that
can point anywhere is arbitrary code execution on a timer, so a script is
addressed by name inside one directory and nowhere else:

    ~/.andromeda-cli/scripts/<name>

Absolute paths are refused, `..` is refused after resolution, and
a symlink pointing out of the directory is refused — the check is done on the
resolved path, because a link is exactly how a "contained" path stops being
contained.

**The interpreter is chosen by extension, and an unknown extension is refused
rather than guessed.** The tempting default — send anything that is not
`.sh`/`.bash` to Python — quietly feeds a `.rb` to the wrong interpreter and
reports a syntax error the author has to decode. Two known kinds, and a clear "no" for
the rest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Generous, because a data-collection script may legitimately talk to a slow
# API — and bounded, because it runs on every tick and a hung one would wedge
# the whole scheduler rather than one job.
DEFAULT_TIMEOUT = 120
MAX_OUTPUT = 100_000

SHELL_SUFFIXES = frozenset({".sh", ".bash"})
PYTHON_SUFFIXES = frozenset({".py"})


class ScriptError(ValueError):
    pass


def scripts_dir(home: Path) -> Path:
    return Path(home) / "scripts"


def resolve(home: Path, name: str) -> Path:
    """The one path a job may name, or an error explaining why it may not."""
    raw = (name or "").strip()
    if not raw:
        raise ScriptError("A script name is required.")

    candidate = Path(raw)
    if candidate.is_absolute():
        raise ScriptError(
            f"{raw!r} is an absolute path. Scripts are named relative to "
            f"{scripts_dir(home)} — put it there and pass just the name."
        )

    root = scripts_dir(home).resolve()
    # `strict=False`: the file may legitimately not exist yet, and "no such
    # script" is a better message than a resolution error.
    target = (root / candidate).resolve(strict=False)
    if root != target and root not in target.parents:
        raise ScriptError(f"{raw!r} resolves outside {root}.")
    if not target.exists():
        raise ScriptError(f"No script {raw!r} in {root}.")
    if not target.is_file():
        raise ScriptError(f"{target} is not a file.")

    suffix = target.suffix.lower()
    if suffix not in SHELL_SUFFIXES and suffix not in PYTHON_SUFFIXES:
        raise ScriptError(
            f"{target.name} has an unrecognised extension. Use .sh, .bash or .py "
            "— guessing an interpreter is how a script fails with somebody "
            "else's syntax error."
        )
    return target


def command_for(path: Path) -> list[str]:
    """How to run it.

    `sys.executable`, not a bare `python`: the CLI is reached through a symlink
    and PATH's first `python` is frequently not the interpreter this install
    runs on — the same trap `andromeda browser install` documents.
    """
    if path.suffix.lower() in SHELL_SUFFIXES:
        bash = shutil.which("bash")
        if bash is None:
            raise ScriptError("bash is not installed, so a .sh script cannot run.")
        return [bash, str(path)]
    return [sys.executable, str(path)]


@dataclass
class ScriptResult:
    ok: bool
    output: str
    error: str = ""
    exit_code: int = 0


def run(
    home: Path,
    name: str,
    workspace: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> ScriptResult:
    """Run one script and capture its stdout.

    stderr is kept separate and only surfaced on failure. A script that logs
    progress to stderr is normal, and folding that into stdout would make a
    monitor source look changed on every tick.
    """
    try:
        path = resolve(Path(home), name)
        command = command_for(path)
    except ScriptError as exc:
        return ScriptResult(ok=False, output="", error=str(exc))

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace or None,
        )
    except subprocess.TimeoutExpired:
        return ScriptResult(
            ok=False, output="", error=f"{name} did not finish within {timeout}s."
        )
    except OSError as exc:
        return ScriptResult(ok=False, output="", error=f"{name} could not run: {exc}")

    output = completed.stdout[:MAX_OUTPUT]
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:1000] or f"exit {completed.returncode}"
        return ScriptResult(
            ok=False, output=output, error=detail, exit_code=completed.returncode
        )
    return ScriptResult(ok=True, output=output)
