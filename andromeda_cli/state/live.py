"""Which sessions are open right now, in which terminals.

The transcripts record conversations that happened. This records the ones
happening — including a session that has been opened and not yet typed into,
which has no transcript at all.

It exists because two terminals resuming the same session silently interleave
their turns into one file, and the second one to save wins. Knowing a session
is held elsewhere is what lets the surface say so before that happens.

**A claim is only released when its owner is proved gone**, by pid *and*
process start time — the same rule the execution ledger uses, from the same
module, because two copies of that check is exactly the pair that drifts. A
heartbeat is not proof: a session that is genuinely working for twenty minutes
without writing must not be reaped, and neither must one whose pid has since
been handed to somebody's editor.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from andromeda_agent import liveness

from . import db as db_module


@dataclass
class LiveSession:
    session_id: str
    pid: int
    surface: str
    workspace: str
    opened_at: float
    heartbeat_at: float
    mine: bool = False

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.opened_at)


def _row(row: sqlite3.Row) -> LiveSession:
    return LiveSession(
        session_id=row["session_id"],
        pid=int(row["pid"]),
        surface=row["surface"],
        workspace=row["workspace"],
        opened_at=float(row["opened_at"] or 0),
        heartbeat_at=float(row["heartbeat_at"] or 0),
        mine=int(row["pid"]) == os.getpid(),
    )


def host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def claim(session_id: str, *, surface: str = "repl", workspace: str = "") -> bool:
    """Record that this process holds the session. True if it is now ours.

    False means somebody else's live process holds it. The caller decides what
    to do about that — this does not refuse, because refusing to open a session
    the user asked for, on the word of a registry, is worse than saying so.
    """
    now = time.time()
    with db_module.connect_quietly() as conn:
        if conn is None:
            return True
        holder = conn.execute(
            "SELECT * FROM live_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if holder is not None and int(holder["pid"]) != os.getpid():
            if liveness.owner_is_live(int(holder["pid"]), holder["pid_started"]):
                return False
        conn.execute(
            "INSERT INTO live_sessions(session_id, pid, pid_started, surface, "
            "workspace, opened_at, heartbeat_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "pid=excluded.pid, pid_started=excluded.pid_started, "
            "surface=excluded.surface, workspace=excluded.workspace, "
            "opened_at=excluded.opened_at, heartbeat_at=excluded.heartbeat_at",
            (
                session_id,
                os.getpid(),
                liveness.process_start_time(os.getpid()),
                surface,
                workspace,
                now,
                now,
            ),
        )
        return True


def beat(session_id: str) -> None:
    """Say the session is still open. Never used as proof of anything."""
    with db_module.connect_quietly() as conn:
        if conn is None:
            return
        conn.execute(
            "UPDATE live_sessions SET heartbeat_at = ? "
            "WHERE session_id = ? AND pid = ?",
            (time.time(), session_id, os.getpid()),
        )


def release(session_id: str) -> None:
    """Give up this process's claim. Another process's claim is left alone."""
    with db_module.connect_quietly() as conn:
        if conn is None:
            return
        conn.execute(
            "DELETE FROM live_sessions WHERE session_id = ? AND pid = ?",
            (session_id, os.getpid()),
        )


def reap() -> int:
    """Drop claims whose owner is provably gone. Returns how many."""
    with db_module.connect_quietly() as conn:
        if conn is None:
            return 0
        dropped = 0
        for row in conn.execute("SELECT * FROM live_sessions").fetchall():
            if liveness.owner_is_live(int(row["pid"]), row["pid_started"]):
                continue
            conn.execute(
                "DELETE FROM live_sessions WHERE session_id = ?",
                (row["session_id"],),
            )
            dropped += 1
        return dropped


def held_by(session_id: str) -> LiveSession | None:
    """The live holder of this session, if it has one that is really alive."""
    with db_module.connect_quietly() as conn:
        if conn is None:
            return None
        row = conn.execute(
            "SELECT * FROM live_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if not liveness.owner_is_live(int(row["pid"]), row["pid_started"]):
            return None
        return _row(row)


def all_live(prune: bool = True) -> list[LiveSession]:
    if prune:
        reap()
    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM live_sessions ORDER BY opened_at ASC"
        ).fetchall()
        return [_row(row) for row in rows]


def summary() -> dict[str, Any]:
    sessions = all_live()
    return {
        "count": len(sessions),
        "mine": sum(1 for item in sessions if item.mine),
        "host": host(),
        "sessions": sessions,
    }
