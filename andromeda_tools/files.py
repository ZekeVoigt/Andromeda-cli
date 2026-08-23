"""Reading and changing files on the user's own disk."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from .spec import ToolResult, failure
from .workspace import PathOutsideWorkspace, Workspace

# A tool result becomes context on the next turn, so an unbounded read is a
# bill as well as a wall of text. Both limits are generous enough that a normal
# source file is never truncated.
MAX_READ_LINES = 2000
MAX_READ_BYTES = 400_000
MAX_MATCHES = 200

BINARY_HINT = b"\x00"


def _read_text(path: Path) -> str | None:
    """Return the file's text, or None if it is not text.

    A NUL byte in the first block is the same heuristic `grep` uses. Decoding a
    binary as UTF-8 with replacement would "succeed" and hand the model a
    screenful of U+FFFD.
    """
    with path.open("rb") as handle:
        head = handle.read(8192)
    if BINARY_HINT in head:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def read_file(
    workspace: Workspace,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_LINES,
) -> ToolResult:
    try:
        target = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    if not target.exists():
        return failure(f"{workspace.relative(target)} does not exist.")
    if target.is_dir():
        return failure(f"{workspace.relative(target)} is a directory. Use list_dir.")
    if target.stat().st_size > MAX_READ_BYTES:
        return failure(
            f"{workspace.relative(target)} is "
            f"{target.stat().st_size:,} bytes — too large to read whole. "
            "Pass offset and limit, or search it instead."
        )

    text = _read_text(target)
    if text is None:
        return failure(f"{workspace.relative(target)} is not a text file.")

    lines = text.splitlines()
    offset = max(0, offset)
    window = lines[offset : offset + max(1, limit)]

    # Line numbers so the model can cite and patch by position.
    width = len(str(offset + len(window)))
    body = "\n".join(
        f"{str(offset + i + 1).rjust(width)}\t{line}" for i, line in enumerate(window)
    )

    truncated = offset + len(window) < len(lines)
    if truncated:
        body += (
            f"\n\n… {len(lines) - offset - len(window)} more lines. "
            f"Read from offset {offset + len(window)} to continue."
        )

    return ToolResult(
        content=body or "(empty file)",
        display=f"{workspace.relative(target)} — {len(window)} lines",
        metadata={"lines": len(lines), "truncated": truncated},
    )


def write_file(workspace: Workspace, path: str, content: str) -> ToolResult:
    try:
        target = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    if target.is_dir():
        return failure(f"{workspace.relative(target)} is a directory.")

    existed = target.exists()
    previous_size = target.stat().st_size if existed else 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    verb = "Overwrote" if existed else "Created"
    detail = f" (was {previous_size:,} bytes)" if existed else ""
    return ToolResult(
        content=f"{verb} {workspace.relative(target)} — {len(content):,} bytes{detail}.",
        metadata={"created": not existed},
    )


def patch(
    workspace: Workspace,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    """Exact-string replacement.

    Ambiguity is an error, not a coin flip: if `old_string` appears more than
    once and `replace_all` was not asked for, the edit is refused with the
    count. Silently taking the first match is how an edit lands in the wrong
    function.
    """
    try:
        target = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    if not target.exists():
        return failure(f"{workspace.relative(target)} does not exist.")

    text = _read_text(target)
    if text is None:
        return failure(f"{workspace.relative(target)} is not a text file.")

    if old_string == new_string:
        return failure("old_string and new_string are identical — nothing to do.")

    count = text.count(old_string)
    if count == 0:
        return failure(
            f"old_string was not found in {workspace.relative(target)}. "
            "It must match exactly, including whitespace and indentation."
        )
    if count > 1 and not replace_all:
        return failure(
            f"old_string appears {count} times in {workspace.relative(target)}. "
            "Include more surrounding context to make it unique, or pass replace_all."
        )

    updated = text.replace(old_string, new_string)
    target.write_text(updated, encoding="utf-8")

    replaced = count if replace_all else 1
    return ToolResult(
        content=(
            f"Patched {workspace.relative(target)} — "
            f"{replaced} replacement{'s' if replaced != 1 else ''}."
        ),
        metadata={"replacements": replaced},
    )


def list_dir(workspace: Workspace, path: str = ".") -> ToolResult:
    try:
        target = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    if not target.is_dir():
        return failure(f"{workspace.relative(target)} is not a directory.")

    entries = sorted(
        target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
    )
    rendered = [f"{item.name}/" if item.is_dir() else item.name for item in entries]
    body = "\n".join(rendered) or "(empty directory)"
    return ToolResult(
        content=body,
        display=f"{workspace.relative(target)} — {len(entries)} entries",
    )


def search_files(
    workspace: Workspace,
    pattern: str,
    path: str = ".",
    glob: str = "*",
) -> ToolResult:
    """Regex search across the workspace.

    Pure Python rather than shelling out to ripgrep: `search_files` is
    `safe_local` and must stay that way. Routing it through a subprocess would
    make it a shell call wearing a read tool's tier.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return failure(f"{pattern!r} is not a valid regular expression: {exc}")

    try:
        root = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    if not root.is_dir():
        return failure(f"{workspace.relative(root)} is not a directory.")

    matches: list[str] = []
    scanned = 0
    truncated = False

    for candidate in sorted(root.rglob("*")):
        if len(matches) >= MAX_MATCHES:
            truncated = True
            break
        if not candidate.is_file():
            continue
        if any(part.startswith(".") or part == "node_modules" for part in candidate.parts):
            continue
        if not fnmatch.fnmatch(candidate.name, glob):
            continue

        try:
            text = _read_text(candidate)
        except OSError:
            continue
        if text is None:
            continue

        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                matches.append(
                    f"{workspace.relative(candidate)}:{number}: {line.strip()[:200]}"
                )
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break

    if not matches:
        return ToolResult(
            content=f"No matches for {pattern!r} in {scanned} files.",
            display=f"no matches ({scanned} files)",
        )

    body = "\n".join(matches)
    if truncated:
        body += f"\n\n… stopped at {MAX_MATCHES} matches. Narrow the pattern or the glob."

    return ToolResult(
        content=body,
        display=f"{len(matches)} match{'es' if len(matches) != 1 else ''} in {scanned} files",
        metadata={"matches": len(matches), "truncated": truncated},
    )


def arguments_summary_read(arguments: dict[str, Any]) -> str:
    return f"read {arguments.get('path', '?')}"


def arguments_summary_write(arguments: dict[str, Any]) -> str:
    content = arguments.get("content") or ""
    return f"write {arguments.get('path', '?')} ({len(content):,} bytes)"


def arguments_summary_patch(arguments: dict[str, Any]) -> str:
    scope = "all occurrences" if arguments.get("replace_all") else "one occurrence"
    return f"patch {arguments.get('path', '?')} ({scope})"
