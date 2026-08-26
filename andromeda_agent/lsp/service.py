"""The seam: one object the loop calls twice per edit, and everything else hidden.

`before(path)` takes a snapshot; `after(path, snapshot)` returns the block to
append to the tool's result. Between those two calls it starts a language
server if one is needed and not already running, opens the file, waits briefly
for diagnostics, and subtracts what was already wrong.

Everything here is bounded and best-effort, in that order:

**Bounded.** A server is started at most once per (root, server). At most
`MAX_SERVERS` run at a time — a monorepo touching six languages must not end
up with six indexers competing for the machine the user is working on. Waiting
for diagnostics has a deadline measured in seconds, because the alternative is
a CLI that pauses after every edit.

**Best-effort.** Nothing in this module may raise into a turn. A server that
does not exist, does not start, does not answer or dies produces no block and
no error. The edit already happened; a missing report is a missing convenience,
and a traceback in the middle of a turn is a bug.

The first-touch problem is why `before` exists. Diagnostics are only useful as
a delta, and a delta needs a baseline. On the first edit of a file there is no
baseline unless one is taken deliberately — so `before` opens the file with its
*pre-edit* contents and lets the server settle. That costs a few seconds once
per file, and it buys the report on the edit that matters most: the first one.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import report, servers
from .client import DIAGNOSTIC_TIMEOUT, Client

logger = logging.getLogger("andromeda.lsp")

# Concurrent language servers. Each one is a real indexer with a real memory
# footprint; four is enough for a polyglot repository and few enough that
# nobody notices them.
MAX_SERVERS = 4

# A file bigger than this is not worth a round trip — a generated bundle or a
# vendored blob, where the diagnostics are somebody else's and there are
# thousands of them.
MAX_FILE_BYTES = 2 * 1024 * 1024

# How long to let a settled server answer for a file it has not seen. Short:
# this is paid on the first edit of every file, and a warm server answers in
# well under a second.
BASELINE_TIMEOUT = 5.0

# How long to let a server that has never published anything settle. Paid once
# per server per session, and worth paying: a language server that is still
# building its index answers the first file with silence, and a baseline of
# "nothing was wrong" turns the file's existing problems into the model's.
COLD_BASELINE_TIMEOUT = 20.0


@dataclass
class Snapshot:
    """What a file looked like, and what was wrong with it, before an edit.

    Two flags rather than one, because "the baseline is empty" and "there is no
    baseline" lead to opposite reports and conflating them is the bug this
    layer is easiest to get wrong in.

    `settled` — the server actually answered about these exact contents. Only
    then is a delta meaningful.

    `existed` — the file was there before the edit. A file this edit *created*
    has a genuinely empty baseline, so everything now wrong with it is new; a
    file that existed but whose server did not answer in time has an *unknown*
    baseline, and reporting its pre-existing problems as the model's own sends
    it off to fix code it never touched.
    """

    path: Path
    text: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    settled: bool = False
    existed: bool = False


class Service:
    """The language servers this session is using, and what they have said."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        enabled: bool = True,
        severities: frozenset[int] | None = None,
        max_servers: int = MAX_SERVERS,
        baseline_timeout: float = BASELINE_TIMEOUT,
        cold_timeout: float = COLD_BASELINE_TIMEOUT,
        diagnostic_timeout: float = DIAGNOSTIC_TIMEOUT,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.enabled = enabled
        self.severities = severities or report.DEFAULT_SEVERITIES
        self.max_servers = max_servers
        # Held as attributes rather than read from the module constants at the
        # call site: a default argument binds once at import, so patching the
        # constant changes nothing and a test that thinks it shortened a wait
        # is quietly measuring the real one.
        self.baseline_timeout = baseline_timeout
        self.cold_timeout = cold_timeout
        self.diagnostic_timeout = diagnostic_timeout
        self._clients: dict[tuple[str, str], Client] = {}
        self._failed: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- the two calls the loop makes ---------------------------------------

    def before(self, path: Path | str) -> Snapshot | None:
        """Baseline this file, or `None` if nothing here will report on it.

        `None` is the fast path and the common one: no server installed, an
        extension nobody handles, a file too large to be worth it. The caller
        skips `after` entirely, so an ordinary session pays one dictionary
        lookup per edit.
        """
        if not self.enabled:
            return None
        target = Path(path)
        client = self._client_for(target)
        if client is None:
            return None

        text = _read(target)
        if text is None:
            # A file that does not exist yet. Its baseline is genuinely empty,
            # so the edit that creates it gets everything reported.
            return Snapshot(path=target, text="", settled=True, existed=False)

        try:
            # `after` is the push count taken *before* the sync, so the wait is
            # for something newer. Passing anything lower satisfies the
            # fallback immediately and returns an empty baseline the moment the
            # file is opened — which then reads as "nothing was wrong here".
            before = client.push_count(target)
            client.sync(target, text)
            diagnostics = client.wait_for_diagnostics(
                target,
                after=before,
                timeout=self.cold_timeout if client.cold else self.baseline_timeout,
            )
            return Snapshot(
                path=target,
                text=text,
                diagnostics=diagnostics,
                settled=client.settled_for(target),
                existed=True,
            )
        except Exception:  # noqa: BLE001 - a baseline is a convenience
            logger.debug("could not baseline %s", target, exc_info=True)
            return Snapshot(path=target, text=text, settled=False, existed=True)

    def after(self, snapshot: Snapshot | None) -> str:
        """The block to append to the edit's result, or `""`.

        Reads the file from disk rather than being handed the new text: the
        tool may have written something different from what it was asked to
        write — a formatter hook, a partial patch — and the language server has
        to see what is actually there.
        """
        if snapshot is None or not self.enabled:
            return ""
        target = snapshot.path
        client = self._client_for(target)
        if client is None:
            return ""

        text = _read(target)
        if text is None:
            return ""

        try:
            before_pushes = client.push_count(target)
            client.sync(target, text)
            client.mark_saved(target, text)
            current = client.wait_for_diagnostics(
                target, after=before_pushes, timeout=self.diagnostic_timeout
            )
        except Exception:  # noqa: BLE001
            logger.debug("could not read diagnostics for %s", target, exc_info=True)
            return ""

        if snapshot.settled and snapshot.existed:
            introduced = report.new_diagnostics(
                snapshot.diagnostics,
                current,
                before=snapshot.text,
                after=text,
            )
        elif snapshot.settled:
            # A file this edit created. Its baseline really was empty, so
            # everything now wrong with it is new.
            introduced = list(current)
        else:
            # The file existed and the server never answered about what it
            # looked like. Reporting now would attribute somebody else's
            # problems to this edit and send the model off to fix code it never
            # touched — worse than saying nothing, because the next edit to
            # this file will have a baseline and will report properly.
            return ""

        try:
            relative = str(target.resolve().relative_to(self.workspace))
        except (ValueError, OSError):
            relative = str(target)
        return report.block(relative, introduced, severities=self.severities)

    # -- servers -------------------------------------------------------------

    def _client_for(self, path: Path) -> Client | None:
        """A running client for this file, starting one if that is cheap enough."""
        candidates = servers.for_file(path)
        if not candidates:
            return None
        for server in candidates:
            binary = servers.find_binary(server, self.workspace)
            if binary is None:
                continue
            root = servers.project_root(path, server, self.workspace)
            key = (server.id, str(root))
            with self._lock:
                existing = self._clients.get(key)
                if existing is not None:
                    if existing.alive:
                        return existing
                    # A server that died is not restarted inside the same
                    # session. Something is wrong with it, and a restart loop
                    # around a crashing indexer is worse than no diagnostics.
                    self._failed.setdefault(server.id, existing.failure or "it exited")
                    self._clients.pop(key, None)
                    continue
                if server.id in self._failed:
                    continue
                if len(self._clients) >= self.max_servers:
                    return None
                client = Client(server, binary, root)
                # Registered before `start`, so a second thread reaching this
                # for the same key waits on the lock rather than spawning a
                # second copy of the same indexer.
                self._clients[key] = client
            if client.start():
                return client
            with self._lock:
                self._clients.pop(key, None)
                self._failed[server.id] = client.failure or "it would not start"
        return None

    # -- reporting and shutdown ---------------------------------------------

    def status(self) -> dict[str, Any]:
        """What is running, what failed, and what this machine could use."""
        with self._lock:
            running = [
                {
                    "server": client.server.id,
                    "label": client.server.label,
                    "root": str(client.root),
                    "binary": client.binary,
                    "alive": client.alive,
                    "uptime": round(time.time() - client.started_at, 1)
                    if client.started_at
                    else 0.0,
                }
                for client in self._clients.values()
            ]
            failed = dict(self._failed)
        return {
            "enabled": self.enabled,
            "workspace": str(self.workspace),
            "running": running,
            "failed": failed,
            "severities": sorted(self.severities),
        }

    def stop(self) -> None:
        """Shut every server down. Safe to call twice; never raises."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("could not stop %s", client.server.id, exc_info=True)


def _read(path: Path) -> str | None:
    """A file's text, or `None` if it is missing, huge, or not text."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


__all__ = ["BASELINE_TIMEOUT", "MAX_FILE_BYTES", "MAX_SERVERS", "Service", "Snapshot"]
