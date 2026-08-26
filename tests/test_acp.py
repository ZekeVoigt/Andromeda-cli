"""The editor-facing protocol: framing, the methods, a turn, the gate."""

from __future__ import annotations

import io
import json
import threading
from typing import Any

import pytest

from andromeda_agent import Conversation, Policy, acp
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider, call, turn_with


class Recorder(io.StringIO):
    """A stdout that keeps every frame, parsed."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []

    def write(self, text: str) -> int:  # type: ignore[override]
        for line in text.splitlines():
            if line.strip():
                self.frames.append(json.loads(line))
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing to flush
        pass

    def results(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if "result" in frame]

    def errors(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if "error" in frame]

    def updates(self, kind: str = "") -> list[dict[str, Any]]:
        out = []
        for frame in self.frames:
            if frame.get("method") != "session/update":
                continue
            update = frame["params"]["update"]
            if not kind or update.get("sessionUpdate") == kind:
                out.append(update)
        return out


def drive(lines: list[str], build=lambda cwd: object(), version: str = "0") -> Recorder:
    """Feed a script of frames through a connection and collect the answers."""
    recorder = Recorder()
    connection = acp.Connection(
        stdin=io.StringIO("\n".join(lines) + "\n"), stdout=recorder
    )
    acp.Agent(connection, build, version=version)
    connection.serve()
    return recorder


def frame(identifier: Any, method: str, params: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if identifier is not None:
        payload["id"] = identifier
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def test_a_request_gets_a_result():
    recorder = drive([frame(1, "initialize", {"protocolVersion": 1})])
    assert recorder.results()[0]["id"] == 1


def test_unparseable_input_is_answered_not_fatal():
    recorder = drive(["{not json", frame(1, "initialize", {"protocolVersion": 1})])
    assert recorder.errors()[0]["error"]["code"] == acp.PARSE_ERROR
    assert recorder.results()


def test_a_non_object_frame_is_refused():
    recorder = drive(["[1,2,3]", frame(1, "initialize", {})])
    assert recorder.errors()[0]["error"]["code"] == acp.INVALID_REQUEST


def test_blank_lines_are_ignored():
    recorder = drive(["", "   ", frame(1, "initialize", {})])
    assert len(recorder.results()) == 1


def test_an_unknown_method_is_a_method_not_found():
    recorder = drive([frame(1, "session/teleport", {})])
    assert recorder.errors()[0]["error"]["code"] == acp.METHOD_NOT_FOUND


def test_a_notification_of_an_unknown_method_is_silent():
    """No id means nobody is waiting for an answer, and an error frame with a
    null id is noise the client cannot route."""
    recorder = drive([frame(None, "session/teleport", {})])
    assert recorder.frames == []


def test_bad_parameters_are_an_invalid_params():
    recorder = drive([frame(1, "session/new", {})])
    assert recorder.errors()[0]["error"]["code"] == acp.INVALID_PARAMS


def test_a_handler_that_raises_is_an_internal_error():
    def explode(cwd):
        raise RuntimeError("no")

    recorder = drive([frame(1, "session/new", {"cwd": "/tmp"})], build=explode)
    assert recorder.errors()[0]["error"]["code"] == acp.INTERNAL_ERROR


def test_the_server_survives_a_failed_turn():
    def explode(cwd):
        raise RuntimeError("no")

    recorder = drive(
        [frame(1, "session/new", {"cwd": "/tmp"}), frame(2, "initialize", {})],
        build=explode,
    )
    assert recorder.errors()
    assert recorder.results()


# ---------------------------------------------------------------------------
# the handshake
# ---------------------------------------------------------------------------


def test_initialize_declares_what_is_spoken():
    result = drive([frame(1, "initialize", {"protocolVersion": 1})]).results()[0]["result"]

    assert result["protocolVersion"] == acp.PROTOCOL_VERSION
    assert result["agentCapabilities"]["loadSession"] is True
    assert result["agentCapabilities"]["promptCapabilities"]["embeddedContext"] is True
    assert result["agentInfo"]["name"] == "andromeda"


def test_a_newer_client_is_answered_with_what_is_actually_spoken():
    """Claiming a version to be agreeable is how two peers get a session that
    fails later, on a message neither can explain."""
    result = drive([frame(1, "initialize", {"protocolVersion": 99})]).results()[0]
    assert result["result"]["protocolVersion"] == acp.PROTOCOL_VERSION


def test_images_are_not_claimed():
    result = drive([frame(1, "initialize", {})]).results()[0]["result"]
    assert result["agentCapabilities"]["promptCapabilities"]["image"] is False


def test_authenticate_is_a_no_op():
    """It runs as whoever started it, with that person's install."""
    recorder = drive([frame(1, "authenticate", {"methodId": "x"})])
    assert recorder.results()[0]["result"] == {}


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_a_new_session_gets_an_id():
    result = drive([frame(1, "session/new", {"cwd": "/tmp"})]).results()[0]["result"]
    assert result["sessionId"].startswith("acp-")


def test_a_new_session_builds_a_conversation_for_that_directory():
    seen: list[str] = []
    drive([frame(1, "session/new", {"cwd": "/tmp/project"})], build=lambda cwd: seen.append(cwd))
    assert seen == ["/tmp/project"]


def test_a_session_needs_a_working_directory():
    recorder = drive([frame(1, "session/new", {})])
    assert "cwd" in recorder.errors()[0]["error"]["message"]


def test_loading_an_unknown_session_makes_one():
    """An editor reopening a project should get a working agent, not a fault."""
    recorder = drive([frame(1, "session/load", {"sessionId": "old", "cwd": "/tmp"})])
    assert recorder.results()[0]["result"] == {}


def test_prompting_an_unknown_session_is_an_error():
    recorder = drive([frame(1, "session/prompt", {"sessionId": "nope", "prompt": []})])
    assert "no session" in recorder.errors()[0]["error"]["message"]


# ---------------------------------------------------------------------------
# content blocks
# ---------------------------------------------------------------------------


def test_text_blocks_become_the_prompt():
    assert acp.text_from_blocks(
        [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    ) == "hello\n\nworld"


def test_an_attached_file_arrives_with_its_contents():
    """The editor already read it — that is the point of the protocol carrying
    it rather than a path."""
    text = acp.text_from_blocks(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///a.py", "text": "print(1)"},
            }
        ]
    )
    assert "file:///a.py" in text
    assert "print(1)" in text


def test_a_link_is_named():
    assert "file:///a.py" in acp.text_from_blocks(
        [{"type": "resource_link", "uri": "file:///a.py"}]
    )


def test_content_that_cannot_be_read_says_so():
    """A model asked about an image it cannot see should be told that is what
    happened, not handed silence."""
    assert "cannot read" in acp.text_from_blocks([{"type": "image", "data": "..."}])


def test_nonsense_blocks_are_survivable():
    assert acp.text_from_blocks(None) == ""
    assert acp.text_from_blocks(["not a block", 5]) == ""


# ---------------------------------------------------------------------------
# a real turn
# ---------------------------------------------------------------------------


def conversation_for(tmp_path, script):
    workspace = Workspace(tmp_path)
    todos = TodoList()
    return Conversation(
        provider=ScriptedProvider(script=list(script)),  # type: ignore[arg-type]
        policy=Policy(
            mode="auto",
            enabled=frozenset({"read_file", "write_file", "terminal"}),
            max_tier="destructive",
        ),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
    )


def test_a_turn_streams_the_answer(tmp_path):
    recorder = drive(
        [
            frame(1, "session/new", {"cwd": str(tmp_path)}),
            frame(
                2,
                "session/prompt",
                {
                    "sessionId": "acp-fixed",
                    "prompt": [{"type": "text", "text": "hello"}],
                },
            ),
        ],
        build=lambda cwd: conversation_for(tmp_path, ["hi there"]),
    )
    # The id from session/new is generated, so drive the second call against
    # the one that came back.
    session_id = recorder.results()[0]["result"]["sessionId"]

    recorder = drive(
        [
            frame(1, "session/load", {"sessionId": session_id, "cwd": str(tmp_path)}),
            frame(
                2,
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello"}]},
            ),
        ],
        build=lambda cwd: conversation_for(tmp_path, ["hi there"]),
    )

    chunks = recorder.updates("agent_message_chunk")
    assert "".join(chunk["content"]["text"] for chunk in chunks) == "hi there"
    assert recorder.results()[-1]["result"]["stopReason"] == "end_turn"


def test_an_empty_prompt_ends_the_turn(tmp_path):
    recorder = drive(
        [
            frame(1, "session/load", {"sessionId": "s", "cwd": str(tmp_path)}),
            frame(2, "session/prompt", {"sessionId": "s", "prompt": []}),
        ],
        build=lambda cwd: conversation_for(tmp_path, ["never asked"]),
    )
    assert recorder.results()[-1]["result"]["stopReason"] == "end_turn"
    assert recorder.updates("agent_message_chunk") == []


def test_a_tool_call_is_reported_as_it_runs(tmp_path):
    script = [
        turn_with(call("write_file", {"path": "out.txt", "content": "x"})),
        "done",
    ]
    recorder = drive(
        [
            frame(1, "session/load", {"sessionId": "s", "cwd": str(tmp_path)}),
            frame(
                2,
                "session/prompt",
                {"sessionId": "s", "prompt": [{"type": "text", "text": "write it"}]},
            ),
        ],
        build=lambda cwd: conversation_for(tmp_path, script),
    )

    started = recorder.updates("tool_call")
    finished = recorder.updates("tool_call_update")

    assert started[0]["status"] == "in_progress"
    assert started[0]["kind"] == "edit"
    assert "out.txt" in started[0]["title"]
    assert finished[0]["status"] == "completed"
    assert finished[0]["toolCallId"] == started[0]["toolCallId"]


def test_a_failing_tool_is_reported_as_failed(tmp_path):
    script = [turn_with(call("read_file", {"path": "missing.txt"})), "done"]
    recorder = drive(
        [
            frame(1, "session/load", {"sessionId": "s", "cwd": str(tmp_path)}),
            frame(
                2,
                "session/prompt",
                {"sessionId": "s", "prompt": [{"type": "text", "text": "read it"}]},
            ),
        ],
        build=lambda cwd: conversation_for(tmp_path, script),
    )
    assert recorder.updates("tool_call_update")[0]["status"] == "failed"


def test_a_denied_tool_is_reported_with_its_reason(tmp_path):
    workspace = Workspace(tmp_path)
    todos = TodoList()
    conversation = Conversation(
        provider=ScriptedProvider(  # type: ignore[arg-type]
            script=[turn_with(call("terminal", {"command": "ls"})), "done"]
        ),
        policy=Policy(mode="auto", enabled=frozenset({"read_file"})),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
    )
    recorder = drive(
        [
            frame(1, "session/load", {"sessionId": "s", "cwd": str(tmp_path)}),
            frame(
                2,
                "session/prompt",
                {"sessionId": "s", "prompt": [{"type": "text", "text": "run it"}]},
            ),
        ],
        build=lambda cwd: conversation,
    )
    # The tool never started, so there is no call to update — the denial
    # reaches the model, and the editor sees the turn end.
    assert recorder.results()[-1]["result"]["stopReason"] == "end_turn"


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


class Answering(Recorder):
    """A client that answers `session/request_permission` the way a person
    would, and records what it was asked."""

    def __init__(self, option: str | None) -> None:
        super().__init__()
        self.option = option
        self.asked: list[dict[str, Any]] = []
        self.connection: acp.Connection | None = None

    def write(self, text: str) -> int:  # type: ignore[override]
        super().write(text)
        for line in text.splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("method") != "session/request_permission":
                continue
            self.asked.append(message["params"])
            outcome = (
                {"outcome": "selected", "optionId": self.option}
                if self.option
                else {"outcome": "cancelled"}
            )
            assert self.connection is not None
            # Answered from this thread; the agent is blocked on it from the
            # reader thread, which is exactly the live arrangement.
            threading.Thread(
                target=self.connection._dispatch,
                args=({"id": message["id"], "result": {"outcome": outcome}},),
            ).start()
        return len(text)


def drive_with_gate(tmp_path, option, script):
    recorder = Answering(option)
    connection = acp.Connection(
        stdin=io.StringIO(
            "\n".join(
                [
                    frame(1, "session/load", {"sessionId": "s", "cwd": str(tmp_path)}),
                    frame(
                        2,
                        "session/prompt",
                        {"sessionId": "s", "prompt": [{"type": "text", "text": "go"}]},
                    ),
                ]
            )
            + "\n"
        ),
        stdout=recorder,
    )
    recorder.connection = connection
    workspace = Workspace(tmp_path)
    todos = TodoList()
    conversation = Conversation(
        provider=ScriptedProvider(script=list(script)),  # type: ignore[arg-type]
        policy=Policy(
            mode="ask",
            enabled=frozenset({"read_file", "write_file"}),
            max_tier="destructive",
        ),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
    )
    acp.Agent(connection, lambda cwd: conversation)
    connection.serve()
    return recorder


WRITE = [turn_with(call("write_file", {"path": "out.txt", "content": "x"})), "done"]


def test_the_gate_is_asked_through_the_editor(tmp_path):
    recorder = drive_with_gate(tmp_path, "once", WRITE)

    assert recorder.asked
    request = recorder.asked[0]
    assert request["toolCall"]["title"]
    assert {option["optionId"] for option in request["options"]} == {
        "once",
        "session",
        "always",
        "no",
    }
    assert (tmp_path / "out.txt").exists()


def test_denying_in_the_editor_stops_the_call(tmp_path):
    drive_with_gate(tmp_path, "no", WRITE)
    assert not (tmp_path / "out.txt").exists()


def test_a_cancelled_dialog_is_a_refusal(tmp_path):
    """Anything that is not one of the four answers is a refusal — the
    alternative is a tool that runs when nobody answered."""
    drive_with_gate(tmp_path, None, WRITE)
    assert not (tmp_path / "out.txt").exists()


def test_an_invented_option_is_a_refusal(tmp_path):
    drive_with_gate(tmp_path, "definitely-yes", WRITE)
    assert not (tmp_path / "out.txt").exists()


# ---------------------------------------------------------------------------
# odds and ends
# ---------------------------------------------------------------------------


def test_cancel_marks_the_session(tmp_path):
    recorder = Recorder()
    connection = acp.Connection(stdin=io.StringIO(""), stdout=recorder)
    agent = acp.Agent(connection, lambda cwd: object())
    agent.handle("session/new", {"cwd": str(tmp_path)})
    identifier = next(iter(agent.sessions))

    agent.handle("session/cancel", {"sessionId": identifier})

    assert agent.sessions[identifier].cancelled.is_set()


def test_cancelling_an_unknown_session_is_harmless():
    connection = acp.Connection(stdin=io.StringIO(""), stdout=Recorder())
    agent = acp.Agent(connection, lambda cwd: object())
    assert agent.handle("session/cancel", {"sessionId": "nope"}) is None


@pytest.mark.parametrize(
    "category,tier,expected",
    [
        ("read", "safe_local", "read"),
        ("write", "destructive", "edit"),
        ("execute", "destructive", "execute"),
        ("read", "outbound", "fetch"),
        ("unknown", "safe_local", "other"),
    ],
)
def test_tool_kinds_come_from_the_tool(category, tier, expected):
    class Spec:
        pass

    spec = Spec()
    spec.category = category
    spec.risk_tier = tier
    assert acp.tool_kind(spec) == expected


def test_an_unanswered_request_times_out_rather_than_hanging():
    connection = acp.Connection(stdin=io.StringIO(""), stdout=Recorder())
    with pytest.raises(TimeoutError):
        connection.request("session/request_permission", {}, timeout=0.05)


def test_a_response_to_nothing_is_ignored():
    connection = acp.Connection(stdin=io.StringIO(""), stdout=Recorder())
    connection._dispatch({"id": "a99", "result": {}})  # must not raise
