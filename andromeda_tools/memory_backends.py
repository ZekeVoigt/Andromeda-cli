"""Where memories live, and how candidates for recall are found.

Two backends ship. They differ in exactly two things — storage and candidate
retrieval — and deliberately in nothing else:

  ``json``    one file, `memory/memories.json`. Readable with `cat`, editable
              by hand, trivially portable. Recall scans it.
  ``sqlite``  rows in the state index, with an FTS5 table over their content.
              Recall asks the index for candidates instead of reading every
              memory. Worth it past a few thousand; pointless below that.

**Scoring is not part of the backend.** The tool description promises that
`minScore` means "this fraction of the query's meaningful terms appear in the
memory", and a backend that quietly swapped in bm25 would keep the parameter
name while changing what a given number does — so a threshold tuned on one
install would mean something else on another. The sqlite backend uses FTS to
find *candidates* fast and then scores them with the same function the json
backend uses, so the number keeps its meaning on both.

**One backend is active at a time.** Two stores that both answer
`memory_search` is two sets of facts that drift, and the agent then contradicts
itself depending on which one it read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .memory import Memory

DEFAULT_BACKEND = "json"


class MemoryBackend(ABC):
    """Storage and candidate retrieval for one memory store."""

    name = "abstract"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- availability ----------------------------------------------------

    def available(self) -> bool:
        """Whether this backend can be used on this machine, without I/O."""
        return True

    def unavailable_reason(self) -> str:
        """Why not, in words a person can act on. Empty when available."""
        return ""

    # ---- storage ---------------------------------------------------------

    @property
    @abstractmethod
    def file(self) -> Path:
        """Where the memories physically are. Named in diagnostics and in the
        agent's own context block, so "where is what you remember" has an
        answer that is true for the backend in use."""

    @abstractmethod
    def load(self) -> list["Memory"]:
        """Every memory. A corrupt store reads as empty and is left on disk."""

    @abstractmethod
    def replace(self, memories: Iterable["Memory"]) -> None:
        """Write the whole set. The operations layer computes it; this stores
        it. Whole-set replacement keeps supersession and trimming atomic."""

    # ---- retrieval -------------------------------------------------------

    def candidates(self, query: str) -> list["Memory"]:
        """Memories worth scoring for this query.

        The default is "all of them", which is correct and is what the json
        backend does. A backend with an index narrows it — and may only ever
        narrow: returning a memory that does not match is a wasted comparison,
        while omitting one that does is a fact the agent has and cannot recall.
        """
        return self.load()


# ---- json -----------------------------------------------------------------


class JsonBackend(MemoryBackend):
    """One file, written atomically, owner-readable only."""

    name = "json"

    @property
    def file(self) -> Path:
        return self.root / "memories.json"

    def load(self) -> list["Memory"]:
        from .memory import Memory

        if not self.file.exists():
            return []
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt store reads as empty rather than crashing every turn.
            # The file is left on disk so it can be recovered by hand.
            return []
        if not isinstance(raw, list):
            return []
        parsed = [Memory.from_json(item) for item in raw if isinstance(item, dict)]
        return [memory for memory in parsed if memory is not None]

    def replace(self, memories: Iterable["Memory"]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([memory.to_json() for memory in memories], indent=2)

        # Write-then-rename, so an interrupted save cannot truncate the store.
        temporary = self.file.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        temporary.replace(self.file)


# ---- sqlite ---------------------------------------------------------------


class SqliteBackend(MemoryBackend):
    """Rows in the state index, with FTS5 over their content."""

    name = "sqlite"

    def _connect(self):
        from andromeda_cli.state import db as db_module

        return db_module.connect()

    def available(self) -> bool:
        try:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'memories' LIMIT 1"
                ).fetchone() is not None
        except (sqlite3.Error, OSError, ImportError):
            return False

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        return (
            "the state index could not be opened — run `andromeda sessions doctor`"
        )

    @property
    def file(self) -> Path:
        from andromeda_cli.state import db as db_module

        return db_module.db_path()

    @staticmethod
    def _row(row: sqlite3.Row) -> "Memory":
        from .memory import Memory

        return Memory(
            id=row["id"],
            content=row["content"],
            scope=row["scope"],
            category=row["category"],
            tags=[tag for tag in str(row["tags"] or "").split("\x1f") if tag],
            path=row["path"],
            created_at=float(row["created_at"] or 0),
        )

    def load(self) -> list["Memory"]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, content, scope, category, tags, path, created_at "
                    "FROM memories ORDER BY created_at ASC"
                ).fetchall()
        except (sqlite3.Error, OSError):
            return []
        return [self._row(row) for row in rows]

    def replace(self, memories: Iterable["Memory"]) -> None:
        rows = [
            (
                memory.id,
                memory.content,
                memory.scope,
                memory.category,
                # Unit separator rather than a comma: a tag may legitimately
                # contain one, and splitting on a character the value can hold
                # is how a tag silently becomes two.
                "\x1f".join(memory.tags),
                memory.path,
                memory.created_at,
            )
            for memory in memories
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM memories")
            conn.executemany(
                "INSERT INTO memories(id, content, scope, category, tags, path, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                rows,
            )

    @staticmethod
    def _any_of(match: str) -> str:
        """Turn a sanitized query into an OR over its terms.

        FTS5 ANDs adjacent terms implicitly, and that is **stricter than the
        scorer**: `minScore` defaults to 0.3, so a memory covering one term of
        three is a legitimate recall — and an ANDed MATCH would never offer it
        as a candidate. Narrowing that drops a real match is worse than not
        narrowing at all, so the candidate query is deliberately the loosest
        one that still uses the index, and the scorer does the deciding.

        Quoted phrases are kept whole; a bare boolean operator is dropped,
        because `retry OR OR budget` is a syntax error.
        """
        tokens = re.findall(r'"[^"]*"|\S+', match)
        terms = [
            token
            for token in tokens
            if token.upper() not in {"AND", "OR", "NOT"}
        ]
        return " OR ".join(terms)

    def candidates(self, query: str) -> list["Memory"]:
        from andromeda_cli.state import db as db_module
        from andromeda_cli.state.queries import sanitize

        match = self._any_of(sanitize(query))
        if not match:
            return self.load()
        try:
            with self._connect() as conn:
                if not db_module.table_exists(conn, "memories_fts"):
                    return self.load()
                rows = conn.execute(
                    "SELECT m.id, m.content, m.scope, m.category, m.tags, m.path, "
                    "m.created_at FROM memories_fts "
                    "JOIN memories m ON m.rowid = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT 200",
                    (match,),
                ).fetchall()
        except (sqlite3.Error, OSError):
            # Narrowing failed, so do not narrow. A search that silently sees
            # fewer memories is worse than a slow one.
            return self.load()
        return [self._row(row) for row in rows]


BACKENDS: dict[str, type[MemoryBackend]] = {
    JsonBackend.name: JsonBackend,
    SqliteBackend.name: SqliteBackend,
}


def build(name: str, root: Path) -> tuple[MemoryBackend, str]:
    """The backend for this name, and a note if it is not the one asked for.

    An unknown name falls back to the built-in rather than refusing to start —
    the same rule the scheduler uses for `cron_provider`, and for the same
    reason: a typo in a setting must not take away the agent's memory.
    """
    wanted = (name or DEFAULT_BACKEND).strip().lower()
    factory = BACKENDS.get(wanted) or _plugin_backends().get(wanted)
    if factory is None:
        return (
            JsonBackend(root),
            f"unknown memory backend {name!r}; using {DEFAULT_BACKEND}",
        )
    backend = factory(root)
    if not backend.available():
        return (
            JsonBackend(root),
            f"{wanted} backend unavailable ({backend.unavailable_reason()});"
            f" using {DEFAULT_BACKEND}",
        )
    return backend, ""


def _plugin_backends() -> dict[str, "Callable[[Path], MemoryBackend]"]:
    """Backends a plugin registered, or nothing.

    Consulted only after the built-ins, so a plugin cannot shadow `json` or
    `sqlite` by claiming their names — the fallback path above still ends at
    `JsonBackend`, which means a broken plugin backend costs you the setting
    and never the memories.
    """
    try:
        from andromeda_agent import plugins as plugins_module
    except ImportError:  # pragma: no cover - half-installed package
        return {}
    return plugins_module.memory_backends()
