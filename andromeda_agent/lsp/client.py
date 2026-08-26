"""One language server, running as a child process.

The shape is the MCP transport's — a subprocess, a reader thread, a pending
table keyed by request id — with three differences that are all forced by the
protocol rather than chosen:

**Frames, not lines.** `Content-Length` headers, so the reader is a framer
rather than a `for line in stream`.

**The interesting traffic goes the wrong way.** In MCP every message is an
answer to something we asked. Here the payload — `publishDiagnostics` — is an
unsolicited notification, and the reader's real job is to collect those rather
than to match replies.

**Requests arrive from the server.** `workspace/configuration`,
`client/registerCapability` and the rest are requests *to us*, and a server
that is not answered blocks forever waiting. Everything unrecognised is
answered with `null`, which every server accepts.

The whole client is best-effort. A language server is a convenience: if it does
not start, does not answer, or dies mid-session, the edit still happened and
the tool still returns. Nothing in here may raise into a turn.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import protocol
from .servers import Server, language_id

logger = logging.getLogger("andromeda.lsp")

# How long to wait for `initialize`. A cold `rust-analyzer` on a large tree can
# take a while to answer, and giving up early leaves a process running with
# nobody talking to it.
INIT_TIMEOUT = 30.0

# How long to wait for diagnostics after a change. Past this, the honest report
# is "no diagnostics yet" rather than a stall: nobody will accept a CLI that
# pauses ten seconds after every edit.
DIAGNOSTIC_TIMEOUT = 6.0

# How long a server may take to shut down politely before it is killed.
SHUTDOWN_TIMEOUT = 3.0

# Stderr kept for the failure report. A server that will not start says why
# there and nowhere else.
STDERR_LINES = 40


def to_uri(path: Path | str) -> str:
    """A `file://` URI. Servers key everything on these, not on paths."""
    return Path(path).resolve().as_uri()


def from_uri(uri: str) -> str:
    """The path inside a `file://` URI, or the string unchanged.

    Unchanged rather than raising: a server that reports a diagnostic against
    something that is not a file URI is telling us something, and dropping it
    to preserve a type is the wrong trade.
    """
    if not uri.startswith("file://"):
        return uri
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    return unquote(parsed.path)


class Client:
    """A running language server and the documents it has been told about."""

    def __init__(self, server: Server, binary: str, root: Path) -> None:
        self.server = server
        self.binary = binary
        self.root = root
        self.started_at = 0.0

        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        # Bumped every time the server publishes anything. A waiter watches the
        # counter rather than the dictionary, because "published an empty list"
        # is a real answer — it means the file is clean — and is indistinguishable
        # from "has not published" if you only look at the contents.
        self._pushes: dict[str, int] = {}
        # The document version each push was about, when the server said. LSP
        # makes this optional, so `None` means "the server did not say" and the
        # push counter is the only ordering available.
        self._published_version: dict[str, int | None] = {}
        self._events = threading.Condition(self._lock)
        self._stderr: list[str] = []
        self._versions: dict[str, int] = {}
        self._open: set[str] = set()
        self.failure = ""
        # True until this server has published anything at all. A cold server
        # is still building its index and answers the first file slowly; every
        # file after that is fast, so the two are given different budgets.
        self.cold = True

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Spawn the server and complete the handshake. `False` on any failure."""
        try:
            self._process = subprocess.Popen(
                [self.binary, *self.server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.root),
                env=os.environ.copy(),
                # A language server on a big tree emits large frames; an
                # unbuffered pipe turns each one into thousands of syscalls.
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            self.failure = f"could not start {self.binary}: {exc}"
            return False

        self.started_at = time.time()
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            self._request("initialize", _initialize_params(self.root), INIT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a server that will not talk
            self.failure = f"{self.server.id} did not complete initialize: {exc}"
            self.stop()
            return False

        self._notify("initialized", {})
        # Sent unconditionally, because a server that asked for configuration
        # during `initialize` is already waiting for it and a server that did
        # not ignores it.
        self._notify("workspace/didChangeConfiguration", {"settings": self.server.settings})
        return True

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        """Shut the server down. Never raises; always leaves no process behind."""
        process, self._process = self._process, None
        if process is None:
            return
        # Written to the local handle, deliberately. `_write` goes through
        # `self._process`, which has just been cleared — routing the farewell
        # through it silently raises, nothing is ever sent, and every server
        # is killed after the full timeout instead of exiting cleanly. That
        # costs three seconds on every session end and leaves servers no
        # chance to flush their index.
        try:
            stdin = process.stdin
            if stdin is not None:
                stdin.write(protocol.encode(protocol.request(self._take_id(), "shutdown")))
                stdin.write(protocol.encode(protocol.notification("exit")))
                stdin.flush()
        except Exception:  # noqa: BLE001 - a dead server cannot be asked politely
            pass
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.debug("%s would not die", self.server.id)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    # -- documents ----------------------------------------------------------

    def sync(self, path: Path, text: str) -> None:
        """Tell the server this file's current contents.

        `didOpen` the first time and `didChange` after, because a server that
        receives two `didOpen`s for one URI is entitled to treat the second as
        an error, and several do.

        Full-document syncs only. Incremental sync would save bytes on a pipe
        that is not the bottleneck, and it would mean maintaining a second
        model of the document that has to agree with the server's — the place
        every LSP client's hardest bugs live.
        """
        uri = to_uri(path)
        version = self._versions.get(uri, 0) + 1
        self._versions[uri] = version
        if uri in self._open:
            self._notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
            return
        self._open.add(uri)
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id(path),
                    "version": version,
                    "text": text,
                }
            },
        )

    def close(self, path: Path) -> None:
        uri = to_uri(path)
        if uri not in self._open:
            return
        self._open.discard(uri)
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def mark_saved(self, path: Path, text: str) -> None:
        """Tell the server the file is on disk.

        Some servers only run their expensive checks on save, so an edit that
        is only ever `didChange`d gets syntax errors and nothing else.
        """
        uri = to_uri(path)
        if uri not in self._open:
            return
        self._notify("textDocument/didSave", {"textDocument": {"uri": uri}, "text": text})

    # -- diagnostics --------------------------------------------------------

    def wait_for_diagnostics(
        self, path: Path, *, after: int = -1, timeout: float = DIAGNOSTIC_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Whatever the server says about this file's current contents.

        Two orderings, and the difference is the whole correctness of the delta.

        When the server reports a document version — most do, once
        `versionSupport` is declared — the wait is for a push *about the
        version we last sent*. A cold server publishes its first diagnostics
        seconds after the file was already changed, and that push is about the
        previous contents; accepting it would report the old file's problems as
        the new file's, or more often report nothing and call a broken edit
        clean.

        When the server does not report a version, `after` — a push count taken
        before the change was sent — is the only ordering available, and a late
        push for the old contents can still slip through. That is why
        `versionSupport` is declared.

        Returns what is known when the deadline passes rather than raising.
        A slow server costs a report, never a turn.
        """
        uri = to_uri(path)
        wanted = self._versions.get(uri)
        deadline = time.time() + timeout
        with self._events:
            while True:
                published = self._published_version.get(uri, "missing")
                pushes = self._pushes.get(uri, 0)
                if published is not None and published != "missing":
                    if wanted is None or published >= wanted:
                        break
                elif pushes > after:
                    break
                remaining = deadline - time.time()
                if remaining <= 0 or not self.alive:
                    break
                self._events.wait(min(remaining, 0.25))
            return list(self._diagnostics.get(uri, []))

    def settled_for(self, path: Path) -> bool:
        """Whether the server has answered about this file's current version.

        The question `Snapshot.settled` needs: a wait that ended on the deadline
        rather than on an answer leaves the diagnostics list empty, and an empty
        list is otherwise indistinguishable from "this file is clean".
        """
        uri = to_uri(path)
        with self._lock:
            published = self._published_version.get(uri, "missing")
            if published == "missing":
                return False
            wanted = self._versions.get(uri)
            if published is None:
                # No version reported. A push happened for this file and that is
                # the strongest statement available.
                return True
            return wanted is None or published >= wanted

    def push_count(self, path: Path) -> int:
        with self._lock:
            return self._pushes.get(to_uri(path), 0)

    def diagnostics(self, path: Path) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._diagnostics.get(to_uri(path), []))

    # -- transport ----------------------------------------------------------

    def _take_id(self) -> int:
        with self._lock:
            identifier = self._next_id
            self._next_id += 1
            return identifier

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError(f"{self.server.id} is not running")
        process.stdin.write(protocol.encode(message))
        process.stdin.flush()

    def _notify(self, method: str, params: Any = None) -> None:
        try:
            self._write(protocol.notification(method, params))
        except Exception as exc:  # noqa: BLE001 - a notification is fire-and-forget
            logger.debug("%s: notification %s failed: %s", self.server.id, method, exc)

    def _request(self, method: str, params: Any, timeout: float) -> Any:
        identifier = self._take_id()
        self._write(protocol.request(identifier, method, params))
        deadline = time.time() + timeout
        with self._events:
            while identifier not in self._replies:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
                if not self.alive:
                    detail = "\n".join(self._stderr[-5:])
                    raise RuntimeError(
                        f"{self.server.id} exited"
                        + (f": {detail}" if detail else "")
                    )
                self._events.wait(min(remaining, 0.25))
            message = self._replies.pop(identifier)
        if "error" in message:
            error = message["error"] or {}
            raise protocol.RequestFailed(
                int(error.get("code", 0)), str(error.get("message", "")), error.get("data")
            )
        return message.get("result")

    def _read_loop(self) -> None:
        process = self._process
        stream = process.stdout if process else None
        if stream is None:
            return
        while True:
            try:
                message = protocol.read(stream)
            except protocol.ProtocolError as exc:
                # A broken frame means the stream is out of step and cannot be
                # resynchronised — there is no framing marker to seek to. Stop
                # reading; the client is dead and `alive` will say so.
                logger.debug("%s: %s", self.server.id, exc)
                return
            except (OSError, ValueError):
                return
            if message is None:
                with self._events:
                    self._events.notify_all()
                return
            try:
                self._handle(message)
            except Exception:  # noqa: BLE001 - one bad message must not stop the reader
                logger.debug("%s: could not handle a message", self.server.id, exc_info=True)

    def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")

        if method is None:
            identifier = message.get("id")
            if isinstance(identifier, int):
                with self._events:
                    self._replies[identifier] = message
                    self._events.notify_all()
            return

        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = str(params.get("uri", ""))
            items = params.get("diagnostics")
            version = params.get("version")
            with self._events:
                self._diagnostics[uri] = list(items) if isinstance(items, list) else []
                self._pushes[uri] = self._pushes.get(uri, 0) + 1
                self._published_version[uri] = (
                    int(version) if isinstance(version, int) else None
                )
                self.cold = False
                self._events.notify_all()
            return

        # A request from the server. Answering `null` is valid for every one of
        # these and is what an unconfigured client should say; not answering
        # leaves the server blocked, which looks like a hang in the editor and
        # like a timeout here.
        identifier = message.get("id")
        if identifier is not None:
            result: Any = None
            if method == "workspace/configuration":
                items = (message.get("params") or {}).get("items") or []
                result = [self.server.settings for _ in items]
            try:
                self._write(protocol.response(identifier, result))
            except Exception:  # noqa: BLE001
                pass

    def _read_stderr(self) -> None:
        process = self._process
        stream = process.stderr if process else None
        if stream is None:
            return
        for line in stream:
            try:
                text = line.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:  # noqa: BLE001
                continue
            self._stderr.append(text)
            del self._stderr[:-STDERR_LINES]

    @property
    def stderr(self) -> str:
        return "\n".join(self._stderr[-5:])


def _initialize_params(root: Path) -> dict[str, Any]:
    """The handshake.

    Deliberately modest. Every capability declared here is one a server may
    then use, and this client consumes exactly one thing — diagnostics. Claiming
    to support workspace edits or code actions would invite traffic nothing
    reads, and some servers do noticeably more work for a client that asks.
    """
    uri = root.as_uri()
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "andromeda"},
        "rootUri": uri,
        "rootPath": str(root),
        "workspaceFolders": [{"uri": uri, "name": root.name}],
        "capabilities": {
            "workspace": {
                "configuration": True,
                "didChangeConfiguration": {"dynamicRegistration": False},
                "workspaceFolders": True,
            },
            "textDocument": {
                "synchronization": {
                    "dynamicRegistration": False,
                    "didSave": True,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                },
                # `versionSupport` is the one that matters. Without it a
                # server may omit the document version from its diagnostics,
                # and then a push that arrived late for the *previous*
                # contents is indistinguishable from the answer about the
                # edit — which reads as "your change introduced nothing".
                "publishDiagnostics": {
                    "relatedInformation": False,
                    "versionSupport": True,
                    "tagSupport": {"valueSet": [1, 2]},
                },
            },
        },
        "initializationOptions": {},
    }


__all__ = ["Client", "DIAGNOSTIC_TIMEOUT", "INIT_TIMEOUT", "from_uri", "to_uri"]
