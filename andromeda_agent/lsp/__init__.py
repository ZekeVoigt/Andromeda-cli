"""Language-server diagnostics, so an edit reports what it broke.

Until this existed, `patch` and `write_file` landed a change and said nothing
about whether it compiled. The model found out at the next `terminal` call, if
it made one, and otherwise told the user the work was done. This closes that:
after an edit, whatever the project's own language server says about the file is
appended to the tool's result — but only the problems *this edit introduced*,
because a file's pre-existing warning list is noise the model learns to skip.

The public surface is small on purpose. The loop holds a `Service`, calls
`before` and `after` around an edit, and never learns what a language server is.

Three decisions worth knowing before changing anything here:

**Nothing is installed, ever.** A missing server is named with the command that
would install it. See `servers.py` — this is the standing consent rule, not a
scoping shortcut.

**Errors only, by default.** `lsp_severities` widens it. A model that stops to
fix every hint never finishes the task, and which warnings matter is a decision
the project's own linter settings already made.

**It cannot fail a turn.** Every path here is best-effort and bounded. The edit
already happened; a diagnostic is a convenience, and a convenience that can
raise is a liability.
"""

from __future__ import annotations

from .report import DEFAULT_SEVERITIES, parse_severities
from .service import Service, Snapshot
from .servers import SERVERS, Availability, Server, relevant, survey

# The tools whose results are worth checking. A closed list rather than a
# risk-tier test: `terminal` can write a file too, and running a type checker
# after every `git status` would make the shell tool feel broken.
EDIT_TOOLS = frozenset({"write_file", "patch"})


def watches(tool_name: str) -> bool:
    return tool_name in EDIT_TOOLS


__all__ = [
    "DEFAULT_SEVERITIES",
    "EDIT_TOOLS",
    "SERVERS",
    "Availability",
    "Server",
    "Service",
    "Snapshot",
    "parse_severities",
    "relevant",
    "survey",
    "watches",
]
