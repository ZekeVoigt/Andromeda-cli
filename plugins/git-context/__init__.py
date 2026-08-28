"""The reference plugin, and the shortest complete example of the socket.

It does one useful thing and demonstrates three of the four registration
families:

    ctx.register_hook     `pre_llm_call`, to put the branch in front of the model
    ctx.register_tool     `git_status`, so it can ask again mid-turn
    ctx.register_command  `/branch`, for the person

There is deliberately no `capabilities:` in its manifest. Everything here
*adds*; nothing replaces a seam the harness already owns. That is the ordinary
case, and a plugin that needs no grant should not have to ask for one.

Why a hook rather than a system-prompt section: the branch changes during a
session. A prompt section is the cached prefix of every request, so putting a
value that moves into it means invalidating that cache every time somebody
checks out. `pre_llm_call` injects into the *user* turn instead, which costs
one line and no cache.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TIMEOUT = 2.0


def _git(*arguments: str, cwd: Path | None = None) -> str:
    """Run one git command. Returns its output, or "" for anything unusual.

    Never raises and never blocks for long. This runs on the path of every
    turn, so a repository on a stalled network mount must cost a couple of
    seconds once, not the session.
    """
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def summary(cwd: Path | None = None) -> str:
    """`branch (clean)` / `branch (3 changed)`, or "" outside a repository."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    if not branch:
        return ""
    changed = _git("status", "--porcelain", cwd=cwd)
    count = len([line for line in changed.splitlines() if line.strip()])
    state = "clean" if count == 0 else f"{count} changed"
    return f"{branch} ({state})"


def register(ctx) -> None:
    def on_llm_call(**_kwargs):
        line = summary()
        # Returning None means "nothing to add". An empty string would be a
        # blank line in the prompt on every turn outside a repository.
        return f"Git: {line}" if line else None

    ctx.register_hook("pre_llm_call", on_llm_call)

    def run_git_status():
        from andromeda_tools.spec import ToolResult, failure

        line = summary()
        if not line:
            return failure("not inside a git repository")
        return ToolResult(content=line, display=line)

    ctx.register_tool(
        "git_status",
        "The current git branch and whether the working tree is clean.",
        {"type": "object", "properties": {}},
        run_git_status,
        # Reads the local repository and nothing else, so it is a safe local
        # read — the same tier `read_file` carries, and for the same reason.
        risk_tier="safe_local",
        category="read",
        summarize=lambda _arguments: "git status",
    )

    ctx.register_command(
        "branch",
        lambda _raw: summary() or "not inside a git repository",
        "The current git branch, and whether it is clean.",
    )
