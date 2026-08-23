"""Listing, searching and inspecting past sessions."""

from __future__ import annotations

import time

from .. import output
from .. import sessions as store


def _age(timestamp: float) -> str:
    seconds = max(0, time.time() - timestamp)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def show_list(limit: int = store.LIST_LIMIT) -> int:
    found = store.recent(limit)
    if not found:
        output.info("No saved sessions yet.")
        return 0

    for session in found:
        output.console.print(
            f"  [cyan]{session.id}[/cyan]  "
            f"[dim]{_age(session.updated_at).rjust(9)}  "
            f"{str(session.turns).rjust(3)} turn{' ' if session.turns == 1 else 's'}[/dim]"
            f"  {session.title}"
        )
    output.console.print()
    output.info("  andromeda --resume <id>")
    output.console.print(f"  [dim]{store.sessions_dir()}[/dim]", soft_wrap=True)
    return 0


def show(prefix: str) -> int:
    session = store.resolve(prefix)
    if session is None:
        output.fail(f"No session matching {prefix!r}.", "andromeda sessions list")
        return 2

    output.info(f"  {session.id} · {session.model} · {session.workspace}")
    output.console.print()
    for message in session.messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system" or not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            output.console.print(f"[bold cyan]› {content.strip()}[/bold cyan]\n", markup=True)
        elif role == "assistant":
            output.console.print(content.strip(), markup=False, highlight=False)
            output.console.print()
        elif role == "tool":
            first = content.strip().splitlines()[0] if content.strip() else ""
            output.console.print(f"  [dim]⚙ {first[:120]}[/dim]")
    return 0


def find(query: str) -> int:
    results = store.search(query)
    if not results:
        output.info(f"Nothing found for {query!r}.")
        return 1

    for session, line in results:
        output.console.print(
            f"  [cyan]{session.id}[/cyan]  [dim]{_age(session.updated_at).rjust(9)}[/dim]  {line}"
        )
    return 0
