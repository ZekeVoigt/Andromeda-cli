"""The wire format: JSON-RPC 2.0 inside `Content-Length` frames.

LSP is JSON-RPC with an HTTP-style header, which is the one thing that stops
the MCP transport being reused verbatim — MCP is newline-delimited, and a
newline reader against an LSP server stops at the first blank line of the
header and never recovers.

Deliberately synchronous. Upstream is asyncio because its whole runtime is;
this harness runs its concurrency on threads, and a second event loop inside a
CLI turn is a source of deadlocks rather than a source of speed.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

# The one field a frame must have. Anything else in the header is informational
# — `Content-Type` appears in the specification and nobody has ever needed it.
_LENGTH = b"content-length:"

# A frame bigger than this is a server that has gone wrong. `rust-analyzer`'s
# largest legitimate messages are workspace symbol dumps in the low megabytes;
# past sixty-four the honest reading is a corrupt stream, and allocating for it
# would turn one broken server into an out-of-memory kill.
MAX_FRAME_BYTES = 64 * 1024 * 1024


class ProtocolError(RuntimeError):
    """The framing itself is broken — not the same as a server saying no."""


class RequestFailed(RuntimeError):
    """The server answered with a JSON-RPC error, which is protocol-conformant."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"LSP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def encode(message: dict[str, Any]) -> bytes:
    """One framed message.

    Compact separators, because `Content-Length` counts bytes and a pretty
    printer that adds a space changes the count. `ensure_ascii=False` so a
    diagnostic about an identifier with an accent in it is not expanded into
    escapes the server then has to undo.
    """
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def read(stream: BinaryIO) -> dict[str, Any] | None:
    """One framed message, or `None` at a clean end of stream.

    `None` rather than an exception at EOF: a server that has been asked to
    shut down closes its stdout between frames, and that is the successful
    case, not a failure.
    """
    length = -1
    while True:
        line = stream.readline()
        if not line:
            # EOF. Mid-header it is a truncated frame, but the caller cannot do
            # anything different about it either way, and treating a dead
            # server as "no more messages" is what every caller wants.
            return None
        if line in (b"\r\n", b"\n"):
            break
        lowered = line.lower()
        if lowered.startswith(_LENGTH):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError) as exc:
                raise ProtocolError(f"unreadable Content-Length: {line!r}") from exc

    if length < 0:
        raise ProtocolError("a frame arrived with no Content-Length")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"a frame claimed {length} bytes")

    body = _read_exactly(stream, length)
    if body is None:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"a frame was not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("a frame was not a JSON-RPC envelope")
    return message


def _read_exactly(stream: BinaryIO, count: int) -> bytes | None:
    """Exactly `count` bytes, or `None` if the stream ended first.

    `read(n)` on a pipe is allowed to return fewer bytes than asked for, and it
    does under load. Assuming otherwise gives a JSON parse error on a message
    that was perfectly well formed — the kind of bug that only appears on a
    busy machine.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def request(identifier: int, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def response(identifier: Any, result: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


__all__ = [
    "MAX_FRAME_BYTES",
    "ProtocolError",
    "RequestFailed",
    "encode",
    "notification",
    "read",
    "request",
    "response",
]
