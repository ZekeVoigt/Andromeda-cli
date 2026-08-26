"""Model Context Protocol clients.

One integration, and the harness gains whatever anyone has published a server
for. This is the highest capability-per-line thing in the codebase: every other
tool here buys one capability, this buys a category.

Implemented against the wire protocol directly rather than through an SDK.
MCP over stdio is newline-delimited JSON-RPC 2.0 — an `initialize` handshake, an
`initialized` notification, `tools/list`, then `tools/call`. That is a few
hundred lines, and it means no dependency whose next release can break every
session. Streamable HTTP is the same JSON-RPC over POST.

Naming follows the `mcp__<server>__<tool>` convention MCP clients share, so a
tool's origin is legible in its name and two servers exposing `search` do not
collide.

**Every MCP tool is `outbound`.** It is third-party code, configured by the
user, reaching somewhere this harness knows nothing about. Tiering by what a
server *claims* its tool does would take the word of the thing being gated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .spec import ToolResult, ToolSpec, failure

PREFIX = "mcp__"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "andromeda-cli", "version": "0.1.0"}

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 40_000

SAFE_NAME = re.compile(r"[^a-z0-9_]+")


def config_path(home: Path) -> Path:
    return home / "mcp.json"


def sanitize(name: str) -> str:
    """A tool-name component the API will accept.

    Model tool names are `[a-zA-Z0-9_-]` and length-limited. A server called
    `@acme/search-tools` has to become something legal without colliding with
    its neighbour.
    """
    cleaned = SAFE_NAME.sub("_", (name or "").strip().lower()).strip("_")
    return cleaned or "unnamed"


def tool_name(server: str, tool: str) -> str:
    return f"{PREFIX}{sanitize(server)}__{sanitize(tool)}"


class MCPError(RuntimeError):
    pass


def _reason(exc: Exception) -> str:
    """An exception as a message the person reading it can act on.

    `mcp_auth` raises with a `hint` carrying the command to run. Dropping it
    turns "sign in with this command" into "something went wrong", which sends
    people to check their config file instead.
    """
    hint = getattr(exc, "hint", "")
    return (f"{exc} {hint}".strip() if hint else str(exc))[:300]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class StdioTransport:
    """A server run as a child process, spoken to over its stdin and stdout.

    Responses are matched to requests by id and handed to the waiting caller,
    because a server may interleave notifications and progress messages with
    the reply you are waiting for — reading the next line and assuming it is
    yours works until the first server that logs.
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None) -> None:
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[int, dict[str, Any]] = {}
        self._events = threading.Condition()
        self._stderr: list[str] = []

    def start(self) -> None:
        self._process = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, **self.env},
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        stream = self._process.stdout if self._process else None
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Servers that print to stdout are common and not fatal; the
                # protocol only cares about lines that parse.
                continue
            identifier = message.get("id")
            if identifier is None:
                continue
            with self._events:
                self._pending[identifier] = message
                self._events.notify_all()

    def _read_stderr(self) -> None:
        stream = self._process.stderr if self._process else None
        if stream is None:
            return
        for line in stream:
            # Kept, not discarded: when a server fails to start, its stderr is
            # the only thing that says why.
            self._stderr.append(line.rstrip("\n"))
            del self._stderr[:-50]

    def send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        if self._process is None or self._process.poll() is not None:
            detail = "\n".join(self._stderr[-5:])
            raise MCPError(f"the server is not running{': ' + detail if detail else ''}")

        line = json.dumps(payload) + "\n"
        try:
            self._process.stdin.write(line)  # type: ignore[union-attr]
            self._process.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"could not write to the server: {exc}") from exc

        identifier = payload.get("id")
        if identifier is None:
            return None

        deadline = time.time() + timeout
        with self._events:
            while identifier not in self._pending:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise MCPError(f"timed out after {timeout:.0f}s")
                self._events.wait(remaining)
            return self._pending.pop(identifier)

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            self._process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            try:
                self._process.kill()
            except OSError:
                pass
        self._process = None


class HTTPTransport:
    """A server reached over streamable HTTP. Each request is one POST.

    `auth`, when present, owns everything about credentials: it supplies the
    headers and it decides what a 401 means. This transport knows only that a
    401 is worth asking about once.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None,
        auth: Any = None,
    ) -> None:
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.auth = auth
        self._session_id: str | None = None
        self._client = httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT))

    def start(self) -> None:
        return None

    def send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        response = self._post(payload, timeout)

        if response.status_code == 401 and self.auth is not None:
            # Once, never in a loop. A server that refuses a freshly issued
            # token will refuse the next one too, and a retry loop spends the
            # user's authorization on a spin.
            if self.auth.handle_unauthorized():
                response = self._post(payload, timeout)

        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session

        if payload.get("id") is None:
            return None
        if response.status_code == 401:
            raise MCPError(
                "the server rejected our credentials — "
                "run `andromeda mcp login <server>` to sign in again"
            )
        if response.status_code >= 400:
            raise MCPError(f"HTTP {response.status_code}: {response.text[:200]}")

        body = response.text.strip()
        if body.startswith("event:") or body.startswith("data:"):
            # A single SSE frame carrying the JSON-RPC reply. Anything richer
            # than that needs a streaming client, which this is not.
            for line in body.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
                    break
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPError(f"the server returned invalid JSON: {exc}") from exc

    def _post(self, payload: dict[str, Any], timeout: float) -> httpx.Response:
        headers = dict(self.headers)
        headers["Accept"] = "application/json, text/event-stream"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.auth is not None:
            # Asked for per request, not cached: this is where an expiring
            # token gets refreshed, and a header captured at connect time would
            # go stale in the middle of a long session.
            headers.update(self.auth.headers())

        try:
            return self._client.post(
                self.url, json=payload, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise MCPError(f"request failed: {exc}") from exc

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


@dataclass
class MCPServer:
    name: str
    config: dict[str, Any]
    # Where credentials live. `None` means this server was built without a home
    # — a test, or a caller that has no business signing anything in — and OAuth
    # is then simply not offered rather than falling back to a guessed path.
    home: Path | None = None
    transport: Any = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    connected: bool = False
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    @property
    def uses_oauth(self) -> bool:
        """Whether this server is configured to authorize rather than assert.

        `auth: "oauth"` is explicit, and `oauth: {...}` implies it — a config
        that carries a scope and a client id and then does not use them is a
        config somebody expects to work.
        """
        return bool(
            str(self.config.get("auth") or "").lower() == "oauth"
            or self.config.get("oauth")
        )

    def _authorization(self):
        if not self.uses_oauth or self.home is None or "url" not in self.config:
            return None
        from . import mcp_auth

        return mcp_auth.Authorization(
            self.home,
            self.name,
            str(self.config["url"]),
            self.config.get("oauth") if isinstance(self.config.get("oauth"), dict) else {},
            # A tool call is never the place a browser opens. The model asked
            # for a search, not for a consent screen, and a session that stops
            # to authorize mid-turn has taken a decision that belongs to the
            # person. `andromeda mcp login` is where that happens.
            interactive=False,
        )

    def _build_transport(self):
        if "url" in self.config:
            return HTTPTransport(
                str(self.config["url"]),
                self.config.get("headers"),
                self._authorization(),
            )
        command = self.config.get("command")
        if not command:
            raise MCPError("needs either `command` or `url`")
        return StdioTransport(
            str(command),
            [str(a) for a in (self.config.get("args") or [])],
            {str(k): str(v) for k, v in (self.config.get("env") or {}).items()},
        )

    def connect(self) -> bool:
        """Handshake and list tools. Failure is recorded, never raised.

        One misconfigured server must not take the session down with it — the
        other servers still work, and the error belongs in `andromeda mcp` where
        someone can read it.
        """
        if self.connected:
            return True
        try:
            self.transport = self._build_transport()
            self.transport.start()

            reply = self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": CLIENT_INFO,
                    },
                },
                CONNECT_TIMEOUT,
            )
            if reply and "error" in reply:
                raise MCPError(str(reply["error"].get("message", reply["error"])))

            # A notification, so no id and no reply to wait for. Servers are
            # entitled to refuse everything until they receive it.
            self.transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, CONNECT_TIMEOUT
            )

            listed = self.transport.send(
                {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"},
                CONNECT_TIMEOUT,
            )
            if listed and "error" in listed:
                raise MCPError(str(listed["error"].get("message", listed["error"])))

            self.tools = list(((listed or {}).get("result") or {}).get("tools") or [])
            self.connected = True
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - surfaced through `error`
            # A server that needs signing in is not broken, and reporting it as
            # broken sends people to check their config file instead of running
            # the one command that fixes it.
            self.error = _reason(exc)
            self.close()
            return False

    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        if not self.connected and not self.connect():
            return failure(f"{self.name} is not available: {self.error}")

        try:
            reply = self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {}},
                },
                CALL_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - a tool must never end a turn
            # Broad on purpose. `MCPError` is the common case — and a dead
            # server must not stay marked live, so the next call reconnects
            # rather than writing into a closed pipe forever. But an expired
            # authorization raises out of `mcp_auth` instead, and a tool call
            # that propagates ends the turn rather than letting the model read
            # what happened and move on.
            self.connected = False
            return failure(f"{self.name}/{tool} failed: {_reason(exc)}")

        if reply and "error" in reply:
            message = reply["error"].get("message", reply["error"])
            return failure(f"{self.name}/{tool}: {message}")

        result = (reply or {}).get("result") or {}
        text = _flatten(result)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n\n… truncated."

        return ToolResult(
            content=text or "(the tool returned nothing)",
            display=f"{self.name}/{tool}",
            ok=not result.get("isError", False),
        )

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        self.transport = None
        self.connected = False


def _flatten(result: dict[str, Any]) -> str:
    """MCP content blocks, as text.

    Images and audio are named but not decoded: this harness reasons about
    structure, never pixels, and a base64 blob in a transcript is pure cost.
    """
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind} content, {block.get('mimeType', 'unknown type')}, not shown]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], indent=2))
    return "\n".join(part for part in parts if part).strip()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(home: Path) -> dict[str, dict[str, Any]]:
    """Servers from `mcp.json`.

    Accepts `mcpServers` (what every other MCP client writes) and the
    `mcp_servers` snake_case variant, so a config can be copied in from either
    spelling without editing.
    """
    path = config_path(home)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}

    servers = raw.get("mcpServers") or raw.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): config
        for name, config in servers.items()
        if isinstance(config, dict) and not config.get("disabled")
    }


def build_servers(home: Path) -> list[MCPServer]:
    return [
        MCPServer(name=name, config=config, home=home)
        for name, config in load_config(home).items()
    ]


def specs_for(server: MCPServer) -> list[ToolSpec]:
    """One ToolSpec per tool the server advertises."""
    out: list[ToolSpec] = []
    for tool in server.tools:
        name = str(tool.get("name") or "")
        if not name:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}

        description = str(tool.get("description") or f"{name} on the {server.name} server.")
        out.append(
            ToolSpec(
                name=tool_name(server.name, name),
                description=f"[{server.name}] {description}",
                parameters=schema,
                # Third-party code reaching somewhere this harness knows nothing
                # about. Trusting a server's own account of what it does would
                # take the word of the thing being gated.
                risk_tier="outbound",
                category="write",
                run=_bind(server, name),
                summarize=lambda arguments, s=server.name, t=name: (
                    f"{s}/{t} " + json.dumps(arguments)[:70]
                ),
            )
        )
    return out


def _bind(server: MCPServer, tool: str):
    def run(**arguments: Any) -> ToolResult:
        return server.call(tool, arguments)

    return run
