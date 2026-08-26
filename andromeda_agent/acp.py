"""Andromeda as an editor's agent, over the Agent Client Protocol.

`andromeda acp` speaks ACP on stdin and stdout, so an editor that knows the
protocol — Zed and the others that have adopted it — can drive this harness as
its agent. The editor owns the window; this owns the turn.

**Written against the wire, not an SDK.** ACP is newline-delimited JSON-RPC
2.0, the same shape the MCP client here already speaks, and the same reasoning
applies: a few hundred lines against a protocol that is versioned, or a
dependency whose next release breaks every session. The protocol version is
declared and checked; a client asking for a version this does not implement is
told so rather than guessed at.

What the editor sees while a turn runs:

    session/update  agent_message_chunk    the answer, as it arrives
    session/update  tool_call              a tool started, with its title
    session/update  tool_call_update       and how it ended
    session/update  plan                   the todo list, when it changes

and the one call that goes the other way:

    session/request_permission             the approval gate, in the editor

That last one is the reason this adapter is more than a pipe. The gate is the
harness's own — same policy, same tiers, same learned approvals — and the
editor is only the surface it is asked through. An adapter that answered on the
user's behalf would be a way to lose the gate by changing terminal.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

PROTOCOL_VERSION = 1

# JSON-RPC error codes. Only the ones this speaks.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# What the harness's four answers look like to an editor. `never` is
# deliberately absent: a standing refusal is a decision about this machine, and
# it belongs where the other standing decisions are made rather than in a
# dialog somebody is clicking through.
PERMISSION_OPTIONS = [
    {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "session", "name": "Allow for this session", "kind": "allow_once"},
    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "no", "name": "Deny", "kind": "reject_once"},
]

# Which icon an editor draws. Read from the tool's own category and tier rather
# than a table of names, so a tool added later is classified without an edit.
KIND_BY_CATEGORY = {
    "read": "read",
    "write": "edit",
    "execute": "execute",
    "search": "search",
}


class Cancelled(Exception):
    """The editor asked for the turn to stop."""


@dataclass
class Connection:
    """One JSON-RPC peer over a pair of streams.

    Requests from the editor arrive on a reader thread; requests *to* the
    editor block the turn until their answer arrives. That is the whole reason
    for the pending-response table: `session/request_permission` is a question
    asked in the middle of a tool call, and the answer comes back interleaved
    with everything else on the same pipe.
    """

    stdin: TextIO
    stdout: TextIO
    handler: Callable[[str, dict[str, Any]], Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pending: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    _answers: dict[str, Any] = field(default_factory=dict, repr=False)
    _next_id: int = 0

    # -- writing ------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float = 3600) -> Any:
        """Ask the editor something and wait for the answer.

        The timeout is an hour rather than a minute: the question at the other
        end is being read by a person, and a gate that answers itself because
        somebody went to lunch is not a gate.
        """
        with self._lock:
            self._next_id += 1
            identifier = f"a{self._next_id}"
        event = threading.Event()
        self._pending[identifier] = event

        self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )

        if not event.wait(timeout):
            self._pending.pop(identifier, None)
            raise TimeoutError(f"{method} was never answered")

        self._pending.pop(identifier, None)
        return self._answers.pop(identifier, None)

    def respond(self, identifier: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": identifier, "result": result})

    def fail(self, identifier: Any, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": code, "message": message},
            }
        )

    # -- reading ------------------------------------------------------------

    def serve(self) -> None:
        """Read until the editor closes the pipe."""
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.fail(None, PARSE_ERROR, "not valid JSON")
                continue
            if not isinstance(message, dict):
                self.fail(None, INVALID_REQUEST, "expected an object")
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        identifier = message.get("id")
        method = message.get("method")

        if method is None:
            # A response to something we asked. Matched by id — reading the
            # next line and assuming it is yours works until the first client
            # that sends a notification mid-answer.
            key = str(identifier)
            if key in self._pending:
                self._answers[key] = message.get("result", message.get("error"))
                self._pending[key].set()
            return

        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if self.handler is None:
            if identifier is not None:
                self.fail(identifier, INTERNAL_ERROR, "no handler")
            return

        try:
            result = self.handler(str(method), params)
        except NotImplementedError as exc:
            if identifier is not None:
                self.fail(identifier, METHOD_NOT_FOUND, str(exc))
            return
        except ValueError as exc:
            if identifier is not None:
                self.fail(identifier, INVALID_PARAMS, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a bad turn is not a dead server
            if identifier is not None:
                self.fail(identifier, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return

        if identifier is not None:
            self.respond(identifier, result if result is not None else {})


# ---------------------------------------------------------------------------
# Content in and out
# ---------------------------------------------------------------------------


def text_from_blocks(blocks: Any) -> str:
    """The prompt, out of ACP's content blocks.

    Text and resources become text; an editor's `@file` mention arrives as a
    resource with its contents already read, which is the whole point of the
    protocol carrying them. Anything else is named rather than dropped, so a
    model asked about an image it cannot see is told that is what happened.
    """
    if not isinstance(blocks, list):
        return ""

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict):
                name = resource.get("uri") or "a file"
                body = resource.get("text")
                if isinstance(body, str):
                    parts.append(f"--- {name}\n{body}")
                else:
                    parts.append(f"[{name}: not text]")
        elif kind == "resource_link":
            parts.append(f"[{block.get('uri') or 'a file'}]")
        elif kind:
            parts.append(f"[{kind} content, which this agent cannot read]")
    return "\n\n".join(part for part in parts if part.strip())


def tool_kind(spec: Any) -> str:
    category = getattr(spec, "category", "")
    tier = getattr(spec, "risk_tier", "")
    if tier == "outbound":
        return "fetch"
    return KIND_BY_CATEGORY.get(category, "other")


def plan_entries(todos: Any) -> list[dict[str, Any]]:
    """The todo list, in the shape an editor draws as a plan."""
    items = getattr(todos, "items", None)
    if not items:
        return []
    status_map = {
        "pending": "pending",
        "in_progress": "in_progress",
        "completed": "completed",
    }
    entries = []
    for item in items:
        entries.append(
            {
                "content": getattr(item, "title", "") or getattr(item, "text", ""),
                "priority": "medium",
                "status": status_map.get(getattr(item, "status", ""), "pending"),
            }
        )
    return entries


# ---------------------------------------------------------------------------
# The agent side of the protocol
# ---------------------------------------------------------------------------


@dataclass
class Session:
    identifier: str
    cwd: str
    conversation: Any
    cancelled: threading.Event = field(default_factory=threading.Event)


class Agent:
    """The ACP methods, over one harness.

    `build` makes a conversation for a working directory; injected so this
    module never learns what a provider or a workspace is, and so a test can
    drive the whole protocol without a model.
    """

    def __init__(
        self,
        connection: Connection,
        build: Callable[[str], Any],
        *,
        name: str = "andromeda",
        version: str = "0",
    ) -> None:
        self.connection = connection
        self.build = build
        self.name = name
        self.version = version
        self.sessions: dict[str, Session] = {}
        connection.handler = self.handle

    # -- dispatch -----------------------------------------------------------

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self.initialize(params)
        if method == "authenticate":
            # Nothing to authenticate against: this runs as the person who
            # started it, with whatever credentials that person's install has.
            return {}
        if method == "session/new":
            return self.new_session(params)
        if method == "session/load":
            return self.load_session(params)
        if method == "session/prompt":
            return self.prompt(params)
        if method == "session/cancel":
            return self.cancel(params)
        raise NotImplementedError(f"{method} is not implemented")

    # -- methods ------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if isinstance(requested, int) and requested > PROTOCOL_VERSION:
            # Answer with what is actually spoken rather than echoing theirs.
            # Claiming a version to be agreeable is how two peers get a session
            # that fails later, on a message neither can explain.
            requested = PROTOCOL_VERSION

        return {
            "protocolVersion": requested if isinstance(requested, int) else PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": True,
                },
            },
            "authMethods": [],
            "agentInfo": {"name": self.name, "version": self.version},
        }

    def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params.get("cwd") or "")
        if not cwd:
            raise ValueError("session/new requires an absolute cwd")

        identifier = f"acp-{uuid.uuid4().hex[:12]}"
        self.sessions[identifier] = Session(
            identifier=identifier, cwd=cwd, conversation=self.build(cwd)
        )
        return {"sessionId": identifier}

    def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        identifier = str(params.get("sessionId") or "")
        if not identifier:
            raise ValueError("session/load requires a sessionId")
        if identifier not in self.sessions:
            cwd = str(params.get("cwd") or "")
            self.sessions[identifier] = Session(
                identifier=identifier, cwd=cwd, conversation=self.build(cwd)
            )
        return {}

    def cancel(self, params: dict[str, Any]) -> None:
        session = self.sessions.get(str(params.get("sessionId") or ""))
        if session is not None:
            session.cancelled.set()
        return None

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        identifier = str(params.get("sessionId") or "")
        session = self.sessions.get(identifier)
        if session is None:
            raise ValueError(f"no session {identifier!r}")

        text = text_from_blocks(params.get("prompt"))
        if not text.strip():
            return {"stopReason": "end_turn"}

        session.cancelled.clear()
        try:
            self.run_turn(session, text)
        except Cancelled:
            return {"stopReason": "cancelled"}
        return {"stopReason": "end_turn"}

    # -- the turn -----------------------------------------------------------

    def run_turn(self, session: Session, prompt: str) -> None:
        from andromeda_agent import Callbacks

        conversation = session.conversation
        update = self._updater(session)
        calls: dict[str, str] = {}
        last_plan: list[dict[str, Any]] = []

        def check_cancelled() -> None:
            if session.cancelled.is_set():
                raise Cancelled()

        def on_text(chunk: str) -> None:
            check_cancelled()
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk},
                }
            )

        def on_tool_start(spec: Any, arguments: dict[str, Any]) -> None:
            check_cancelled()
            call_id = f"call-{uuid.uuid4().hex[:8]}"
            calls[spec.name] = call_id
            update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": spec.summary(arguments),
                    "kind": tool_kind(spec),
                    "status": "in_progress",
                    "rawInput": arguments,
                }
            )

        def on_tool_result(spec: Any, result: Any) -> None:
            call_id = calls.pop(spec.name, None)
            if call_id is None:
                return
            update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed" if result.ok else "failed",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": (result.display or result.content)[:2000],
                            },
                        }
                    ],
                }
            )
            nonlocal last_plan
            entries = plan_entries(getattr(conversation, "todos", None))
            if entries and entries != last_plan:
                last_plan = entries
                update({"sessionUpdate": "plan", "entries": entries})

        def on_tool_denied(spec: Any, reason: str) -> None:
            call_id = calls.pop(spec.name, None)
            if call_id is None:
                return
            update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "failed",
                    "content": [
                        {"type": "content", "content": {"type": "text", "text": reason}}
                    ],
                }
            )

        def on_retry(reason: str) -> None:
            # An `agent_message_chunk` rather than a status of its own: the
            # protocol has no "still working" update, and an editor that has
            # been silent for twenty seconds needs to say why in the one place
            # it is already showing text.
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"\n[{reason}]\n"},
                }
            )

        conversation.send(
            prompt,
            Callbacks(
                on_text=on_text,
                on_tool_start=on_tool_start,
                on_tool_result=on_tool_result,
                on_tool_denied=on_tool_denied,
                on_retry=on_retry,
                ask_approval=self._asker(session, calls),
            ),
        )

    def _updater(self, session: Session) -> Callable[[dict[str, Any]], None]:
        def update(payload: dict[str, Any]) -> None:
            self.connection.notify(
                "session/update", {"sessionId": session.identifier, "update": payload}
            )

        return update

    def _asker(self, session: Session, calls: dict[str, str]) -> Callable[[Any], str]:
        """The approval gate, asked through the editor.

        The harness decides *that* a call needs a person; this only decides
        where the person is. Anything other than one of the four answers — a
        cancelled dialog, a closed window, a client that invents an option id —
        is a refusal, because the alternative is a tool that runs when nobody
        answered.
        """

        def ask(request: Any) -> str:
            call_id = calls.get(request.spec.name) or f"call-{uuid.uuid4().hex[:8]}"
            try:
                answer = self.connection.request(
                    "session/request_permission",
                    {
                        "sessionId": session.identifier,
                        "toolCall": {
                            "toolCallId": call_id,
                            "title": request.summary,
                            "kind": tool_kind(request.spec),
                            "status": "pending",
                            "rawInput": request.arguments,
                        },
                        "options": PERMISSION_OPTIONS,
                    },
                )
            except (TimeoutError, OSError):
                return "no"

            if not isinstance(answer, dict):
                return "no"
            outcome = answer.get("outcome")
            if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
                return "no"

            chosen = str(outcome.get("optionId") or "")
            return chosen if chosen in {"once", "session", "always", "no"} else "no"

        return ask


def serve(build: Callable[[str], Any], *, version: str = "0") -> int:
    """Speak ACP on this process's stdin and stdout until the editor stops.

    Nothing else may write to stdout while this runs — a stray `print` is a
    corrupt frame, and the editor's next parse error takes the session with it.
    """
    connection = Connection(stdin=sys.stdin, stdout=sys.stdout)
    Agent(connection, build, version=version)
    try:
        connection.serve()
    except KeyboardInterrupt:
        return 0
    return 0
