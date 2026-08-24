"""When a transcript will not load.

The harness this was ported from needs 1,700 lines here, because its database
is the only copy of a conversation and a damaged page means the conversation
is gone. Here the transcripts are flat JSON files and the database is derived,
so most of that problem does not exist: a broken index is repaired by deleting
it and reindexing.

What is left is the case files really do hit — a transcript truncated by a
machine that lost power or a disk that filled mid-write. JSON is all-or-nothing
to a parser, so one missing closing brace loses a whole conversation to
`json.loads` even though every earlier message is intact on disk.

So the salvage here is a scanner rather than a parser: walk the `messages`
array and keep every object that is complete, stop at the first that is not.

**Nothing is deleted, ever.** A file that cannot be read is moved to
`sessions/quarantine/` and the salvaged version written in its place, so a
recovery that guessed wrong is undoable by hand.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import sessions as sessions_store
from . import db as db_module
from . import index as index_module

QUARANTINE = "quarantine"

_FIELD = {
    "id": re.compile(r'"id"\s*:\s*"([^"]{1,64})"'),
    "model": re.compile(r'"model"\s*:\s*"([^"]*)"'),
    "provider": re.compile(r'"provider"\s*:\s*"([^"]*)"'),
    "workspace": re.compile(r'"workspace"\s*:\s*"([^"]*)"'),
    "created_at": re.compile(r'"created_at"\s*:\s*([0-9.]+)'),
    "updated_at": re.compile(r'"updated_at"\s*:\s*([0-9.]+)'),
}


@dataclass
class Damaged:
    path: Path
    reason: str
    salvageable: int = 0


@dataclass
class Report:
    database: str = ""
    integrity: str = "unknown"
    fts: bool = False
    trigram: bool = False
    sessions_on_disk: int = 0
    sessions_indexed: int = 0
    messages_indexed: int = 0
    stale: int = 0
    damaged: list[Damaged] = field(default_factory=list)
    live_claims: int = 0
    error: str = ""

    @property
    def healthy(self) -> bool:
        return (
            not self.error
            and self.integrity == "ok"
            and not self.damaged
            and self.stale == 0
        )


def quarantine_dir() -> Path:
    return sessions_store.sessions_dir() / QUARANTINE


# ---- salvage --------------------------------------------------------------


def _scan_objects(text: str, start: int) -> list[dict[str, Any]]:
    """Every complete JSON object in an array starting at `start`.

    String-aware, so a brace inside a pasted code block does not end an object
    early — which is the failure that makes a naive brace counter recover half
    a transcript and call it whole.
    """
    objects: list[dict[str, Any]] = []
    depth = 0
    in_string = False
    escaped = False
    begin = -1

    for position in range(start, len(text)):
        character = text[position]

        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
            continue
        if character == "{":
            if depth == 0:
                begin = position
            depth += 1
            continue
        if character == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                chunk = text[begin:position + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    # A complete-looking object that will not parse means the
                    # damage is inside it. Stop rather than skip: past this
                    # point the offsets cannot be trusted.
                    break
                if isinstance(parsed, dict):
                    objects.append(parsed)
                begin = -1
            elif depth < 0:
                break
            continue
        if character == "]" and depth == 0:
            break

    return objects


def salvage(path: Path) -> "sessions_store.Session | None":
    """Best effort reconstruction of one transcript. None if nothing survives."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return sessions_store.Session.from_json(parsed)

    marker = text.find('"messages"')
    if marker == -1:
        return None
    bracket = text.find("[", marker)
    if bracket == -1:
        return None

    messages = _scan_objects(text, bracket + 1)
    if not messages:
        return None

    values: dict[str, Any] = {}
    head = text[:marker] if marker > 0 else text
    for name, pattern in _FIELD.items():
        found = pattern.search(head) or pattern.search(text)
        if found:
            values[name] = found.group(1)

    return sessions_store.Session(
        id=str(values.get("id") or path.stem),
        created_at=float(values.get("created_at") or 0) or time.time(),
        updated_at=float(values.get("updated_at") or 0) or time.time(),
        provider=str(values.get("provider") or ""),
        model=str(values.get("model") or ""),
        workspace=str(values.get("workspace") or ""),
        messages=messages,
    )


def _reason(path: Path) -> str | None:
    """Why this file will not load, or None if it loads fine."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"malformed JSON at line {exc.lineno}"
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    if not isinstance(raw, dict):
        return "not an object"
    if sessions_store.Session.from_json(raw) is None:
        return "no session id"
    return None


# ---- checking -------------------------------------------------------------


def check() -> Report:
    report = Report(database=str(db_module.db_path()))
    directory = sessions_store.sessions_dir()

    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            report.sessions_on_disk += 1
            reason = _reason(path)
            if reason is None:
                continue
            recovered = salvage(path)
            report.damaged.append(
                Damaged(
                    path=path,
                    reason=reason,
                    salvageable=len(recovered.messages) if recovered else 0,
                )
            )

    try:
        with db_module.connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            report.integrity = str(row[0]) if row else "unknown"
            report.fts = db_module.has_fts(conn)
            report.trigram = db_module.has_trigram(conn)
            report.sessions_indexed = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()["n"]
            report.messages_indexed = conn.execute(
                "SELECT COUNT(*) AS n FROM messages"
            ).fetchone()["n"]
            report.live_claims = conn.execute(
                "SELECT COUNT(*) AS n FROM live_sessions"
            ).fetchone()["n"]
    except (sqlite3.Error, OSError) as exc:
        report.error = str(exc)
        return report

    report.stale = index_module.stale_count()
    return report


# ---- repairing ------------------------------------------------------------


@dataclass
class Repair:
    quarantined: list[Path] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    lost: list[Path] = field(default_factory=list)
    reindexed: dict[str, int] = field(default_factory=dict)
    applied: bool = False


def repair(apply: bool = False) -> Repair:
    """Quarantine what will not load, write back what can be salvaged.

    A dry run by default. Recovery that rewrites files the first time it is
    asked a question is recovery nobody runs twice.
    """
    outcome = Repair(applied=apply)
    report = check()

    for damaged in report.damaged:
        recovered = salvage(damaged.path)
        if recovered is None or not recovered.messages:
            outcome.lost.append(damaged.path)
            continue
        outcome.recovered.append(recovered.id)
        outcome.quarantined.append(damaged.path)
        if not apply:
            continue
        holding = quarantine_dir()
        holding.mkdir(parents=True, exist_ok=True)
        shutil.move(
            str(damaged.path),
            str(holding / f"{damaged.path.stem}.{int(time.time())}.json"),
        )
        recovered.save()

    if apply:
        # The index describes files that have just changed underneath it.
        outcome.reindexed = index_module.reindex(force=False)

    return outcome


def rebuild_index() -> dict[str, int]:
    """Throw the index away and build it again from the transcripts.

    The blunt repair, and the one that is always safe: the index holds no
    original data, so the worst case of deleting it is the time it takes to
    read every transcript once.
    """
    path = db_module.db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return index_module.reindex(force=True)
