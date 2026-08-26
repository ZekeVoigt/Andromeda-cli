"""One fire, claimed once.

A hosted runner is woken by an HTTP request, and that request will arrive more
than once. Not occasionally — by design: the caller cannot tell "the machine
never got it" from "the machine got it and died", so anything short of a
confirmed acceptance has to be retried, or a job silently stops firing the first
time a packet is lost. Retries are the feature. This module is what makes them
safe.

`executions.py` answers the same question for a local scheduler and its proof of
a dead owner is **pid plus process start time**. That proof does not survive a
machine boundary: a stopped machine's pid means nothing, and there is nobody to
ask. So the claim here is a **lease** — a holder and an expiry — and the four
outcomes are deliberately not symmetric:

  WON         no row for this fire. Run it.
  IN_FLIGHT   a live lease. A retry of something already running. Do nothing,
              and tell the caller we accepted it, so it stops retrying.
  SETTLED     already finished. Same answer: accepted, stop retrying.
  UNKNOWN     an expired lease that never settled. **Refuse.**

That last one is the whole reason this file is not four lines. An expired lease
means a machine took the fire and died somewhere in the middle. The side effects
may have run — the email may be sent, the file may be written, the webhook may
have posted — and nobody can know which. Reclaiming it would be a retry queue,
and a retry queue here is a machine that sends the same message twice at 3am
forever.

`executions.py` already states the rule this inherits: **`unknown` is not a
retry queue.** It says the side effects may or may not have run, which is the
only thing anybody can honestly know, and a person decides. The next
*scheduled* fire — a different `fire_at` — proceeds normally, so a job that hit
this does not stop; it skips one beat and says so.

## Two backends, one contract

`Fires` keeps the claim in SQLite, which is correct exactly when one process
owns one file: a runner that is a machine, or a laptop.

`RemoteFires` keeps it in the server, and exists because that assumption breaks
on a shared-pool host. Modal's Volumes are last-write-wins and their own
documentation says not to put SQLite on one; two containers claiming the same
fire from two copies of a database is precisely the double-send this module is
for. So the claim moves to the one place that is already transactional and
already knows when a job is due.

**The four outcomes are identical across both.** That is the contract — the
semantics are the thing, and only the storage moved. `serve.Runner` takes either
and cannot tell the difference, which is what makes the host a deployment
decision rather than a rewrite.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

# How long a holder has to finish before its claim is considered abandoned.
# Generous on purpose: the cost of a lease that is too short is a *second* run
# of work that is still going, which is the exact failure this module exists to
# prevent. The cost of one that is too long is a job that skips a beat.
DEFAULT_LEASE_SECONDS = 45 * 60

# Bounded like the execution ledger, and for the same reason: the interesting
# rows are the recent ones and the ones that never settled.
MAX_ROWS = 2000

_lock = threading.RLock()


class Outcome(str, Enum):
    """What happened when this process tried to claim a fire."""

    WON = "won"
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"
    UNKNOWN = "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class Fires:
    """Every fire this machine has been asked to run, and who held it."""

    def __init__(self, path: Path, holder: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Identifies this process. A machine id would not be enough: two
        # processes on one machine (a `cron serve` and a manual `cron run`) are
        # exactly the pair whose overlap this has to catch.
        self.holder = holder or f"{uuid.uuid4().hex[:12]}"
        with self._transaction() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS fires (
                     job_id TEXT NOT NULL,
                     fire_at TEXT NOT NULL,
                     holder TEXT NOT NULL,
                     claimed_at TEXT NOT NULL,
                     lease_expires_at TEXT NOT NULL,
                     settled_at TEXT,
                     ok INTEGER,
                     PRIMARY KEY (job_id, fire_at)
                   )"""
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # ---- claiming ---------------------------------------------------------

    def claim(
        self, job_id: str, fire_at: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Outcome:
        """Try to take this fire. One winner, ever, per `(job_id, fire_at)`.

        The insert and the read happen inside one `BEGIN IMMEDIATE`, so two
        requests arriving in the same millisecond cannot both see "no row" and
        both proceed. Doing the check and the write as two statements is the
        classic version of this bug and it only shows up under the load that
        matters.
        """
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT holder, lease_expires_at, settled_at FROM fires "
                "WHERE job_id = ? AND fire_at = ?",
                (job_id, fire_at),
            ).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO fires "
                    "(job_id, fire_at, holder, claimed_at, lease_expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        job_id,
                        fire_at,
                        self.holder,
                        _iso(now),
                        _iso(now.fromtimestamp(now.timestamp() + lease_seconds, timezone.utc)),
                    ),
                )
                self._prune(conn)
                return Outcome.WON

            if row["settled_at"]:
                return Outcome.SETTLED

            expires = datetime.fromisoformat(row["lease_expires_at"])
            if expires > now:
                return Outcome.IN_FLIGHT

            # An expired, unsettled lease. Deliberately NOT reclaimed — see the
            # module note. The row is left exactly as it is so `unresolved()`
            # keeps reporting it until a person looks.
            return Outcome.UNKNOWN

    def settle(self, job_id: str, fire_at: str, ok: bool) -> bool:
        """Mark a fire finished. Terminal, and written once.

        A second settle is refused rather than applied, matching the execution
        ledger's rule: a late-arriving process must not be able to overwrite
        what actually happened.
        """
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE fires SET settled_at = ?, ok = ? "
                "WHERE job_id = ? AND fire_at = ? AND settled_at IS NULL",
                (_iso(_now()), 1 if ok else 0, job_id, fire_at),
            ).rowcount
        return bool(changed)

    # ---- reading ----------------------------------------------------------

    def unresolved(self) -> list[dict[str, Any]]:
        """Fires whose lease ran out with nothing recorded.

        The list a person reads to decide what, if anything, happened. It is
        not a work queue and nothing consumes it automatically.
        """
        now = _iso(_now())
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM fires WHERE settled_at IS NULL "
                "AND lease_expires_at <= ? ORDER BY claimed_at DESC",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, job_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT * FROM fires WHERE job_id = ? "
                    "ORDER BY claimed_at DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fires ORDER BY claimed_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _prune(conn: sqlite3.Connection) -> None:
        """Settled rows only. An unresolved one is the whole point of the file
        and is never pruned, however old — it is the record of a run whose
        outcome nobody knows."""
        conn.execute(
            "DELETE FROM fires WHERE settled_at IS NOT NULL AND rowid NOT IN "
            "(SELECT rowid FROM fires WHERE settled_at IS NOT NULL "
            " ORDER BY claimed_at DESC LIMIT ?)",
            (MAX_ROWS,),
        )


class RemoteFires:
    """The claim, held by the server instead of by a file.

    Same four outcomes, same rules, no local state. Used wherever the runner is
    one of many interchangeable containers rather than one machine that owns a
    disk.

    **A failure to reach the server is not a claim.** It raises rather than
    guessing, and `serve.Runner` turns that into a retryable `503`. The
    alternative — assuming `WON` when the claim could not be checked — is how
    two containers run the same job, which is the entire failure this class
    exists to prevent. Failing closed here costs a delayed run; failing open
    costs a duplicated side effect.
    """

    def __init__(self, client: Any, user_id: str, holder: str = "") -> None:
        self.client = client
        self.user_id = user_id
        self.holder = holder or uuid.uuid4().hex[:12]

    def claim(
        self, job_id: str, fire_at: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Outcome:
        # `lease_seconds` is accepted and ignored: the lease is the server's to
        # set, because it is the only party that can see every container. A
        # signature that quietly diverged from the local one would be a seam
        # that only looks interchangeable.
        answer = self.client.claim_fire(self.user_id, job_id, fire_at, self.holder)
        try:
            return Outcome(str(answer))
        except ValueError:
            # An outcome this build does not recognise is not a licence to run.
            # A newer server that adds a fifth case must not cause an older
            # runner to treat it as a win.
            return Outcome.UNKNOWN

    def settle(self, job_id: str, fire_at: str, ok: bool) -> bool:
        return bool(self.client.settle_fire(self.user_id, job_id, fire_at, ok))

    def unresolved(self) -> list[dict[str, Any]]:
        return list(self.client.unresolved_fires(self.user_id))

    def recent(self, job_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        # Deliberately not implemented against the server. The list a person
        # reads is `andromeda cron runs`, which reads outcomes rather than
        # claims; a second history that could disagree with it would be a
        # second thing to keep true.
        return []
