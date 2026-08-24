"""The derived state index.

**The JSON transcripts under `sessions/` stay the source of truth.** This
SQLite file is an *index* over them: it can be deleted at any moment and
rebuilt from the files without losing a single message. That inverts the
arrangement in the harness this was ported from, where the database is the
only copy — and it is why recovery here is a reindex rather than a page-level
salvage operation.

What the index buys is the thing flat files cannot do: full-text search across
every session at zero model cost, and listing that stays fast when there are
thousands of them.

Three rules hold it together:

- **Migrations are keyed by name, never by number.** A numbered ledger breaks
  the day two branches both add "migration 4", or somebody renumbers a shipped
  one — every existing install then either re-runs a migration or skips one.
  Names cannot collide by accident and cannot be renumbered.
- **FTS5 is probed, not assumed.** Python's bundled SQLite usually has it and
  sometimes does not, and the trigram tokenizer needs 3.34+. Where either is
  missing the index still builds and search falls back to LIKE, slower and
  still correct.
- **Nothing here may break a turn.** Every write from the conversation path is
  best-effort: a failure to index is a failure to search later, not a failure
  to answer now.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .. import config as config_module

# Bumped only for the record — the ledger below is what actually decides what
# runs. Useful in `sessions doctor` output and in a bug report.
SCHEMA_VERSION = 1

_lock = threading.RLock()


def db_path() -> Path:
    return config_module.home() / "state.db"


# ---- schema ---------------------------------------------------------------

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    workspace     TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    turns         INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    -- What the file looked like when it was indexed. Cheap staleness check:
    -- a transcript whose size and mtime are unchanged cannot have new turns.
    source_mtime  REAL NOT NULL DEFAULT 0,
    source_size   INTEGER NOT NULL DEFAULT 0,
    -- Fingerprints of the first and last messages that went into the index.
    -- An append leaves both matching; a rewind changes the tail and a
    -- compaction changes the head, which is how the indexer knows to rebuild
    -- the session rather than append to it. Both are needed: checking only
    -- the tail would let a compaction that collapsed the head while leaving
    -- the last message intact slip through as an append, leaving stale rows
    -- for messages that no longer exist.
    head_hash     TEXT NOT NULL DEFAULT '',
    tail_hash     TEXT NOT NULL DEFAULT '',
    indexed_at    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    -- Offset in the transcript at the time it was indexed. Display and
    -- ordering only: an archived row and a live row can share one, because
    -- compaction restarts the transcript. `id` is the stable anchor.
    position   INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    tool_name  TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL DEFAULT 0,
    -- Compacted out of the live conversation but kept here. This is what lets
    -- a summary honestly say the turns it replaced are still readable: the
    -- model can search them even though they are no longer in its context.
    -- Rebuilding the index for a session leaves these rows alone, because
    -- there is nothing on disk left to rebuild them from.
    archived   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS live_sessions (
    session_id   TEXT PRIMARY KEY,
    pid          INTEGER NOT NULL,
    pid_started  INTEGER,
    surface      TEXT NOT NULL DEFAULT '',
    workspace    TEXT NOT NULL DEFAULT '',
    opened_at    REAL NOT NULL DEFAULT 0,
    heartbeat_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, position);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_archived ON messages(session_id, archived);
"""

# External-content FTS over `messages`, kept in step by triggers rather than by
# the indexer remembering to write both. `content=''` is deliberately NOT used:
# the snippet() call needs the text, and a session transcript is small enough
# that storing it twice is cheaper than re-reading JSON to render a hit.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name)
    VALUES (new.id, new.content, new.tool_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name)
    VALUES ('delete', old.id, old.content, old.tool_name);
END;

-- `UPDATE OF` rather than a bare UPDATE trigger: the index only cares about
-- the indexed columns, and firing FTS I/O for a bookkeeping write is how a
-- large index gets slow for no benefit.
CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE OF content, tool_name ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name)
    VALUES ('delete', old.id, old.content, old.tool_name);
    INSERT INTO messages_fts(rowid, content, tool_name)
    VALUES (new.id, new.content, new.tool_name);
END;
"""

# A second index, tokenized as overlapping 3-character sequences. The default
# unicode61 tokenizer splits CJK into single characters, so phrase matching in
# Chinese, Japanese or Korean does not work at all against `messages_fts`; the
# trigram tokenizer makes substring search work natively for any script. It is
# the more expensive index, so it covers only what the default one handles
# badly — and it is optional, because it needs SQLite 3.34+.
TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_trigram USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_trigram(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_trigram(messages_trigram, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_update
AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_trigram(messages_trigram, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_trigram(rowid, content) VALUES (new.id, new.content);
END;
"""

# Memories, indexed alongside sessions so the sqlite memory backend can rank
# recall with the same FTS machinery instead of a second lexical scorer.
MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'episode',
    category   TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


class Migration:
    """One named, idempotent step.

    `optional` marks a step that is allowed to fail on a SQLite build that
    cannot run it — the trigram index is the whole reason this exists. An
    optional step that fails is *not* recorded as applied, so it is retried on
    the next open; a machine that gets a newer SQLite picks it up without an
    explicit repair command.
    """

    def __init__(
        self,
        name: str,
        apply: str | Callable[[sqlite3.Connection], None],
        *,
        optional: bool = False,
    ) -> None:
        self.name = name
        self.apply = apply
        self.optional = optional

    def run(self, conn: sqlite3.Connection) -> None:
        if callable(self.apply):
            self.apply(conn)
        else:
            conn.executescript(self.apply)


def _add_head_hash(conn: sqlite3.Connection) -> None:
    """Add `head_hash` to a store created before it existed.

    Named, not numbered, so an install that already ran `base-tables` with the
    column present skips it and one that ran the older DDL picks it up — the
    two cases a numbered ledger cannot tell apart.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "head_hash" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN head_hash TEXT NOT NULL DEFAULT ''")
    # Every stored fingerprint pair is now incomplete, so force one rebuild
    # pass rather than trusting an append against a hash nothing wrote.
    conn.execute("UPDATE sessions SET head_hash = ''")


def _add_archived(conn: sqlite3.Connection) -> None:
    """Add `archived` to a store created before compaction archiving existed."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "archived" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_archived "
        "ON messages(session_id, archived)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration("base-tables", BASE_SCHEMA),
    Migration("session-head-hash", _add_head_hash),
    Migration("message-archived", _add_archived),
    Migration("messages-fts", FTS_SCHEMA, optional=True),
    Migration("messages-trigram", TRIGRAM_SCHEMA, optional=True),
    Migration("memories", MEMORY_SCHEMA, optional=True),
)


class StateError(RuntimeError):
    pass


# ---- connections ----------------------------------------------------------


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL so a `sessions search` in one terminal never blocks the turn being
    # written in another. NORMAL rather than FULL: this file is derived, and
    # the cost of losing the last write to a power cut is one reindex.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A configured, migrated connection, committed on a clean exit."""
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(str(target), timeout=10)
        try:
            _configure(conn)
            migrate(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def connect_quietly(path: Path | None = None) -> Iterator[sqlite3.Connection | None]:
    """`connect`, but yields None instead of raising.

    For the paths that run inside a turn. A machine whose home directory has
    gone read-only should still answer the question it was asked.
    """
    try:
        with connect(path) as conn:
            yield conn
    except (sqlite3.Error, OSError):
        yield None


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every unapplied migration. Returns the names that ran."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    applied = {
        row["name"] for row in conn.execute("SELECT name FROM schema_migrations")
    }
    ran: list[str] = []
    for migration in MIGRATIONS:
        if migration.name in applied:
            continue
        try:
            migration.run(conn)
        except sqlite3.Error:
            if migration.optional:
                # Left unrecorded on purpose, so it is retried against a
                # SQLite that can run it rather than skipped forever.
                continue
            raise
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(name, applied_at) "
            "VALUES (?, strftime('%s','now'))",
            (migration.name,),
        )
        ran.append(migration.name)
    return ran


# ---- capability probes ----------------------------------------------------


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def has_fts(conn: sqlite3.Connection) -> bool:
    return table_exists(conn, "messages_fts")


def has_trigram(conn: sqlite3.Connection) -> bool:
    return table_exists(conn, "messages_trigram")


def capabilities() -> dict[str, Any]:
    """What this machine's SQLite can actually do, for `sessions doctor`."""
    report: dict[str, Any] = {
        "path": str(db_path()),
        "sqlite": sqlite3.sqlite_version,
        "schema_version": SCHEMA_VERSION,
        "fts5": False,
        "trigram": False,
        "applied": [],
        "error": "",
    }
    try:
        with connect() as conn:
            report["fts5"] = has_fts(conn)
            report["trigram"] = has_trigram(conn)
            report["applied"] = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM schema_migrations ORDER BY name"
                )
            ]
    except (sqlite3.Error, OSError) as exc:
        report["error"] = str(exc)
    return report
