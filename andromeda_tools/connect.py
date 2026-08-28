"""Connecting an app, from inside the conversation.

The client could reach any MCP server there is, the catalog knew where nineteen
of them live, and none of it was reachable by the thing the person is actually
talking to. Asked about Stripe, the agent answered "there's nothing in my
toolset that talks to Stripe" — true, and the wrong answer, because connecting
one was a single command it had no idea existed and could not have run.

Worse, the fix on offer was `/exit`, then `andromeda mcp install stripe`, then
start again. Sending somebody out of the session to install the thing they are
mid-sentence about is the friction that stops people ever trying.

So: one tool, three actions.

- `list` — what is connected, what is signed in, and what could be.
- `find` — which catalog entries an app name or a URL points at.
- `connect` — write the config and, when it needs one, open the browser.

**`connect` is `outbound` and stops for approval.** It reaches a third party,
it writes credentials-adjacent config, and it can open a browser at a consent
screen. The original rule was that a browser never opens inside a tool call,
and the reason still holds — signing in is the person's decision, not the
model's. What changed is where that decision is taken: at the approval prompt,
with the server named, rather than in a shell after the conversation ended. The
person still says yes. They just do not have to leave to say it.

**A tool can never invent a server.** `connect` installs a catalog entry by
name and nothing else. A model that could write an arbitrary URL into the
config could be talked into writing one by a web page it read — and the config
is the file that decides what the next session connects to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .spec import ToolResult, ToolSpec, failure

# What `connect` says when a server needs a person at a browser.
SIGN_IN = "not signed in, so its tools are not available yet"

# Said on every path that configures something, because it is true on every
# path: the toolset is chosen when a session starts. A model that believes the
# tools are live now will reach for them on the next turn and get nothing,
# which reads to the person as the connection having failed.
NEXT_SESSION = (
    "These tools are not in this session yet — the toolset is chosen when a "
    "session starts. Tell the user to run /new or start a new session."
)


def _status(home: Path, name: str, config: dict[str, Any]) -> str:
    """One word for where a configured server has got to."""
    from . import mcp as mcp_module

    server = mcp_module.MCPServer(name=name, config=config, home=home)
    if not server.uses_oauth:
        return "ready"
    from . import mcp_auth

    stored = mcp_auth.load(home, name)
    if not stored.tokens.access_token:
        return "not signed in"
    if stored.tokens.valid or stored.tokens.refreshable:
        return "signed in"
    return "sign-in expired"


def summary(home: Path) -> str:
    """What is connected and what could be. The `list` action, and reusable.

    Written for a model to act on rather than for a person to admire: the state
    of each connected server, then the names still available, then the one
    sentence that says the catalog is not a limit.
    """
    from . import mcp_catalog, mcp_config

    try:
        configured = mcp_config.servers(home)
    except Exception:  # noqa: BLE001 - a broken config is a message, not a crash
        configured = {}

    lines: list[str] = []
    if configured:
        lines.append("Connected:")
        for name in sorted(configured):
            state = _status(home, name, configured[name])
            hint = (
                f" — run `andromeda mcp login {name}`, or connect_app with "
                f"action='connect' app='{name}' to do it now"
                if state in {"not signed in", "sign-in expired"}
                else ""
            )
            lines.append(f"  {name}: {state}{hint}")
    else:
        lines.append("Nothing is connected yet.")

    available = [e.name for e in mcp_catalog.entries() if e.name not in configured]
    if available:
        lines.append("")
        lines.append("Available to connect by name: " + ", ".join(available))
    lines.append("")
    lines.append(
        "Those are the ones that connect by name. Any other MCP server can be "
        "added, but that needs its URL, so ask the user for it rather than "
        "guessing one."
    )
    return "\n".join(lines)


def _find(query: str) -> str:
    from . import mcp_catalog

    hits = mcp_catalog.search(query) or mcp_catalog.suggest_for(query)
    if not hits:
        return (
            f"Nothing in the catalog matches {query!r}. That does not mean the "
            f"app has no MCP server — it means this build does not know its "
            f"address. Ask the user for the URL."
        )
    return "\n".join(f"{entry.name}: {entry.summary}" for entry in hits)


def _connect(home: Path, app: str, sign_in: bool, reload=None) -> ToolResult:
    from . import mcp as mcp_module
    from . import mcp_catalog, mcp_config

    entry = mcp_catalog.get(app)
    if entry is None:
        near = mcp_catalog.search(app)
        hint = (
            " Closest: " + ", ".join(item.name for item in near[:4]) if near else ""
        )
        return failure(
            f"`{app}` is not a catalog entry, and this tool only installs "
            f"catalog entries.{hint} If the user has the server's URL, tell "
            f"them: andromeda mcp add {app} --url <endpoint>"
        )

    if entry.env:
        # Values this tool must not invent and must not ask for mid-turn: an
        # API key belongs in a prompt the person answers, not in a tool
        # argument the model filled in.
        needed = ", ".join(spec.name for spec in entry.env)
        return failure(
            f"`{entry.name}` needs credentials ({needed}), which have to be "
            f"entered by the user rather than passed through a tool call. "
            f"Tell them to run: andromeda mcp install {entry.name}"
        )

    config = mcp_catalog.config_for(entry, {})
    try:
        mcp_config.save(home, entry.name, config)
    except Exception as exc:  # noqa: BLE001 - surfaced as a result, never raised
        return failure(f"could not write the config for {entry.name}: {exc}")

    lines = [f"Configured `{entry.name}` — {entry.description}"]

    if entry.auth == "oauth":
        if not sign_in:
            lines.append(f"It is {SIGN_IN}.")
            lines.append(f"Run: andromeda mcp login {entry.name}")
            lines.append(NEXT_SESSION)
            # Nothing to activate: an unauthorized server advertises no tools,
            # so a reload here would add nothing and report that it had.
            return ToolResult(
                content="\n".join(lines), display=f"connect {entry.name}", ok=True
            )
        from . import mcp_auth

        opened: list[str] = []

        def announce(url: str, was_opened: bool) -> None:
            opened.append(url)
            print(
                f"\n  {'Opened your browser' if was_opened else 'Open this'} "
                f"to authorize {entry.name}:\n  {url}\n"
            )

        try:
            mcp_auth.authorize(
                home=home,
                server=entry.name,
                server_url=str(config["url"]),
                config={},
                announce=announce,
            )
        except Exception as exc:  # noqa: BLE001 - a failed sign-in is a result
            lines.append(f"The sign-in did not complete: {exc}")
            lines.append(f"It can be retried with: andromeda mcp login {entry.name}")
            return ToolResult(
                content="\n".join(lines), display=f"connect {entry.name}", ok=False
            )
        lines.append("Signed in.")

    # Proved, not assumed. Reporting a connection that does not work sends the
    # next turn confidently reaching for tools that are not there.
    server = mcp_module.MCPServer(name=entry.name, config=config, home=home)
    try:
        if server.connect():
            names = [str(tool.get("name") or "") for tool in server.tools]
            lines.append(f"{len(names)} tools: " + ", ".join(names[:15]))
            lines.append(_activate(reload, entry.name))
        else:
            lines.append(f"Configured, but it did not answer: {server.error}")
    finally:
        server.close()

    return ToolResult(content="\n".join(lines), display=f"connect {entry.name}", ok=True)


def _activate(reload, name: str) -> str:
    """Bring a just-connected server's tools into this session, or say why not.

    `reload` is the surface's hook. Absent — a one-shot run, a lane, a test —
    the honest answer is the restart, and saying so beats silently doing
    nothing. Present, the tools are usable on the very next step, which is the
    whole point: telling somebody to restart is the harness admitting it cannot
    use the thing it just did.
    """
    if reload is None:
        return NEXT_SESSION
    try:
        added = reload()
    except Exception as exc:  # noqa: BLE001 - a failed reload is not a failed connection
        return f"{NEXT_SESSION} (loading them now failed: {exc})"
    mine = [tool for tool in added if tool.startswith(f"mcp__{name}")] or added
    if not mine:
        return NEXT_SESSION
    return (
        f"{len(mine)} of them are live in this session now — no restart needed. "
        f"You can call them on your next step."
    )


def run(
    home: Path, action: str, app: str = "", sign_in: bool = True, reload=None
) -> ToolResult:
    chosen = (action or "list").strip().lower()
    if chosen == "list":
        return ToolResult(content=summary(home), display="connected apps", ok=True)
    if chosen == "find":
        if not app:
            return failure("`find` needs an app name or a URL in `app`.")
        return ToolResult(content=_find(app), display=f"find {app}", ok=True)
    if chosen == "connect":
        if not app:
            return failure("`connect` needs the catalog name of an app in `app`.")
        return _connect(home, app, sign_in, reload)
    return failure(f"unknown action {action!r} — one of list, find, connect")


def spec(home: Path, reload=None) -> ToolSpec:
    """The tool, bound to one home.

    Its description is doing real work. The failure this exists to fix was not
    the model being unable to connect an app — it was the model not knowing
    that connecting one was a thing that could happen, and answering "I have no
    Stripe access" as though that were the end of it.
    """
    return ToolSpec(
        name="connect_app",
        description=(
            "Connect a third-party app (Stripe, Linear, Notion, Figma, Sentry, "
            "Supabase, Vercel, Jira and others) so its tools become available. "
            "This is how this agent gains access to an app it has no tools for. "
            "When the user asks about an app you have no tools for, use "
            "action='list' or 'find' to see whether it can be connected, and "
            "offer to connect it — do not simply report that you lack access. "
            "Actions: 'list' what is connected and what is available, 'find' a "
            "catalog entry by app name or URL, 'connect' one by name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "find", "connect"],
                    "description": "What to do. Default 'list'.",
                },
                "app": {
                    "type": "string",
                    "description": "Catalog name for 'connect'; a name or URL for 'find'.",
                },
                "sign_in": {
                    "type": "boolean",
                    "description": (
                        "For 'connect': open a browser to authorize now. True by "
                        "default. False writes the config and leaves signing in "
                        "to the user."
                    ),
                },
            },
            "required": ["action"],
        },
        # Reaches a third party, writes the file that decides what the next
        # session connects to, and can open a consent screen. The person says
        # yes at the approval prompt — which is the point of putting it here
        # rather than behind a command they have to leave to run.
        risk_tier="outbound",
        category="write",
        run=lambda action="list", app="", sign_in=True: run(
            home, action, app, sign_in, reload
        ),
        summarize=lambda arguments: (
            f"connect {arguments.get('app', '')}"
            if arguments.get("action") == "connect"
            else f"{arguments.get('action', 'list')} connectable apps"
        ),
    )
