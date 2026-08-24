"""A durable record of every attempt to run a job.

The run history in `cron.json` answers "what did this job produce". This
answers a different and harder question: **what was in flight when the machine
went down.**

That matters because a job attempt has side effects. If the scheduler is killed
between "started the job" and "wrote the result", the run history shows
nothing — and "it never ran" and "it ran, sent the email, and died before
recording it" look identical. Guessing between them is how a scheduler either
re-sends an email or silently drops a run.

So this is a ledger, not a retry queue, and two rules make it trustworthy:

- **Terminal states are immutable.** `completed`, `failed` and `unknown` are
  written once. An update that would rewrite one is refused, so a late-arriving
  process cannot overwrite what actually happened.
- **An interrupted attempt becomes `unknown` only when its exact owner is
  proved gone.** Not "the pid is missing" — pids are reused, and marking a live
  attempt abandoned is how you get two copies. The proof is pid *plus process
  start time*: same pid, different start time, different process.

SQLite rather than JSON, unlike everything else in this layer. This is the one
file written concurrently by processes that are not coordinating — a daemon, a
manual `cron run`, a recovery sweep — and it is append-mostly. A JSON file
rewritten whole on every attempt loses the race it is supposed to record.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import liveness

# Kept bounded. The interesting rows are the recent ones and the ones that
# never reached a terminal state; a year of successful ticks is noise.
MAX_TERMINAL = 1000

STATUSES = ("claimed", "running", "completed", "failed", "unknown")
TERMINAL = ("completed", "failed", "unknown")

# Identifies this process's own rows, so a recovery sweep never touches an
# attempt it is itself running.
_PROCESS_ID = uuid.uuid4().hex
_lock = threading.RLock()

ABANDONED = (
    "The scheduler restarted after this attempt's owner exited without writing "
    "a result. Whether its side effects ran is unknown."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Liveness lives in one module, imported here rather than defined twice. The
# ledger and the live-session registry must agree on what "that process is
# gone" means, and two copies of a pid-plus-start-time check is exactly the
# pair that drifts.
_pid_exists = liveness.pid_exists
_process_start_time = liveness.process_start_time
_owner_is_live = liveness.owner_is_live


class Ledger:
    """Every attempt, on this machine, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            self._schema(conn)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                # WAL so a reader (`cron executions`) never blocks the daemon
                # mid-tick; FULL because the whole point of this file is
                # surviving the crash that made it interesting.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS executions (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 source TEXT NOT NULL,
                 process_id TEXT NOT NULL,
                 pid INTEGER NOT NULL,
                 process_started_at INTEGER,
                 status TEXT NOT NULL CHECK(status IN
                   ('claimed','running','completed','failed','unknown')),
                 claimed_at TEXT NOT NULL,
                 started_at TEXT,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_claimed "
            "ON executions(job_id, claimed_at DESC, id DESC)"
        )

    # ---- writing ----------------------------------------------------------

    def claim(self, job_id: str, source: str = "schedule") -> str:
        """Record an attempt *before* anything with a side effect happens.

        Written first, deliberately. A row created after the work would be a
        row that never exists for exactly the attempts worth recording.
        """
        execution_id = uuid.uuid4().hex[:16]
        pid = os.getpid()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO executions
                   (id, job_id, source, process_id, pid, process_started_at,
                    status, claimed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
                (
                    execution_id,
                    str(job_id),
                    str(source),
                    _PROCESS_ID,
                    pid,
                    _process_start_time(pid),
                    _now(),
                ),
            )
        return execution_id

    def running(self, execution_id: str) -> bool:
        """Claimed → running, exactly once."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE executions SET status='running', started_at=? "
                "WHERE id=? AND status='claimed'",
                (_now(), execution_id),
            )
            return cursor.rowcount == 1

    def finish(self, execution_id: str, ok: bool, error: str = "") -> bool:
        """Write the terminal result. Refused if one is already written."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE executions SET status=?, finished_at=?, error=? "
                "WHERE id=? AND status IN ('claimed','running')",
                (
                    "completed" if ok else "failed",
                    _now(),
                    None if ok else (error or "unknown failure")[:2000],
                    execution_id,
                ),
            )
            if cursor.rowcount == 1:
                self._prune(conn)
            return cursor.rowcount == 1

    def recover(self) -> int:
        """Mark provably abandoned attempts `unknown`. Schedules no retries.

        `unknown` is the honest state and the useful one: it says the side
        effects may or may not have happened, which is the only thing anybody
        can actually know. Retrying would risk doing it twice; assuming success
        would risk never doing it at all. A person reads the row and decides.
        """
        changed = 0
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, process_id, pid, process_started_at FROM executions "
                "WHERE status IN ('claimed','running')"
            ).fetchall()
            for row in rows:
                if row["process_id"] == _PROCESS_ID:
                    continue  # ours, and still running
                if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                    continue
                cursor = conn.execute(
                    "UPDATE executions SET status='unknown', finished_at=?, error=? "
                    "WHERE id=? AND status IN ('claimed','running')",
                    (_now(), ABANDONED, row["id"]),
                )
                changed += cursor.rowcount
            if changed:
                self._prune(conn)
        return changed

    @staticmethod
    def _prune(conn: sqlite3.Connection) -> None:
        conn.execute(
            """DELETE FROM executions WHERE id IN (
                 SELECT id FROM executions WHERE status IN ('completed','failed','unknown')
                 ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
               )""",
            (MAX_TERMINAL,),
        )

    # ---- reading ----------------------------------------------------------

    def recent(self, job_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM executions"
        params: tuple = ()
        if job_id:
            query += " WHERE job_id=?"
            params = (job_id,)
        query += " ORDER BY claimed_at DESC, id DESC LIMIT ?"
        with self._transaction() as conn:
            rows = conn.execute(query, (*params, limit)).fetchall()
        return [dict(row) for row in rows]

    def unresolved(self) -> list[dict[str, Any]]:
        """Attempts that never reached a terminal state, including `unknown`.

        The list worth showing a person: everything here either is running now
        or ended in a way nobody recorded.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM executions WHERE status IN ('claimed','running','unknown') "
                "ORDER BY claimed_at DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]
