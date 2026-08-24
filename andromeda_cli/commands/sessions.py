"""Listing, searching, reading, exporting and repairing past sessions.

Reads the index for everything except `show`, which reads the transcript file
directly — a session you cannot open because the index is broken is exactly
the session you most want to be able to open.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import output
from .. import render
from .. import sessions as store
from .. import state
from ..state import export as export_module
from ..state import filters as filters_module
from ..state import live as live_module
from ..state import recovery as recovery_module


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


def _resolve(prefix: str) -> "store.Session | None":
    """A session by id or unique id prefix.

    The index answers first because it is one query; the files answer when the
    index has not caught up, which is the case immediately after a session is
    created by a build that could not write to the database.
    """
    matches = state.resolve_prefix(prefix)
    if len(matches) == 1:
        found = store.load(matches[0])
        if found is not None:
            return found
    if len(matches) > 1:
        output.fail(
            f"{prefix!r} matches {len(matches)} sessions.",
            ", ".join(matches[:8]),
        )
        return None
    return store.resolve(prefix)


def _ensure_fresh() -> None:
    """Catch the index up if transcripts have moved underneath it.

    Cheap when there is nothing to do — a stat per transcript — and it is what
    makes every command below correct on an install where a session was
    written by a process that could not reach the database.
    """
    try:
        if state.stale_count():
            state.reindex()
    except Exception:  # noqa: BLE001 - listing must not fail over an index
        pass


# ---- listing --------------------------------------------------------------


def show_list(limit: int = store.LIST_LIMIT, **filter_args) -> int:
    _ensure_fresh()
    try:
        narrowed = filters_module.build(**filter_args)
    except filters_module.FilterError as exc:
        output.fail(str(exc))
        return 2

    found = state.recent(limit, narrowed)
    if not found:
        described = filters_module.describe(narrowed)
        output.info(
            f"No sessions matching {described}." if described else "No saved sessions yet."
        )
        return 0

    holders = {item.session_id for item in live_module.all_live()}
    for row in found:
        mark = "[green]●[/green]" if row.id in holders else " "
        output.console.print(
            f" {mark}[cyan]{row.id}[/cyan]  "
            f"[dim]{_age(row.updated_at).rjust(9)}  "
            f"{str(row.turns).rjust(3)} turn{' ' if row.turns == 1 else 's'}[/dim]"
            f"  {row.title}"
        )
    output.console.print()
    described = filters_module.describe(narrowed)
    if described:
        output.info(f"  filtered: {described}")
    output.info("  andromeda --resume <id>")
    output.console.print(f"  [dim]{store.sessions_dir()}[/dim]", soft_wrap=True)
    return 0


def _archived_turns(session_id: str) -> list[dict]:
    """Turns compaction folded away, oldest first.

    They are chronologically before everything left in the transcript — that is
    what compaction does — so printing them ahead of the file is the correct
    order without needing to interleave anything.
    """
    try:
        return [
            row
            for row in state.transcript(session_id, limit=2000)
            if row.get("archived")
        ]
    except Exception:  # noqa: BLE001 - the file is still readable without them
        return []


def show(prefix: str, live_only: bool = False) -> int:
    session = _resolve(prefix)
    if session is None:
        output.fail(f"No session matching {prefix!r}.", "andromeda sessions list")
        return 2

    output.info(f"  {session.id} · {session.model} · {session.workspace}")
    output.console.print()

    # Compaction removes turns from the transcript on disk, so the file alone
    # is not the conversation any more. The index keeps them, and a person
    # reading a session should see what the agent can still search.
    folded = [] if live_only else _archived_turns(session.id)
    if folded:
        output.console.print(
            f"  [yellow]— {len(folded)} turn(s) compacted out of the live "
            "conversation, kept here —[/yellow]\n"
        )
        for row in folded:
            body = (row.get("content") or "").strip()
            if not body:
                continue
            if row["role"] == "user":
                output.console.print(f"[cyan]› {body}[/cyan]\n", markup=True)
            elif row["role"] == "assistant":
                output.console.print(body, markup=False, highlight=False)
                output.console.print()
            else:
                first = body.splitlines()[0] if body else ""
                output.console.print(f"  [dim]⚙ {first[:120]}[/dim]")
        output.console.print("  [yellow]— the live transcript follows —[/yellow]\n")

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


# ---- searching ------------------------------------------------------------


def find(query: str, limit: int = store.LIST_LIMIT, **filter_args) -> int:
    _ensure_fresh()
    try:
        narrowed = filters_module.build(**filter_args)
    except filters_module.FilterError as exc:
        output.fail(str(exc))
        return 2

    hits = state.search(query, filters=narrowed, limit=limit)
    if not hits:
        titled = state.by_title(query, limit=limit)
        if not titled:
            output.info(f"Nothing found for {query!r}.")
            return 1
        output.info(f"No message matched, but {len(titled)} session(s) open with it:")
        for row in titled:
            output.console.print(
                f"  [cyan]{row.id}[/cyan]  [dim]{_age(row.updated_at).rjust(9)}[/dim]  {row.title}"
            )
        return 0

    for hit in hits:
        # The FTS snippet marks the match with guillemets; turn those into
        # colour rather than leaving punctuation the user has to decode.
        marked = (
            " ".join(hit.snippet.split())
            .replace("»", "[bold yellow]")
            .replace("«", "[/bold yellow]")
        )
        output.console.print(
            f"  [cyan]{hit.session_id}[/cyan][dim]@{hit.position}[/dim]  "
            f"[dim]{_age(hit.updated_at).rjust(9)}  {hit.role.ljust(9)}[/dim]  {marked}"
        )
    output.console.print()
    output.info(f"  {len(hits)} match(es) via {hits[0].route}")
    output.info("  andromeda sessions show <id>")
    return 0


# ---- recap ----------------------------------------------------------------


def recap(prefix: str = "") -> int:
    session = _resolve(prefix) if prefix else store.latest()
    if session is None:
        output.info("No session to recap.")
        return 1
    summary = state.build_recap(session.messages)
    output.info(f"  {session.id} · {_age(session.updated_at)}")
    for line in summary.lines():
        output.console.print(f"  [dim]{line}[/dim]" if not line.startswith("you asked") else f"  {line}")
    return 0


# ---- export ---------------------------------------------------------------


def export(prefix: str, fmt: str = "markdown", destination: str = "") -> int:
    session = _resolve(prefix)
    if session is None:
        output.fail(f"No session matching {prefix!r}.", "andromeda sessions list")
        return 2
    try:
        rendered = export_module.render(session, fmt)
    except ValueError as exc:
        output.fail(str(exc))
        return 2

    if not destination:
        # To stdout, unrendered: this is output meant to be piped, and rich
        # would wrap it, colour it and corrupt the file on the other end.
        print(rendered, end="")
        return 0

    path = Path(destination).expanduser()
    if path.is_dir():
        path = path / f"{session.id}{export_module.suffix(fmt)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    output.ok(f"Wrote {path} ({len(rendered) / 1024:.0f} KB)")
    return 0


# ---- deleting -------------------------------------------------------------


def remove(prefix: str, force: bool = False) -> int:
    session = _resolve(prefix)
    if session is None:
        output.fail(f"No session matching {prefix!r}.", "andromeda sessions list")
        return 2
    if not force:
        output.fail(
            f"{session.id} · {session.turns} turns · {session.title}",
            "Pass --force to delete it. There is no undo.",
        )
        return 2
    session.path.unlink(missing_ok=True)
    state.forget_session(session.id)
    output.ok(f"Deleted {session.id}")
    return 0


# ---- index health ---------------------------------------------------------


def reindex(force: bool = False) -> int:
    counts = state.reindex(force=force)
    output.ok(
        f"Indexed {counts['scanned']} transcript(s): "
        f"{counts['rebuilt']} rebuilt, {counts['appended']} appended, "
        f"{counts['unchanged']} unchanged"
    )
    if counts["unreadable"]:
        output.info(
            f"  {counts['unreadable']} could not be read — andromeda sessions doctor"
        )
    if counts["dropped"]:
        output.info(f"  {counts['dropped']} index row(s) dropped for deleted files")
    return 0


def doctor() -> int:
    report = state.check()
    capability = state.capabilities()

    output.console.print(f"  [dim]index[/dim]      {report.database}")
    output.console.print(
        f"  [dim]sqlite[/dim]     {capability['sqlite']}"
        f"   fts5: {'yes' if report.fts else 'no'}"
        f"   trigram: {'yes' if report.trigram else 'no'}"
    )
    output.console.print(f"  [dim]integrity[/dim]  {report.integrity}")
    counted = state.counts()
    archived = counted.get("archived", 0)
    output.console.print(
        f"  [dim]sessions[/dim]   {report.sessions_on_disk} on disk · "
        f"{report.sessions_indexed} indexed · {report.messages_indexed} messages"
        + (f" ({archived} compacted out, kept only here)" if archived else "")
    )
    if report.live_claims:
        live = live_module.all_live()
        output.console.print(f"  [dim]open now[/dim]   {len(live)}")

    if report.error:
        output.fail(f"The index could not be opened: {report.error}")
        output.info("  andromeda sessions recover --rebuild-index")
        return 1

    if not report.fts:
        output.info(
            "  No FTS5 in this Python's SQLite — search still works, "
            "through a slower substring scan."
        )

    if report.stale:
        output.info(f"  {report.stale} transcript(s) newer than the index")
        output.info("  andromeda sessions reindex")

    if report.damaged:
        output.console.print()
        for damaged in report.damaged:
            recoverable = (
                f"{damaged.salvageable} message(s) recoverable"
                if damaged.salvageable
                else "nothing recoverable"
            )
            output.console.print(
                f"  [yellow]{damaged.path.name}[/yellow]  "
                f"[dim]{damaged.reason} — {recoverable}[/dim]"
            )
        output.info("  andromeda sessions recover")
        return 1

    if report.healthy:
        output.ok("Everything readable and indexed.")
    return 0


def recover(apply: bool = False, rebuild: bool = False) -> int:
    if rebuild:
        counts = state.rebuild_index()
        output.ok(
            f"Rebuilt the index from {counts['scanned']} transcript(s) "
            f"({counts['unreadable']} unreadable)"
        )
        return 0

    outcome = recovery_module.repair(apply=apply)
    if not outcome.recovered and not outcome.lost:
        output.ok("Nothing to recover — every transcript reads cleanly.")
        return 0

    for session_id in outcome.recovered:
        output.console.print(f"  [green]recoverable[/green]  {session_id}")
    for path in outcome.lost:
        output.console.print(f"  [red]unrecoverable[/red]  {path.name}")

    if not apply:
        output.console.print()
        output.info("  Nothing was changed. Pass --apply to write the salvage back.")
        output.info(f"  Originals would move to {recovery_module.quarantine_dir()}")
        return 0

    output.ok(
        f"Recovered {len(outcome.recovered)} session(s); "
        f"originals kept in {recovery_module.quarantine_dir()}"
    )
    return 0


# ---- what is open right now ------------------------------------------------


def active() -> int:
    sessions = live_module.all_live()
    if not sessions:
        output.info("No sessions open right now.")
        return 0
    for item in sessions:
        mine = " [dim](this terminal)[/dim]" if item.mine else ""
        output.console.print(
            f"  [cyan]{item.session_id}[/cyan]  [dim]pid {item.pid} · "
            f"{item.surface} · open {int(item.age / 60)}m · "
            f"{item.workspace}[/dim]{mine}"
        )
    return 0


def announce_holder(session_id: str) -> None:
    """Say if another live terminal already holds this session.

    Not a refusal. Two terminals writing one transcript interleave their turns
    and the second save wins, which is worth knowing before it happens — but a
    registry is not entitled to overrule somebody who asked for a session by id.
    """
    holder = live_module.held_by(session_id)
    if holder is None or holder.mine:
        return
    render.note(
        f"{session_id} is also open in pid {holder.pid} — "
        "the last one to save wins"
    )
