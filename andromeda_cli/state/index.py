"""Keeping the index in step with the transcripts.

Two entry points, and the difference between them matters:

`index_session` runs inside a live turn, after each exchange is persisted. It
must be cheap and it must never raise — so it appends the new tail where it
can prove the earlier messages are unchanged, and only rebuilds that one
session when it cannot.

`reindex` runs from the command line and rebuilds whatever is stale, including
dropping rows for transcripts that have been deleted.

**System messages are not indexed.** They carry the skills manifest and every
standing memory, so indexing them makes every session match anything the agent
happens to know — the same reason the flat-file search skipped them.

**Archived rows are never deleted by a rebuild.** When compaction folds turns
away they leave the transcript for good, so the index is the only remaining
copy — that is exactly what lets the summary replacing them say, truthfully,
that they are still readable. Everything else here can be reconstructed from
the files; those cannot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .. import sessions as sessions_store
from . import db as db_module

INDEXED_ROLES = ("user", "assistant", "tool")


def _text(content: Any) -> str:
    """Flatten a message body to searchable text.

    Content is usually a string, but a multimodal or reasoning model can send a
    list of blocks. Every text-like block is concatenated and the rest ignored
    — an image is not something FTS can index.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return str(content)


def _tool_names(message: dict[str, Any]) -> str:
    """The tool names attached to a message, so "which session ran terminal"
    is answerable without reading every transcript."""
    direct = message.get("tool_name") or message.get("name") or ""
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        names = []
        for call in calls:
            if isinstance(call, dict):
                function = call.get("function")
                if isinstance(function, dict) and function.get("name"):
                    names.append(str(function["name"]))
                elif call.get("name"):
                    names.append(str(call["name"]))
        return " ".join(names)
    return ""


def fingerprint(message: dict[str, Any]) -> str:
    """A stable stamp for one message, used to prove nothing earlier moved."""
    digest = hashlib.sha256()
    digest.update(str(message.get("role") or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(_text(message.get("content")).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(_tool_names(message).encode("utf-8"))
    return digest.hexdigest()[:32]


def _rows(
    session_id: str, messages: list[dict[str, Any]], start: int
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for position in range(start, len(messages)):
        message = messages[position]
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in INDEXED_ROLES:
            continue
        body = _text(message.get("content"))
        tools = _tool_names(message)
        if not body.strip() and not tools.strip():
            continue
        rows.append(
            (
                session_id,
                position,
                role,
                body,
                tools,
                float(message.get("timestamp") or 0.0),
            )
        )
    return rows


def _session_row(
    session: "sessions_store.Session", head_hash: str, tail_hash: str
) -> tuple[Any, ...]:
    try:
        stat = session.path.stat()
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = 0.0, 0
    return (
        session.id,
        session.created_at,
        session.updated_at,
        session.provider,
        session.model,
        session.workspace,
        session.title,
        session.turns,
        len(session.messages),
        mtime,
        size,
        head_hash,
        tail_hash,
        time.time(),
    )


_UPSERT = """
INSERT INTO sessions(id, created_at, updated_at, provider, model, workspace,
                     title, turns, message_count, source_mtime, source_size,
                     head_hash, tail_hash, indexed_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
    created_at=excluded.created_at,
    updated_at=excluded.updated_at,
    provider=excluded.provider,
    model=excluded.model,
    workspace=excluded.workspace,
    title=excluded.title,
    turns=excluded.turns,
    message_count=excluded.message_count,
    source_mtime=excluded.source_mtime,
    source_size=excluded.source_size,
    head_hash=excluded.head_hash,
    tail_hash=excluded.tail_hash,
    indexed_at=excluded.indexed_at
"""

_INSERT_MESSAGES = (
    "INSERT INTO messages(session_id, position, role, content, tool_name, at) "
    "VALUES (?,?,?,?,?,?)"
)


def write_session(conn: sqlite3.Connection, session: "sessions_store.Session") -> str:
    """Index one session, appending where it is provably an append.

    Returns "appended", "rebuilt" or "unchanged" — the command-line reindex
    reports the split, and it is the fastest way to see that the incremental
    path has stopped working.
    """
    messages = session.messages
    row = conn.execute(
        "SELECT message_count, head_hash, tail_hash, source_mtime, source_size "
        "FROM sessions WHERE id = ?",
        (session.id,),
    ).fetchone()

    indexed_count = int(row["message_count"]) if row else 0
    known_head = str(row["head_hash"]) if row else ""
    known_tail = str(row["tail_hash"]) if row else ""

    # Both ends, not just the tail. A compaction that collapses the head while
    # leaving the last message intact would otherwise look like an append, and
    # the index would keep rows for messages that no longer exist.
    appendable = (
        row is not None
        and 0 < indexed_count <= len(messages)
        and known_head
        and known_tail
        and fingerprint(messages[0]) == known_head
        and fingerprint(messages[indexed_count - 1]) == known_tail
    )

    if appendable and indexed_count == len(messages):
        # Nothing new. Still refresh the session row: a title or workspace can
        # change without the message list doing so.
        conn.execute(_UPSERT, _session_row(session, known_head, known_tail))
        return "unchanged"

    if not appendable:
        # A rewind rewrote the tail, or compaction rewrote the head. Either
        # way the stored positions no longer describe this transcript, and
        # patching them is how an index quietly starts lying.
        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND archived = 0",
            (session.id,),
        )
        start = 0
    else:
        start = indexed_count

    rows = _rows(session.id, messages, start)
    if rows:
        conn.executemany(_INSERT_MESSAGES, rows)

    head = fingerprint(messages[0]) if messages else ""
    tail = fingerprint(messages[-1]) if messages else ""
    conn.execute(_UPSERT, _session_row(session, head, tail))
    return "appended" if appendable else "rebuilt"


def index_session(session: "sessions_store.Session") -> str:
    """Best-effort indexing from inside a live turn."""
    try:
        with db_module.connect() as conn:
            return write_session(conn, session)
    except (sqlite3.Error, OSError):
        # Searchability is a convenience; answering the question is not.
        return "failed"


def archive_range(session_id: str, first: int, last: int) -> int:
    """Mark the live rows in `[first, last]` as compacted-out. Returns how many.

    Called by the conversation just before it replaces those turns with a
    summary, and only after the pre-compaction transcript has been indexed —
    otherwise there would be nothing to archive. Live positions are transcript
    offsets at that moment, which is what makes the range meaningful.

    Idempotent: a row already archived is not matched again, so a retried
    compaction cannot double-count or resurrect anything.
    """
    with db_module.connect_quietly() as conn:
        if conn is None:
            return 0
        cursor = conn.execute(
            "UPDATE messages SET archived = 1 "
            "WHERE session_id = ? AND archived = 0 AND position BETWEEN ? AND ?",
            (session_id, first, last),
        )
        return cursor.rowcount or 0


def archived_count(session_id: str = "") -> int:
    with db_module.connect_quietly() as conn:
        if conn is None:
            return 0
        if session_id:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE session_id = ? AND archived = 1",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE archived = 1"
            ).fetchone()
        return int(row["n"])


def forget_session(session_id: str) -> None:
    with db_module.connect_quietly() as conn:
        if conn is None:
            return
        # Archived rows included: the session itself is being deleted, and
        # keeping the turns it compacted away would leave search returning a
        # conversation that no longer exists.
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def _is_stale(row: sqlite3.Row | None, path: Path) -> bool:
    if row is None:
        return True
    try:
        stat = path.stat()
    except OSError:
        return True
    return (
        abs(float(row["source_mtime"]) - stat.st_mtime) > 1e-6
        or int(row["source_size"]) != stat.st_size
    )


def reindex(force: bool = False, paths: Iterable[Path] | None = None) -> dict[str, int]:
    """Bring the whole index up to date. Returns a per-outcome count."""
    directory = sessions_store.sessions_dir()
    files = (
        sorted(paths)
        if paths is not None
        else (sorted(directory.glob("*.json")) if directory.is_dir() else [])
    )

    counts = {
        "scanned": 0,
        "rebuilt": 0,
        "appended": 0,
        "unchanged": 0,
        "unreadable": 0,
        "dropped": 0,
    }

    with db_module.connect() as conn:
        seen: set[str] = set()
        for path in files:
            counts["scanned"] += 1
            row = conn.execute(
                "SELECT source_mtime, source_size FROM sessions WHERE id = ?",
                (path.stem,),
            ).fetchone()
            if not force and not _is_stale(row, path):
                seen.add(path.stem)
                counts["unchanged"] += 1
                continue
            try:
                session = sessions_store.Session.from_json(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError, ValueError):
                # One damaged transcript must not stop the other thousand
                # being searchable. `sessions doctor` names them.
                counts["unreadable"] += 1
                continue
            if session is None:
                counts["unreadable"] += 1
                continue
            if force:
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ? AND archived = 0",
                    (session.id,),
                )
                conn.execute("DELETE FROM sessions WHERE id = ?", (session.id,))
            seen.add(session.id)
            counts[write_session(conn, session)] += 1

        # Transcripts deleted off disk leave rows behind that would otherwise
        # be returned by search forever.
        if paths is None:
            for row in conn.execute("SELECT id FROM sessions").fetchall():
                if row["id"] in seen:
                    continue
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ?", (row["id"],)
                )
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
                counts["dropped"] += 1

    return counts


def stale_count() -> int:
    """How many transcripts on disk the index has not caught up with."""
    directory = sessions_store.sessions_dir()
    if not directory.is_dir():
        return 0
    with db_module.connect_quietly() as conn:
        if conn is None:
            return 0
        stale = 0
        for path in directory.glob("*.json"):
            row = conn.execute(
                "SELECT source_mtime, source_size FROM sessions WHERE id = ?",
                (path.stem,),
            ).fetchone()
            if _is_stale(row, path):
                stale += 1
        return stale
