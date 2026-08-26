"""Session persistence.

One JSON file per session under `$ANDROMEDA_HOME/sessions/`. Flat files rather
than a database because the useful operations here are "list the recent ones"
and "grep them" — and a file you can `cat` when something goes wrong is worth
more than an index you cannot.

Saved after every exchange, not at exit: the session that most needs to be
recoverable is the one that ended in a crash or a Ctrl-C.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as config_module

MAX_TITLE = 60
LIST_LIMIT = 20


def sessions_dir() -> Path:
    return config_module.home() / "sessions"


def _title_from(messages: list[dict[str, Any]]) -> str:
    """The first thing the user asked, trimmed.

    Deliberately not a model-generated title: that is a second inference call
    per session, billed, to name something the user will recognise from its
    opening line anyway.
    """
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            first = " ".join(message["content"].split())
            if first:
                return first[:MAX_TITLE] + ("…" if len(first) > MAX_TITLE else "")
    return "(empty)"


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provider: str = ""
    model: str = ""
    workspace: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Serialised checkpoint stack, so resuming restores the ability to rewind.
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    # Tokens this session has spent. Kept on the transcript rather than in the
    # index because the index is derived and may be rebuilt at any time — and a
    # token count is the one thing that cannot be recovered from a transcript.
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return _title_from(self.messages)

    @property
    def turns(self) -> int:
        return sum(1 for message in self.messages if message.get("role") == "user")

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "workspace": self.workspace,
            "messages": self.messages,
            "checkpoints": self.checkpoints,
            "usage": self.usage,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Session | None":
        session_id = str(raw.get("id") or "").strip()
        if not session_id:
            return None
        messages = raw.get("messages")
        return cls(
            id=session_id,
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            workspace=str(raw.get("workspace") or ""),
            messages=messages if isinstance(messages, list) else [],
            checkpoints=(
                raw["checkpoints"] if isinstance(raw.get("checkpoints"), list) else []
            ),
            usage=raw["usage"] if isinstance(raw.get("usage"), dict) else {},
        )

    def save(self) -> Path:
        self.updated_at = time.time()
        directory = sessions_dir()
        directory.mkdir(parents=True, exist_ok=True)

        # A transcript holds whatever the user pasted into it. Owner-only, and
        # write-then-rename so a crash mid-save cannot truncate the file.
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.to_json(), handle, indent=2)
            handle.write("\n")
        temporary.replace(self.path)
        return self.path


@dataclass
class Binding:
    """Which transcript a running conversation writes to.

    A level of indirection with one job: letting a surface switch sessions
    mid-run. The registry, the policy, the provider and the browser belong to
    the *terminal* and must survive the switch; only the transcript changes.

    Every switch saves what is on screen first. A session left half-written
    because somebody moved to another one is the transcript most likely to be
    the one they come back for.
    """

    record: Session

    def switch(self, target: Session, messages: list[dict[str, Any]]) -> Session:
        """Point at `target`, having saved `messages` into the current record."""
        if target.id == self.record.id:
            return self.record
        self.record.messages = messages
        self.record.save()
        self.record = target
        return target


def load(session_id: str) -> Session | None:
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return Session.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def recent(limit: int = LIST_LIMIT) -> list[Session]:
    directory = sessions_dir()
    if not directory.is_dir():
        return []

    loaded: list[Session] = []
    for path in directory.glob("*.json"):
        try:
            session = Session.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, ValueError):
            # One unreadable file must not hide every other session.
            continue
        if session is not None and session.messages:
            loaded.append(session)

    loaded.sort(key=lambda item: item.updated_at, reverse=True)
    return loaded[:limit]


def latest() -> Session | None:
    found = recent(limit=1)
    return found[0] if found else None


def resolve(prefix: str) -> Session | None:
    """Accept a unique id prefix, the way git accepts a short sha."""
    prefix = prefix.strip().lower()
    if not prefix:
        return None
    exact = load(prefix)
    if exact is not None:
        return exact
    matches = [session for session in recent(limit=1000) if session.id.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def search(query: str, limit: int = LIST_LIMIT) -> list[tuple[Session, str]]:
    """Find sessions whose transcript contains `query`, with the matching line."""
    needle = query.strip().lower()
    if not needle:
        return []

    results: list[tuple[Session, str]] = []
    for session in recent(limit=1000):
        for message in session.messages:
            # System messages carry the skills manifest and every standing
            # memory, so searching them makes each session match whatever the
            # agent happens to know. Only what was actually said is searched.
            if message.get("role") == "system":
                continue
            content = message.get("content")
            if not isinstance(content, str) or needle not in content.lower():
                continue
            line = next(
                (
                    row.strip()
                    for row in content.splitlines()
                    if needle in row.lower()
                ),
                content.strip(),
            )
            results.append((session, line[:160]))
            break
        if len(results) >= limit:
            break
    return results
