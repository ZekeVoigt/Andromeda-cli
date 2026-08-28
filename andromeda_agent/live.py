"""What a scheduled run is doing, while it is doing it.

The gap this closes: a job fires in the daemon, works for three minutes, and
the only thing the person ever sees is an OS notification with 240 characters
of summary in it. The run was a full agent turn — it read files, called tools,
changed its mind — and every bit of that was discarded because the process that
produced it had no screen and the process with a screen was not told.

So a run writes a journal, and any surface that cares tails it.

**Append-only JSONL, one file per day.** Not a socket: the writer is a daemon
that may outlive every reader, may run when no reader exists, and must never
block on one. A file that nobody is tailing costs a write; a socket that nobody
is reading is a decision about whether to block. Not a database either — this
is the one thing in the layer that is written a hundred times per run and read
by tailing, which is what a log is for.

**Keyed by the session it attaches to.** A record carries `session`, so a
surface showing one conversation can filter to the runs that belong in it
without reading the job store. That is the same `attach_to` the durable
transcript copy uses; the journal is the live view of what will land there.

**Bounded by construction.** Files are named by date and reaped after
`KEEP_DAYS`. A journal that grows forever is a journal somebody deletes by
hand, and then the feature is off and nobody knows.

Best-effort throughout, exactly like `delivery`. A run whose journal could not
be written is a run that happened; losing the work because the log failed would
invert what matters.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

#: How many days of journals to keep. A week is long enough to answer "what did
#: it do overnight" and short enough that the directory stays small.
KEEP_DAYS = 7

#: Text deltas are coalesced to at most one record per this many seconds. A
#: provider streams tokens; a journal that recorded each one would write
#: thousands of lines for one paragraph and the tail would spend its budget on
#: JSON parsing rather than on painting.
FLUSH_SECONDS = 0.25

#: A single record's text is bounded. A tool that returns a 4000-line file is
#: legitimate; putting it in a journal that a UI tails is not.
MAX_TEXT = 4000


def journal_dir(home: Path) -> Path:
    return Path(home) / "cron" / "live"


def _path(home: Path, when: float | None = None) -> Path:
    stamp = datetime.fromtimestamp(when or time.time()).strftime("%Y%m%d")
    return journal_dir(home) / f"{stamp}.jsonl"


def reap(home: Path, keep_days: int = KEEP_DAYS) -> int:
    """Delete journals older than the window. Returns how many went."""
    directory = journal_dir(home)
    cutoff = date.today() - timedelta(days=keep_days)
    removed = 0
    try:
        entries = list(directory.glob("*.jsonl"))
    except OSError:
        return 0
    for entry in entries:
        try:
            stamp = datetime.strptime(entry.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def append(home: Path, record: dict[str, Any]) -> bool:
    """Write one record. Never raises.

    Opened, written and closed per record rather than held open, because the
    writer is a long-lived daemon and the file it should write to changes at
    midnight. A held handle would keep appending to yesterday.

    `O_APPEND` on a line shorter than the pipe buffer is atomic on every
    platform this runs on, which is what lets a daemon and a manual `cron run`
    write the same file without coordinating.
    """
    record = {"at": round(time.time(), 3), **record}
    try:
        path = _path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        return True
    except (OSError, TypeError, ValueError):
        return False


class Writer:
    """The journal for one run.

    Holds the ids every record needs so callers pass only what varies, and
    coalesces text deltas so a streamed answer becomes a handful of records
    rather than one per token.

    `flush()` must be called at the end of a run — the tail of the answer is
    sitting in the buffer until it is. `finished()` calls it, which is the
    path every real caller takes.
    """

    def __init__(self, home: Path, *, job_id: str, job_name: str, session: str, where: str = "local") -> None:
        self.home = Path(home)
        self.job_id = job_id
        self.job_name = job_name
        self.session = session or ""
        self.where = where
        self._buffer = ""
        self._flushed_at = 0.0

    # -- records ------------------------------------------------------------

    def _emit(self, kind: str, **fields: Any) -> None:
        append(
            self.home,
            {
                "kind": kind,
                "job": self.job_id,
                "name": self.job_name,
                "session": self.session,
                "where": self.where,
                **fields,
            },
        )

    def started(self, reason: str = "") -> None:
        self._emit("run.started", reason=reason)

    def text(self, chunk: str) -> None:
        """A fragment of the answer. Coalesced; see `FLUSH_SECONDS`."""
        if not chunk:
            return
        self._buffer += chunk
        now = time.monotonic()
        if now - self._flushed_at >= FLUSH_SECONDS or len(self._buffer) >= MAX_TEXT:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        chunk, self._buffer = self._buffer[:MAX_TEXT], self._buffer[MAX_TEXT:]
        self._flushed_at = time.monotonic()
        self._emit("text", text=chunk)
        # A single oversized delta can leave more than one record's worth
        # behind. Drain rather than recurse — a 40KB tool echo is not deep
        # recursion territory.
        while self._buffer:
            chunk, self._buffer = self._buffer[:MAX_TEXT], self._buffer[MAX_TEXT:]
            self._emit("text", text=chunk)

    def tool(self, summary: str, tier: str) -> None:
        self.flush()
        self._emit("tool", summary=summary[:400], tier=tier)

    def tool_result(self, detail: str, ok: bool) -> None:
        self._emit("tool.result", detail=(detail or "")[:400], ok=bool(ok))

    def note(self, text: str) -> None:
        self.flush()
        self._emit("note", text=(text or "")[:400])

    def finished(self, status: str, summary: str = "", error: str = "") -> None:
        self.flush()
        self._emit(
            "run.finished",
            status=status,
            summary=(summary or "")[:MAX_TEXT],
            error=(error or "")[:400],
        )


class Tail:
    """Reads new records since the last call. One per surface.

    Holds a byte offset rather than re-reading the file, so a 5MB journal
    costs one `seek` per poll instead of one parse of everything that ever
    happened. The offset is per *path*: at midnight the writer moves to a new
    file and the reader follows it, starting at zero rather than at yesterday's
    length — which is the bug this class exists to not have.

    `since` is the horizon. A surface that opens at 3pm has no business
    replaying the 6am run into a live transcript: that run is already in the
    durable session copy, and showing it again would double it.
    """

    def __init__(self, home: Path, *, since: float | None = None, session: str = "") -> None:
        self.home = Path(home)
        self.session = session
        #: Whether the caller wants history. Omitting `since` means "from now
        #: on", and that case skips to the end of today's file rather than
        #: parsing a day of records it will then discard. Passing `since`
        #: explicitly — including `since=0` — means the opposite, and reading
        #: from the top is the only way to honour it.
        #:
        #: Conflating the two is what made the first `poll()` of a freshly
        #: written journal return nothing: it seeked to EOF *and* applied a
        #: `since` the caller had set to 0 precisely to avoid that.
        self.from_start = since is not None
        # Floored to the same millisecond the writer rounds to. `append` stores
        # `round(time.time(), 3)`, which can round *down* below an unrounded
        # `since` captured a few microseconds earlier — and the record that
        # loses that race is the `run.started` at the top of a run, the one
        # thing a live view most needs. Quantising both ends removes the race
        # rather than papering over it with an epsilon.
        raw = time.time() if since is None else since
        self.since = math.floor(raw * 1000) / 1000
        self._path: Path | None = None
        self._offset = 0
        #: The inode the offset belongs to. A file replaced under the reader
        #: keeps its name and its size may well exceed the old offset, so size
        #: alone cannot detect it — and seeking into the middle of a new file
        #: reads a torn record and then silently skips everything before it.
        self._inode: int | None = None

        # "What was already there" is decided **now**, not at the first poll.
        #
        # Deciding it lazily meant the answer depended on when the first tick
        # happened: a surface opened at 00:04, before any job had run that day,
        # created no file to measure — and then the first poll, arriving after
        # the day's first run had written one, treated that run as history and
        # skipped it. The run that most needed showing was the one guaranteed
        # to be dropped.
        if not self.from_start:
            path = _path(self.home)
            try:
                info = path.stat()
                self._path = path
                self._offset = info.st_size
                self._inode = info.st_ino
            except OSError:
                # No file yet. Leaving `_path` unset means the first poll treats
                # it as new and reads from the top, which is what "everything
                # since I started" means when nothing had been written yet.
                pass

    def _current(self) -> Path:
        return _path(self.home)

    def poll(self) -> list[dict[str, Any]]:
        """Every record written since the last poll, oldest first.

        Never raises. A journal that cannot be read is a surface without live
        run rows, which is where the product was before this existed — a
        strictly worse experience, not a broken one.
        """
        path = self._current()
        if path != self._path:
            # A new day, or the first poll. On the first poll of a surface that
            # wants no history, skip what is already there; otherwise start at
            # zero — which is also right for a genuinely new file, since it is
            # empty.
            # A new day. `__init__` already handled "what was there when I
            # started", so a path change here is always a genuinely new file
            # and always starts at zero.
            self._path = path
            self._offset = 0
            self._inode = None

        try:
            if not path.exists():
                return []
            info = path.stat()
            size = info.st_size
            if self._inode is not None and info.st_ino != self._inode:
                # A different file wearing the same name — a reap and a fresh
                # write, or a hand edit through a tool that replaces rather
                # than appends. The offset described the old one.
                self._offset = 0
            elif size < self._offset:
                # Truncated in place. Re-read from the top rather than seeking
                # past the end.
                self._offset = 0
            self._inode = info.st_ino
            if size == self._offset:
                return []
            with open(path, "rb") as handle:
                # The offset is only meaningful if the byte before it is still
                # a record boundary. Truncate-then-rewrite keeps the inode and
                # can leave the file *longer* than the old offset, so neither
                # of the checks above fires and the reader would seek into the
                # middle of a record it has never seen. One byte answers it.
                if self._offset > 0:
                    handle.seek(self._offset - 1)
                    if handle.read(1) != b"\n":
                        self._offset = 0
                handle.seek(self._offset)
                raw = handle.read()
        except OSError:
            return []

        # Only whole records are consumed. The writer appends while this reads,
        # so the tail of `raw` is routinely half a line; stopping at the last
        # newline leaves it for the next poll and keeps `_offset` permanently
        # on a record boundary — which is what makes the continuity check above
        # mean anything.
        cut = raw.rfind(b"\n")
        if cut < 0:
            return []
        self._offset += cut + 1
        body = raw[: cut + 1].decode("utf-8", errors="replace")

        found: list[dict[str, Any]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # Every line here ended in a newline, so this is not a torn
                # write — it is a line that is genuinely not JSON, which means
                # something other than `append` wrote to the file. Skipped
                # rather than fatal: one bad line must not stop the run rows
                # after it from painting.
                continue
            if not isinstance(record, dict):
                continue
            if record.get("at", 0) < self.since:
                continue
            if self.session and record.get("session") != self.session:
                continue
            found.append(record)
        return found


def read_run(home: Path, job_id: str, *, day: float | None = None) -> Iterator[dict[str, Any]]:
    """Every record for one job on one day. For `cron logs --live`."""
    path = _path(home, day)
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in body.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("job") == job_id:
            yield record
