"""Watching something cheaply.

The problem this solves is the one that makes "check X every ten minutes"
unaffordable: a schedule that fires 144 times a day bills 144 full agent turns,
and 143 of them conclude "nothing changed". The model is the most expensive
part of the loop and the least necessary part of *noticing*.

So a monitor job attaches a cheap source — a script, or a URL — that runs
first. Its output is hashed, and:

- unchanged → **the agent does not run at all.** The tick is recorded as
  `no_change`, so the history still shows the job is alive.
- changed, or first ever run → a change block (a unified diff, plus the new
  output) is injected into the prompt, and the turn proceeds normally.
- the source failed → an **error**, never a change. The stored hash is left
  alone, so a source that recovers to its previous output still suppresses
  rather than announcing a change that never happened.

The rule that is easiest to get wrong: **comparison is exact bytes.** No timestamp stripping, no whitespace
normalisation. Normalising means guessing which differences are meaningful, and
a guess that is wrong in the quiet direction is a monitor that never fires. The
cost is that a source emitting `Generated at 14:02:11` looks changed every
tick — which is the source's bug, and the job's description says so.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import scripts as scripts_module

# The diff is prompt text. A source that rewrites itself entirely would
# otherwise put its whole output in twice — once as the diff, once as the new
# value — on every change.
MAX_DIFF_LINES = 200
MAX_OUTPUT_CHARS = 20_000

MONITOR_KINDS = ("script", "url")


@dataclass
class Sample:
    """One reading of a monitor source."""

    ok: bool
    text: str = ""
    error: str = ""

    @property
    def digest(self) -> str:
        return digest(self.text)


def digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def read(
    kind: str,
    source: str,
    home: Path,
    workspace: str = "",
    allow_private_network: bool = False,
) -> Sample:
    """Take a reading. Never raises — a broken source is a recorded error."""
    if kind == "script":
        result = scripts_module.run(home, source, workspace=workspace)
        if not result.ok:
            return Sample(ok=False, error=result.error)
        return Sample(ok=True, text=result.output)

    if kind == "url":
        # Through the same fetcher the `web_fetch` tool uses, so a monitor URL
        # gets the same SSRF guard — including the re-check after redirects. A
        # URL that reaches a metadata endpoint is not less dangerous for being
        # on a schedule; it is more, because nobody is watching it happen.
        from andromeda_tools import web

        result = web.fetch(source, allow_private=allow_private_network)
        if not result.ok:
            return Sample(ok=False, error=result.content[:500])
        return Sample(ok=True, text=result.content)

    return Sample(ok=False, error=f"Unknown monitor kind {kind!r}.")


def change_block(previous: str, current: str) -> str:
    """What the model is told about the change.

    The diff first, because that is the answer to "what happened"; the full new
    output after it, because the diff alone loses the context a line changed
    inside.
    """
    diff = list(
        difflib.unified_diff(
            (previous or "").splitlines(),
            (current or "").splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    truncated = ""
    if len(diff) > MAX_DIFF_LINES:
        truncated = f"\n[... {len(diff) - MAX_DIFF_LINES} more diff lines]"
        diff = diff[:MAX_DIFF_LINES]

    body = current[:MAX_OUTPUT_CHARS]
    if len(current) > MAX_OUTPUT_CHARS:
        body += f"\n[... truncated at {MAX_OUTPUT_CHARS} characters]"

    if not previous:
        # First reading. There is nothing to diff against, and presenting an
        # all-additions diff as "what changed" would be a lie about a baseline.
        return (
            "MONITOR BASELINE — this is the first reading of the source, so "
            "there is nothing to compare it against. Treat it as the starting "
            "state, not as a change.\n\n" + body
        )

    return (
        "MONITOR CHANGE DETECTED — the watched source is different from the "
        "last time this job ran.\n\nWhat changed:\n"
        + "\n".join(diff)
        + truncated
        + "\n\nThe source now reads:\n"
        + body
    )
