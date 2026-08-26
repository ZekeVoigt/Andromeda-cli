#!/usr/bin/env python3
"""A language server that exists so the client can be tested without one installed.

Speaks enough LSP to be indistinguishable from the real thing at the layer
under test: `Content-Length` framing, `initialize`/`initialized`, document
synchronisation, and `publishDiagnostics` with a document version.

Its diagnostics are the file's own content: every line containing `BAD` becomes
an error, and every line containing `MEH` becomes a warning. That makes a test
able to say exactly what the server will report without depending on any real
language's grammar.

Behaviour is steered by environment variables so one script covers the awkward
cases:

  FAKE_LSP_NO_VERSION=1   omit the document version from every push, which is
                          what a server without `versionSupport` does
  FAKE_LSP_SILENT=1       never publish anything, so the client's deadline
                          rather than its parser decides the answer
  FAKE_LSP_DELAY=<secs>   wait this long before each push
  FAKE_LSP_DIE_ON_INIT=1  exit during the handshake
  FAKE_LSP_ASK_CONFIG=1   send a `workspace/configuration` request and refuse
                          to publish until it is answered
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import unquote, urlparse

DOCUMENTS: dict[str, tuple[str, int]] = {}
CONFIGURED = os.environ.get("FAKE_LSP_ASK_CONFIG") != "1"


def write(message: dict) -> None:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    sys.stdout.buffer.flush()


def read() -> dict | None:
    length = -1
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    if length < 0:
        return None
    body = b""
    while len(body) < length:
        chunk = sys.stdin.buffer.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def publish(uri: str) -> None:
    if os.environ.get("FAKE_LSP_SILENT") == "1" or not CONFIGURED:
        return
    delay = float(os.environ.get("FAKE_LSP_DELAY") or 0)
    if delay:
        time.sleep(delay)
    text, version = DOCUMENTS.get(uri, ("", 0))
    diagnostics = []
    for number, line in enumerate(text.splitlines()):
        for marker, severity in (("BAD", 1), ("MEH", 2)):
            if marker not in line:
                continue
            column = line.index(marker)
            diagnostics.append(
                {
                    "range": {
                        "start": {"line": number, "character": column},
                        "end": {"line": number, "character": column + len(marker)},
                    },
                    "severity": severity,
                    "code": f"{marker.lower()}-code",
                    "source": "fake",
                    "message": re.sub(r"\s+", " ", line.strip()),
                }
            )
    params = {"uri": uri, "diagnostics": diagnostics}
    if os.environ.get("FAKE_LSP_NO_VERSION") != "1":
        params["version"] = version
    write({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params})


def main() -> int:
    global CONFIGURED
    while True:
        message = read()
        if message is None:
            return 0
        method = message.get("method")
        params = message.get("params") or {}

        if method == "initialize":
            if os.environ.get("FAKE_LSP_DIE_ON_INIT") == "1":
                return 1
            write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "capabilities": {"textDocumentSync": 1},
                        "serverInfo": {"name": "fake"},
                    },
                }
            )
            if os.environ.get("FAKE_LSP_ASK_CONFIG") == "1":
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": 9001,
                        "method": "workspace/configuration",
                        "params": {"items": [{"section": "fake"}]},
                    }
                )
        elif method == "textDocument/didOpen":
            document = params.get("textDocument") or {}
            uri = document.get("uri", "")
            DOCUMENTS[uri] = (document.get("text", ""), int(document.get("version", 1)))
            publish(uri)
        elif method == "textDocument/didChange":
            document = params.get("textDocument") or {}
            uri = document.get("uri", "")
            changes = params.get("contentChanges") or [{}]
            DOCUMENTS[uri] = (
                changes[-1].get("text", ""),
                int(document.get("version", 1)),
            )
            publish(uri)
        elif method == "textDocument/didSave":
            uri = (params.get("textDocument") or {}).get("uri", "")
            if uri in DOCUMENTS:
                publish(uri)
        elif method == "textDocument/didClose":
            DOCUMENTS.pop((params.get("textDocument") or {}).get("uri", ""), None)
        elif method == "shutdown":
            write({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "exit":
            return 0
        elif method is None and message.get("id") == 9001:
            # Our configuration request came back.
            CONFIGURED = True
            for uri in list(DOCUMENTS):
                publish(uri)
    return 0


if __name__ == "__main__":
    # Written for readability, not for URI edge cases; the client is what is
    # under test.
    _ = unquote, urlparse
    sys.exit(main())
