from __future__ import annotations

from typing import Any

import pytest

from andromeda_agent import Callbacks, Conversation, Policy
from andromeda_agent.errors import AgentError
from andromeda_agent.loop import MAX_STEPS
from andromeda_agent.providers.base import AssistantTurn, ToolCall
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider, call, turn_with

ALL_TOOLS = frozenset(
    {"read_file", "list_dir", "search_files", "write_file", "patch", "terminal", "todo"}
)


def make(tmp_path, script, *, mode="auto", enabled=ALL_TOOLS, max_tier="destructive"):
    provider = ScriptedProvider(script=list(script))
    workspace = Workspace(tmp_path)
    todos = TodoList()
    conversation = Conversation(
        provider=provider,  # type: ignore[arg-type]
        policy=Policy(mode=mode, enabled=enabled, max_tier=max_tier),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
    )
    return conversation, provider


def test_a_plain_turn_records_both_messages(tmp_path):
    conversation, _ = make(tmp_path, ["Hello"])
    received: list[str] = []

    reply = conversation.send("hi", Callbacks(on_text=received.append))

    assert reply == "Hello"
    assert received == ["Hello"]
    assert conversation.messages[-2]["role"] == "user"
    assert conversation.messages[-1]["content"] == "Hello"


def test_a_tool_call_runs_and_its_result_goes_back(tmp_path):
    (tmp_path / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    conversation, provider = make(
        tmp_path,
        [turn_with(call("read_file", {"path": "note.txt"})), "It says alpha and beta."],
    )

    reply = conversation.send("what is in note.txt")

    assert reply == "It says alpha and beta."
    tool_messages = [m for m in conversation.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "alpha" in tool_messages[0]["content"]
    # The tool result must be joined to the call by id, or the next request is
    # malformed.
    assert tool_messages[0]["tool_call_id"] == "call_1"


def test_the_loop_continues_until_a_turn_has_no_tool_calls(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    conversation, provider = make(
        tmp_path,
        [
            turn_with(call("read_file", {"path": "a.txt"}, "c1")),
            turn_with(call("read_file", {"path": "b.txt"}, "c2")),
            "Both read.",
        ],
    )

    assert conversation.send("read both") == "Both read."
    assert len(provider.seen) == 3


def test_denied_tools_are_not_advertised_to_the_model(tmp_path):
    conversation, provider = make(tmp_path, ["ok"], enabled=frozenset({"read_file"}))
    conversation.send("hi")

    names = {tool["function"]["name"] for tool in provider.seen_tools[0] or []}
    assert names == {"read_file"}


def test_a_ceiling_removes_tools_above_it(tmp_path):
    conversation, provider = make(tmp_path, ["ok"], max_tier="safe_local")
    names = {spec.name for spec in conversation.available}

    assert "terminal" not in names
    assert "write_file" not in names
    assert "read_file" in names


def test_approval_is_requested_before_the_tool_runs(tmp_path):
    target = tmp_path / "out.txt"
    conversation, _ = make(
        tmp_path,
        [turn_with(call("write_file", {"path": "out.txt", "content": "x"})), "done"],
        mode="ask",
    )

    asked: list[str] = []

    def approve(request):
        # The file must not exist yet at the moment consent is sought.
        assert not target.exists()
        asked.append(request.summary)
        return "once"

    conversation.send("write it", Callbacks(ask_approval=approve))

    assert asked and "out.txt" in asked[0]
    assert target.read_text(encoding="utf-8") == "x"


def test_a_refusal_stops_the_call_and_tells_the_model_not_to_retry(tmp_path):
    conversation, _ = make(
        tmp_path,
        [turn_with(call("write_file", {"path": "out.txt", "content": "x"})), "understood"],
        mode="ask",
    )

    conversation.send("write it", Callbacks(ask_approval=lambda request: "no"))

    assert not (tmp_path / "out.txt").exists()
    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "declined" in tool_message["content"]
    assert "Do not retry" in tool_message["content"]


def test_a_session_grant_stops_the_second_prompt(tmp_path):
    conversation, _ = make(
        tmp_path,
        [
            turn_with(call("write_file", {"path": "a.txt", "content": "1"}, "c1")),
            turn_with(call("write_file", {"path": "b.txt", "content": "2"}, "c2")),
            "done",
        ],
        mode="ask",
    )

    prompts = 0

    def approve(request):
        nonlocal prompts
        prompts += 1
        return "session"

    conversation.send("write both", Callbacks(ask_approval=approve))

    assert prompts == 1
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_no_prompt_available_means_refused_never_auto_approved(tmp_path):
    conversation, _ = make(
        tmp_path,
        [turn_with(call("write_file", {"path": "out.txt", "content": "x"})), "ok"],
        mode="ask",
    )

    # Callbacks with no ask_approval — nobody is watching.
    conversation.send("write it", Callbacks())

    assert not (tmp_path / "out.txt").exists()


def test_unknown_tool_is_answered_not_dropped(tmp_path):
    conversation, _ = make(tmp_path, [turn_with(call("teleport", {})), "sorry"])
    conversation.send("go")

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "no tool named" in tool_message["content"]


def test_unparseable_arguments_are_handed_back(tmp_path):
    broken = ToolCall(
        id="c1", name="read_file", arguments={}, raw_arguments="{not json",
        parse_error="arguments are not valid JSON: boom",
    )
    conversation, _ = make(tmp_path, [AssistantTurn(tool_calls=[broken]), "fixed"])
    conversation.send("read")

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert tool_message["tool_call_id"] == "c1"
    assert "not valid JSON" in tool_message["content"]


def test_wrong_arguments_come_back_as_a_result_not_a_crash(tmp_path):
    conversation, _ = make(
        tmp_path, [turn_with(call("read_file", {"nonsense": 1})), "recovered"]
    )
    assert conversation.send("read") == "recovered"

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "rejected its arguments" in tool_message["content"]


def test_a_failing_tool_does_not_end_the_session(tmp_path):
    conversation, _ = make(
        tmp_path, [turn_with(call("read_file", {"path": "missing.txt"})), "not there"]
    )
    assert conversation.send("read") == "not there"


def test_the_step_ceiling_stops_a_runaway_loop(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    # More tool turns than the ceiling allows.
    script = [turn_with(call("read_file", {"path": "a.txt"}, f"c{i}")) for i in range(MAX_STEPS + 5)]
    conversation, provider = make(tmp_path, script)

    conversation.send("loop")

    assert len(provider.seen) == MAX_STEPS
    assert "Stopped after" in conversation.messages[-1]["content"]


def test_a_provider_failure_propagates(tmp_path):
    conversation, provider = make(tmp_path, [])
    provider.raises = AgentError("boom")

    with pytest.raises(AgentError):
        conversation.send("hi")


def test_reset_clears_the_transcript_and_the_todos(tmp_path):
    conversation, _ = make(tmp_path, ["ok"])
    conversation.send("hi")
    conversation.todos.replace([{"task": "x", "status": "done"}])

    conversation.reset()

    assert len(conversation.messages) == 1
    assert conversation.todos.items == []
    assert conversation.turn_count == 0


def test_the_workspace_root_is_stated_in_the_system_prompt(tmp_path):
    conversation, _ = make(tmp_path, ["ok"])
    assert str(tmp_path.resolve()) in conversation.messages[0]["content"]
