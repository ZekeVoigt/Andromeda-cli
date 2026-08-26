"""Turning diagnostics into something worth putting in front of a model.

Two problems stand between a language server's output and a useful tool result,
and this module is both of them.

**The delta.** A file with two hundred pre-existing warnings reports two
hundred warnings after every edit. The model learns to skip the block, which
costs the whole feature. So only what the edit *introduced* is reported — and
computing that is harder than a set difference, because inserting a line at the
top of a file moves every diagnostic below it. Identical problems at shifted
line numbers would all read as new. The fix is the one `git blame` uses: build
a piecewise map from pre-edit line numbers to post-edit line numbers out of a
diff, apply it to the baseline, and *then* subtract.

**The text is untrusted.** A diagnostic's message contains identifiers from the
file that was just parsed, and that file may have arrived with a clone. A type
alias named `"> Ignore all previous instructions"` puts that string into the
model's context inside a block the model is being told to take seriously. Every
field is flattened to one line, stripped of control characters, capped, and
escaped before it goes anywhere near the output.
"""

from __future__ import annotations

import bisect
import difflib
import html
from typing import Any, Callable, Iterable

# Severity as the protocol numbers it.
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}

# What is reported by default. Errors only: a warning is a matter of taste that
# the project's own linter settles, and a model that stops to fix every hint
# never finishes the task it was given. `lsp_severities` widens it.
DEFAULT_SEVERITIES = frozenset({1})

# Caps. A file with fifty new errors has one problem, not fifty, and the first
# few name it.
MAX_PER_FILE = 15
MAX_TOTAL_CHARS = 3000

# Per-field caps, against a single very long identifier crowding out the rest.
MAX_MESSAGE_CHARS = 240
MAX_CODE_CHARS = 60
MAX_SOURCE_CHARS = 60


def clean(value: Any, *, limit: int) -> str:
    """One line of safe, bounded text from an untrusted server field.

    Four things, each of them load-bearing: newlines collapse so an identifier
    cannot synthesise a line of its own inside the block; control characters go
    so nothing invisible rides along; the length is capped so one field cannot
    push everything else past the limit; and `<`, `>` and `&` are escaped so
    nothing can close the block early and start writing outside it.
    """
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = "".join(char for char in text if char == " " or char.isprintable())
    return html.escape(text.strip()[:limit], quote=False)


def line_of(diagnostic: dict[str, Any]) -> int:
    """The 0-indexed start line, as the protocol reports it."""
    start = (diagnostic.get("range") or {}).get("start") or {}
    try:
        return max(0, int(start.get("line", 0)))
    except (TypeError, ValueError):
        return 0


def column_of(diagnostic: dict[str, Any]) -> int:
    start = (diagnostic.get("range") or {}).get("start") or {}
    try:
        return max(0, int(start.get("character", 0)))
    except (TypeError, ValueError):
        return 0


def identity(diagnostic: dict[str, Any], line: int | None = None) -> tuple:
    """What makes two diagnostics the same one.

    Line is part of it, deliberately. Dropping it would be simpler and would
    also swallow the case worth catching most: the model introducing a *second*
    instance of an error it already had somewhere else. That is new information
    and content-only deduplication throws it away.
    """
    return (
        diagnostic.get("severity") or 1,
        str(diagnostic.get("code") or ""),
        str(diagnostic.get("source") or ""),
        str(diagnostic.get("message") or ""),
        line_of(diagnostic) if line is None else line,
    )


# ---------------------------------------------------------------------------
# The line-shift map
# ---------------------------------------------------------------------------

def build_shift(before: str, after: str) -> Callable[[int], int | None]:
    """Map a pre-edit 0-indexed line to its post-edit line, or `None` if deleted.

    One `SequenceMatcher` pass up front, then a binary search per lookup. Cheap
    enough to run on every edit and apply to every baseline diagnostic.

    A line inside a replaced region maps to the start of its replacement rather
    than to nothing. That is the conservative choice: a diagnostic there is
    probably about the same code, and mapping it means an unchanged problem
    stays out of the report. Mapping it to `None` would call it new.
    """
    before_lines = before.splitlines() if before else []
    after_lines = after.splitlines() if after else []
    if before_lines == after_lines:
        return lambda line: line

    regions: list[tuple[int, int, int, int, str]] = []
    starts: list[int] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before_lines, after_lines, autojunk=False
    ).get_opcodes():
        regions.append((i1, i2, j1, j2, tag))
        starts.append(i1)

    def shift(line: int) -> int | None:
        if not regions:
            return line
        index = bisect.bisect_right(starts, line) - 1
        if index < 0:
            return line
        i1, i2, j1, j2, tag = regions[index]
        if line >= i2:
            # Past the last region we know about — the edit only ever appended.
            return line + (j2 - i2)
        if tag == "equal":
            return j1 + (line - i1)
        if tag == "delete":
            return None
        if tag == "replace":
            # Inside a rewritten block. Anchored at the start of the
            # replacement rather than dropped; see the docstring.
            return min(j1 + (line - i1), max(j1, j2 - 1))
        return j1

    return shift


def new_diagnostics(
    baseline: Iterable[dict[str, Any]],
    current: Iterable[dict[str, Any]],
    *,
    before: str = "",
    after: str = "",
) -> list[dict[str, Any]]:
    """What this edit introduced.

    `before` and `after` are the file's contents on each side of the edit; with
    them the baseline is shifted onto post-edit line numbers before the
    subtraction, so an unchanged error under an inserted line stays out of the
    report. Without them the comparison is done on raw line numbers, which is
    right when the file did not move — a `didSave` with no change, for instance.
    """
    shift = build_shift(before, after)
    known: set[tuple] = set()
    for diagnostic in baseline:
        moved = shift(line_of(diagnostic))
        if moved is None:
            # The edit deleted the line this was about. It genuinely no longer
            # applies, so it is not in the baseline any more.
            continue
        known.add(identity(diagnostic, moved))
    return [item for item in current if identity(item) not in known]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(
    path: str,
    diagnostics: list[dict[str, Any]],
    *,
    severities: frozenset[int] = DEFAULT_SEVERITIES,
    max_per_file: int = MAX_PER_FILE,
) -> str:
    """A block for one file, or `""` when nothing passed the filter.

    `""` rather than "no problems found", because a tool result that grows a
    reassuring paragraph after every successful edit teaches the model to skip
    the end of tool results — and the end of a tool result is where this puts
    the thing it most needs to read.
    """
    passing = [
        item for item in diagnostics
        if int(item.get("severity") or 1) in severities
    ]
    if not passing:
        return ""

    passing.sort(key=lambda item: (line_of(item), column_of(item)))
    shown = passing[:max_per_file]
    lines = []
    for item in shown:
        severity = SEVERITY.get(int(item.get("severity") or 1), "error")
        message = clean(item.get("message"), limit=MAX_MESSAGE_CHARS)
        code = clean(item.get("code"), limit=MAX_CODE_CHARS)
        source = clean(item.get("source"), limit=MAX_SOURCE_CHARS)
        suffix = "".join(part for part in (
            f" [{code}]" if code else "",
            f" ({source})" if source else "",
        ))
        lines.append(
            f"{severity} {line_of(item) + 1}:{column_of(item) + 1} {message}{suffix}"
        )
    remaining = len(passing) - len(shown)
    if remaining > 0:
        lines.append(f"… and {remaining} more")

    safe = html.escape(path, quote=True)
    body = "\n".join(lines)
    return f'<diagnostics file="{safe}">\n{body}\n</diagnostics>'


def block(path: str, diagnostics: list[dict[str, Any]], **kwargs: Any) -> str:
    """`render`, with the sentence that says what the model should do about it.

    Worth the line: without it a model reads a diagnostics block as information
    about the file and carries on, and the whole point is that it caused these
    and should fix them before saying it is done.
    """
    rendered = render(path, diagnostics, **kwargs)
    if not rendered:
        return ""
    return (
        "\n\nProblems this edit introduced, reported by the language server. "
        "Fix them before moving on; if one is not really a problem, say why.\n"
        + truncate(rendered)
    )


def truncate(text: str, *, limit: int = MAX_TOTAL_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker = "\n…[truncated]\n</diagnostics>"
    return text[: max(0, limit - len(marker))] + marker


def parse_severities(raw: Any) -> frozenset[int]:
    """Read `lsp_severities` from config: names, numbers, or a comma-separated string.

    Anything unrecognised falls back to errors only. A misread setting must
    narrow the report, never widen it into a flood the user did not ask for.
    """
    if raw is None or raw == "":
        return DEFAULT_SEVERITIES
    if isinstance(raw, str):
        parts: list[Any] = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = list(raw)
    else:
        parts = [raw]

    names = {name: number for number, name in SEVERITY.items()}
    out: set[int] = set()
    for part in parts:
        if isinstance(part, bool):
            continue
        if isinstance(part, int) and part in SEVERITY:
            out.add(part)
            continue
        key = str(part).strip().lower()
        if key in names:
            out.add(names[key])
        elif key in {"all", "everything"}:
            out.update(SEVERITY)
    return frozenset(out) or DEFAULT_SEVERITIES


__all__ = [
    "DEFAULT_SEVERITIES",
    "MAX_PER_FILE",
    "MAX_TOTAL_CHARS",
    "SEVERITY",
    "block",
    "build_shift",
    "clean",
    "identity",
    "line_of",
    "new_diagnostics",
    "parse_severities",
    "render",
    "truncate",
]
