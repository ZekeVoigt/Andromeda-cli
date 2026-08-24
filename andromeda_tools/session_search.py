"""Recalling past conversations, for the agent rather than for a person.

One tool with four shapes, chosen by which arguments arrive — a model given
four tools for one job picks the wrong one, and a model given one tool with a
`mode` enum picks the wrong value:

  discover  ``query``                    find sessions that mention it
  scroll    ``session_id`` + ``anchor``   read around a hit, in context
  read      ``session_id``               the whole conversation
  browse    nothing                      the most recent sessions

It reads the index, so it costs one SQLite query and no tokens beyond what it
returns. That is the point: "what did we decide about X" is otherwise answered
by loading transcripts into the context window until one of them matches.

**It reaches turns the current conversation has compacted away.** When context
runs out those turns leave the transcript but stay in the index, and the summary
that replaced them says so — so "what was that error again" is a lookup rather
than a guess. A hit from compacted context is labelled, because "I read this
earlier" and "this is in front of me now" are different claims.

**It does not cross profiles.** A profile is an isolation boundary a person set
up on purpose, and a tool that reads through it would quietly undo that. The
person can still look — `andromeda -p work sessions search …` — because they
are the one who drew the line.
"""

from __future__ import annotations

from typing import Any

from .spec import ToolResult, failure

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
DEFAULT_WINDOW = 4
# A whole transcript can be enormous. Read returns the shape of it — the
# opening, the closing, and how much was in between — rather than the middle
# of a thousand-message session nobody asked for.
READ_HEAD = 12
READ_TAIL = 8
LINE_WIDTH = 220


def _age(seconds: float) -> str:
    from time import time

    delta = max(0.0, time() - seconds)
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    if delta < 129600:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _line(
    role: str,
    content: str,
    tool_name: str = "",
    width: int = LINE_WIDTH,
    archived: bool = False,
) -> str:
    marker = {"user": "you", "assistant": "me", "tool": tool_name or "tool"}.get(
        role, role
    )
    if archived:
        marker += " · compacted"
    text = " ".join((content or "").split())
    if len(text) > width:
        text = text[:width] + "…"
    return f"  [{marker}] {text}"


def _row_line(row: dict[str, Any]) -> str:
    return _line(
        row["role"], row["content"], row["tool_name"], archived=bool(row.get("archived"))
    )


def discover(query: str, limit: int, roles: tuple[str, ...]) -> ToolResult:
    from andromeda_cli import state

    hits = state.search(query, limit=limit, filters=state.Filters(roles=roles))
    if not hits:
        titled = state.by_title(query, limit=limit)
        if not titled:
            return ToolResult(
                content=(
                    f"No past session mentions {query!r}. "
                    "This may simply not have been discussed before."
                ),
                display="no sessions matched",
            )
        lines = [f"{len(titled)} session(s) whose opening line matches:"]
        for row in titled:
            lines.append(f"  {row.id}  {_age(row.updated_at)}  {row.title}")
        return ToolResult(content="\n".join(lines), display=f"{len(titled)} by title")

    lines = [
        f"{len(hits)} match(es) for {query!r}. "
        "To read around one: session_search(session_id=…, anchor=…)."
    ]
    for hit in hits:
        label = f"{hit.role} · compacted" if hit.archived else hit.role
        lines.append(
            f"  {hit.session_id} anchor={hit.anchor}  {_age(hit.updated_at)}  "
            f"[{label}] {' '.join(hit.snippet.split())[:LINE_WIDTH]}"
        )
        lines.append(f"      from: {hit.title}")
    return ToolResult(
        content="\n".join(lines),
        display=f"{len(hits)} match(es) in past sessions",
        metadata={"count": len(hits), "route": hits[0].route},
    )


def scroll(session_id: str, anchor: int, window: int) -> ToolResult:
    from andromeda_cli import state

    view = state.anchored(session_id, anchor, window=window)
    if not view["window"]:
        return failure(
            f"No message with anchor {anchor} in session {session_id}. "
            "Search again to get a current anchor."
        )

    row = state.get_session(session_id)
    lines = [
        f"Session {session_id}"
        + (f" · {row.title} · {_age(row.updated_at)}" if row else "")
    ]
    if view["opening"]:
        lines.append("  — how it started —")
        lines += [_row_line(item) for item in view["opening"]]
    if view["before"]:
        lines.append(f"  … {view['before']} earlier message(s) …")
    for item in view["window"]:
        marker = " ←" if item["id"] == anchor else ""
        lines.append(_row_line(item) + marker)
    if view["after"]:
        lines.append(f"  … {view['after']} later message(s) …")
    if view["closing"]:
        lines.append("  — how it ended —")
        lines += [_row_line(item) for item in view["closing"]]
    return ToolResult(
        content="\n".join(lines),
        display=f"read {session_id} around {anchor}",
    )


def read(session_id: str) -> ToolResult:
    from andromeda_cli import state

    row = state.get_session(session_id)
    if row is None:
        matches = state.resolve_prefix(session_id)
        if len(matches) == 1:
            return read(matches[0])
        if matches:
            return failure(
                f"{session_id!r} matches {len(matches)} sessions: "
                + ", ".join(matches[:8])
            )
        return failure(f"No session {session_id!r}.")

    messages = state.transcript(row.id)
    compacted = sum(1 for item in messages if item.get("archived"))
    lines = [
        f"Session {row.id} · {row.title} · {row.turns} turns · "
        f"{_age(row.updated_at)} · {row.workspace or 'no workspace'}"
        + (f" · {compacted} message(s) compacted out" if compacted else "")
    ]
    if len(messages) <= READ_HEAD + READ_TAIL:
        lines += [_row_line(item) for item in messages]
    else:
        lines += [_row_line(item) for item in messages[:READ_HEAD]]
        lines.append(
            f"  … {len(messages) - READ_HEAD - READ_TAIL} message(s) omitted — "
            "session_search(session_id=…, anchor=N) reads any part of it …"
        )
        lines += [_row_line(item) for item in messages[-READ_TAIL:]]
    return ToolResult(content="\n".join(lines), display=f"read session {row.id}")


def browse(limit: int) -> ToolResult:
    from andromeda_cli import state

    rows = state.recent(limit)
    if not rows:
        return ToolResult(
            content="No past sessions on this machine yet.",
            display="no sessions",
        )
    lines = [f"{len(rows)} most recent session(s):"]
    for row in rows:
        lines.append(
            f"  {row.id}  {_age(row.updated_at)}  {row.turns} turns  {row.title}"
        )
    return ToolResult(content="\n".join(lines), display=f"{len(rows)} recent sessions")


def run(
    query: str = "",
    session_id: str = "",
    anchor: Any = None,
    limit: Any = DEFAULT_LIMIT,
    window: Any = DEFAULT_WINDOW,
    role: str = "",
) -> ToolResult:
    """Dispatch by shape. An explicit anchor beats a query — the model asked
    for a specific slice, and answering with a search instead is answering a
    question it did not ask."""
    try:
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        window = max(1, min(int(window or DEFAULT_WINDOW), 20))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW

    roles = tuple(part.strip() for part in (role or "").split(",") if part.strip())

    session_id = (session_id or "").strip()
    if session_id and anchor is not None:
        try:
            at = int(anchor)
        except (TypeError, ValueError):
            return failure(f"anchor must be a whole number, got {anchor!r}.")
        return scroll(session_id, at, window)
    if session_id:
        return read(session_id)
    if (query or "").strip():
        return discover(query.strip(), limit, roles)
    return browse(limit)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Text to look for across every past session. Omit to browse "
                "the most recent ones instead."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Read one session. With `anchor`, read around that message; "
                "without it, read the whole conversation."
            ),
        },
        "anchor": {
            "type": "integer",
            "description": (
                "The `anchor=` value from a search result, to read that message "
                "in context along with how the session opened and ended."
            ),
        },
        "limit": {
            "type": "integer",
            "description": f"How many results. 1-{MAX_LIMIT}, default {DEFAULT_LIMIT}.",
        },
        "window": {
            "type": "integer",
            "description": "How many messages either side of `anchor`. Default 4.",
        },
        "role": {
            "type": "string",
            "description": (
                "Restrict a search to these roles, comma separated: user, "
                "assistant, tool. Searching `user` finds what was asked."
            ),
        },
    },
}

DESCRIPTION = (
    "Search and read past conversations on this machine. Use it when the user "
    "refers to something you discussed before — 'what did we decide about X', "
    "'the thing I asked you last week', 'that error from yesterday'. Search "
    "with `query`; then read a hit in context with `session_id` and "
    "`anchor`. It also reaches turns this conversation compacted away, so a "
    "detail a context summary left out is something to look up rather than "
    "guess at. Never claim something was not discussed without searching."
)


def summarize(arguments: dict[str, Any]) -> str:
    if arguments.get("session_id") and arguments.get("anchor") is not None:
        return f"read session {arguments['session_id']} around {arguments['anchor']}"
    if arguments.get("session_id"):
        return f"read session {arguments['session_id']}"
    if arguments.get("query"):
        return f"search past sessions for {arguments['query']!r}"
    return "list recent sessions"
