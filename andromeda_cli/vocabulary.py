"""Every command this product has, in one list, so nothing is undiscoverable.

There were two vocabularies and no way to see either of them. Inside a session,
twenty-odd slash commands listed in a string somebody maintained by hand. Outside
it, thirty `andromeda <verb>` commands that a session never mentioned. Nothing
completed, so both were things you had to already know. People were not failing
to *use* `andromeda mcp` — they were failing to find out it existed.

So: one registry, three consumers. `/help` prints it, the completer filters it,
and both surfaces read the same rows. A command added to the CLI shows up in the
palette without anybody remembering to add it twice, and the two lists cannot
drift because there is only one list.

**Verbs are here too, not just slashes.** A session that only offers its own
twenty words is a session that hides the other thirty. Typing `/mcp` runs the
`andromeda mcp` you would have run in a shell, without leaving the conversation
to do it — because dropping out of a session to run a command and then coming
back is the friction that stops people trying.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

# Slash commands that only exist inside a session — they act on the
# conversation, so there is nothing for them to be outside one.
#
# The order is the order they print in: what you do often, then what you do when
# something has gone sideways, then the accounting, then the exits. Alphabetical
# would be easier to maintain and much worse to read.
CONVERSATION: tuple[tuple[str, str], ...] = (
    ("help", "show this"),
    ("new", "start a fresh conversation"),
    ("rewind", "undo the last exchange (/rewind N for a numbered checkpoint)"),
    ("history", "list the checkpoints you can rewind to"),
    ("recap", "what has happened so far, without asking the model"),
    ("resume", "switch to another session (/resume lists them)"),
    ("sessions", "search past sessions (/sessions <text>)"),
    ("ps", "background processes started this session"),
    ("tools", "list the tools this session can use"),
    ("skills", "list the skills on this machine"),
    ("lanes", "list the delegation specialists"),
    ("jobs", "scheduled jobs and what they have done"),
    ("approve", "let a job act, not just look (/approve <id>)"),
    ("model", "show the model in use"),
    ("think", "show or set the thinking level (off, low, medium, high)"),
    ("cwd", "show the workspace root"),
    ("credits", "the account balance, as the last reply reported it"),
    ("usage", "what this session and this week have spent, in tokens"),
    ("upgrade", "change your plan, in the browser"),
    ("exit", "leave (Ctrl-D also works)"),
)

# Verbs worth reaching without leaving the session. Not every command — `backup`
# and `restore` want a shell and a path, and offering them here would be
# offering something that then asks you to go elsewhere anyway. These are the
# ones where the answer is a list you wanted to look at.
REACHABLE: tuple[str, ...] = (
    "mcp",
    "cron",
    "secrets",
    "skills",
    "plugins",
    "approvals",
    "status",
    "doctor",
    "sessions",
    "worktrees",
    "browser",
    "memory",
    "config",
)


@dataclass(frozen=True)
class Command:
    """One row of the palette."""

    name: str
    summary: str
    # "conversation" for a slash command that acts on this session, "verb" for
    # one that runs an `andromeda` command, "plugin" for one a package added.
    kind: str = "conversation"

    @property
    def display(self) -> str:
        return f"/{self.name}"


def _verbs() -> list[Command]:
    """The `andromeda <verb>` surface, read from the parser that defines it.

    Walked rather than listed, so this cannot fall behind: a verb added to
    `build_command_parser` is in the palette the moment it is added, with the
    help text it was given there.
    """
    from . import completion
    from .__main__ import build_command_parser

    try:
        tree = completion.walk(build_command_parser())
    except Exception:  # noqa: BLE001 - a palette must never be why a session fails
        return []

    out: list[Command] = []
    for name, info in sorted(tree.get("subcommands", {}).items()):
        if name not in REACHABLE:
            continue
        summary = str(info.get("help") or "").strip()
        children = sorted(info.get("subcommands", {}))
        if children:
            # The sub-verbs matter more than the parent's one-liner. "Connect
            # and manage MCP servers" does not tell you that `install` exists,
            # and `install` is the word somebody is looking for.
            summary = f"{summary} — {', '.join(children[:6])}"
        out.append(Command(name=name, summary=summary, kind="verb"))
    return out


def _plugins() -> list[Command]:
    """Commands a package registered.

    These were dispatchable and invisible: `_plugin_command` would run `/foo`
    quite happily while nothing anywhere said `/foo` was a thing.
    """
    try:
        from andromeda_agent import plugins as plugins_module

        registered = plugins_module.plugin_commands()
    except Exception:  # noqa: BLE001 - a broken package is not a broken palette
        return []
    return [
        Command(
            name=name,
            summary=str(getattr(registration, "description", "") or "from a plugin"),
            kind="plugin",
        )
        for name, registration in sorted(registered.items())
    ]


def commands() -> list[Command]:
    """Everything, in the order a palette should show it.

    Conversation commands first: they are what the slash key is mostly for, and
    burying `/new` under an alphabetised merge with thirty verbs would be a
    worse list than the hardcoded one this replaces.

    A plugin that registered a built-in's name appears once, as the plugin —
    which is what will actually run, and a palette that promises otherwise is
    lying about behaviour.
    """
    plugin_rows = _plugins()
    taken = {row.name for row in plugin_rows}

    out = [
        Command(name=name, summary=summary)
        for name, summary in CONVERSATION
        if name not in taken
    ]
    out.extend(row for row in plugin_rows if row.kind == "plugin")

    # A name in both halves — `/skills`, `/sessions` — is listed once, as the
    # conversation command, because that is the one that runs: the built-in
    # chain is checked before a verb is considered. Showing both would put a
    # row in the palette that cannot be reached from it.
    taken |= {row.name for row in out}
    out.extend(row for row in _verbs() if row.name not in taken)
    return out


def matching(prefix: str) -> list[Command]:
    """Rows for what has been typed so far.

    A leading `/` is optional and stripped — the completer sees it, `/help`
    does not. Matching is on the start of the name first and anywhere in the
    row second, so typing `mc` puts `mcp` above a command that merely mentions
    it, and typing `stripe` still finds nothing rather than pretending.
    """
    needle = (prefix or "").lstrip("/").strip().lower()
    if not needle:
        return commands()

    starts = [row for row in commands() if row.name.lower().startswith(needle)]
    contains = [
        row
        for row in commands()
        if row not in starts and needle in f"{row.name} {row.summary}".lower()
    ]
    return starts + contains


def _rows(items: Iterable[Command], width: int) -> list[str]:
    return [f"  /{row.name.ljust(width)}  {row.summary}" for row in items]


def help_text() -> str:
    """What `/help` prints. Generated, so it cannot be out of date.

    Sectioned, because the two halves answer different questions — "what can I
    do to this conversation" and "what else does this thing have" — and running
    them together as one alphabetical block is how a list of fifty items
    becomes a list nobody reads.
    """
    rows = commands()
    width = max((len(row.name) for row in rows), default=8)

    conversation = [row for row in rows if row.kind == "conversation"]
    plugin_rows = [row for row in rows if row.kind == "plugin"]
    verbs = [row for row in rows if row.kind == "verb"]

    lines: list[str] = ["", *_rows(conversation, width)]
    if plugin_rows:
        lines += ["", "  from plugins", *_rows(plugin_rows, width)]
    if verbs:
        lines += [
            "",
            "  these run the `andromeda` command of the same name",
            *_rows(verbs, width),
        ]
    lines += ["", "  Type / to filter this list as you go.", ""]
    return "\n".join(lines)


def is_verb(name: str) -> bool:
    """Whether `/name` should run the `andromeda name` command."""
    wanted = (name or "").lstrip("/").strip().lower()
    return any(row.name == wanted and row.kind == "verb" for row in commands())


def run_verb(name: str, arguments: str = "") -> int:
    """Run `andromeda <name> <arguments>` in this process.

    In-process rather than as a subprocess: it is the same code, it prints to
    the same console, and spawning a second interpreter to do what this one can
    already do would add a second of startup to a command whose whole point is
    that you did not have to leave.
    """
    import shlex

    from . import output
    from .__main__ import _run_command

    try:
        argv = [name.lstrip("/"), *shlex.split(arguments)]
    except ValueError as exc:
        output.fail(f"could not read those arguments: {exc}")
        return 1

    try:
        return _run_command(argv)
    except SystemExit as exc:
        # argparse calls `sys.exit` on a bad argument, having already printed
        # what was wrong with it. Caught here because that must end the
        # command, not the session somebody is in the middle of.
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - a verb must not end the session
        output.fail(f"/{name.lstrip('/')} failed: {exc}")
        return 1


__all__ = [
    "Command",
    "commands",
    "help_text",
    "is_verb",
    "matching",
    "run_verb",
]
