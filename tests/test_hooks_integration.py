"""Hooks against the real turn loop — the part that has to actually change
what the agent does."""

from __future__ import annotations

from typing import Any

import pytest

from andromeda_agent import Callbacks, Conversation, Policy, hooks
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider, call, turn_with

ALL_TOOLS = frozenset(
    {"read_file", "list_dir", "search_files", "write_file", "patch", "terminal", "todo"}
)


@pytest.fixture(autouse=True)
def clean_bus():
    hooks.reset()
    yield
    hooks.reset()


def make(tmp_path, script, *, mode="auto", enabled=ALL_TOOLS, **kwargs):
    provider = ScriptedProvider(script=list(script))
    workspace = Workspace(tmp_path)
    todos = TodoList()
    conversation = Conversation(
        provider=provider,  # type: ignore[arg-type]
        policy=Policy(mode=mode, enabled=enabled, max_tier="destructive"),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
        session_id="sess-1",
        **kwargs,
    )
    return conversation, provider


def write_call(path: str, content: str = "written", call_id: str = "call_1"):
    return call("write_file", {"path": path, "content": content}, call_id)


# ---------------------------------------------------------------------------
# pre_tool_call
# ---------------------------------------------------------------------------


def test_a_block_stops_the_tool_from_running(tmp_path):
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "not that file"})
    conversation, _ = make(tmp_path, [turn_with(write_call("secret.txt")), "ok"])

    denied: list[str] = []
    conversation.send("go", Callbacks(on_tool_denied=lambda spec, reason: denied.append(reason)))

    assert not (tmp_path / "secret.txt").exists()
    assert denied == ["not that file"]
    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "not that file" in tool_message["content"]


def test_a_blocked_call_still_answers_its_tool_call_id(tmp_path):
    """An unanswered tool_call id makes the next request malformed."""
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "no"})
    conversation, _ = make(tmp_path, [turn_with(write_call("x.txt", call_id="abc")), "ok"])
    conversation.send("go")
    assert [m["tool_call_id"] for m in conversation.messages if m["role"] == "tool"] == ["abc"]


def test_a_modify_rewrites_what_the_tool_actually_receives(tmp_path):
    hooks.register(
        "pre_tool_call",
        lambda **kwargs: {"action": "modify", "args": {"content": "rewritten"}},
    )
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt", "original")), "done"])
    conversation.send("go")
    assert (tmp_path / "out.txt").read_text() == "rewritten"


def test_the_hook_sees_the_tool_and_its_arguments(tmp_path):
    seen: list[dict[str, Any]] = []
    hooks.register("pre_tool_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])
    conversation.send("go")

    assert seen[0]["tool_name"] == "write_file"
    assert seen[0]["args"]["path"] == "out.txt"
    assert seen[0]["session_id"] == "sess-1"
    assert seen[0]["risk_tier"] == "destructive"
    assert seen[0]["tool_call_id"] == "call_1"


def test_a_hook_does_not_fire_for_a_tool_the_policy_already_denied(tmp_path):
    """The call never happens, so there is nothing to gate — and a hook that
    fired anyway would report calls the agent cannot make."""
    seen: list[str] = []
    hooks.register("pre_tool_call", lambda **kwargs: seen.append(kwargs["tool_name"]))
    conversation, _ = make(
        tmp_path, [turn_with(write_call("out.txt")), "done"], enabled=frozenset({"read_file"})
    )
    conversation.send("go")
    assert seen == []


# ---------------------------------------------------------------------------
# the approval gate
# ---------------------------------------------------------------------------


def test_an_approve_directive_sends_an_allowed_call_to_the_gate(tmp_path):
    hooks.register(
        "pre_tool_call",
        lambda **kwargs: {"action": "approve", "message": "confirm this one"},
    )
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"], mode="auto")

    asked: list[Any] = []

    def approve(request):
        asked.append(request)
        return "once"

    conversation.send("go", Callbacks(ask_approval=approve))

    assert len(asked) == 1
    assert asked[0].reason == "confirm this one"
    assert (tmp_path / "out.txt").exists()


def test_declining_an_escalated_call_stops_it(tmp_path):
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "approve", "message": "hm"})
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"], mode="auto")
    conversation.send("go", Callbacks(ask_approval=lambda request: "no"))
    assert not (tmp_path / "out.txt").exists()


def test_an_escalation_with_no_one_watching_is_refused(tmp_path):
    """`auto` mode with no prompt attached: a hook asking for a person, and
    no person, is a refusal — never a silent approval."""
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "approve", "message": "hm"})
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"], mode="auto")
    conversation.send("go")
    assert not (tmp_path / "out.txt").exists()


def test_a_modification_is_visible_at_the_prompt(tmp_path):
    """Consent is stated. The prompt has to show what will actually run, so
    the modification must land before the question is asked."""
    hooks.register(
        "pre_tool_call",
        lambda **kwargs: {"action": "modify", "args": {"content": "rewritten"}},
    )
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt", "original")), "done"], mode="ask")

    seen: list[dict] = []
    conversation.send(
        "go",
        Callbacks(ask_approval=lambda request: (seen.append(request.arguments), "once")[1]),
    )

    assert seen[0]["content"] == "rewritten"


def test_the_approval_events_fire_around_the_prompt(tmp_path):
    order: list[str] = []
    hooks.register("pre_approval_request", lambda **kwargs: order.append("before"))
    hooks.register("post_approval_response", lambda **kwargs: order.append(f"after:{kwargs['answer']}"))

    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"], mode="ask")
    conversation.send("go", Callbacks(ask_approval=lambda request: "once"))

    assert order == ["before", "after:once"]


def test_an_approval_observer_cannot_answer_the_gate(tmp_path):
    """Observers only. A script must not be able to consent on a person's
    behalf — that is the one thing the gate exists to prevent."""
    hooks.register("pre_approval_request", lambda **kwargs: {"answer": "once"})
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"], mode="ask")
    conversation.send("go")
    assert not (tmp_path / "out.txt").exists()


# ---------------------------------------------------------------------------
# post_tool_call and transforms
# ---------------------------------------------------------------------------


def test_post_tool_call_reports_a_successful_call(tmp_path):
    seen: list[dict] = []
    hooks.register("post_tool_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])
    conversation.send("go")

    assert seen[0]["status"] == "ok"
    assert seen[0]["tool_name"] == "write_file"
    assert seen[0]["error_message"] is None
    assert seen[0]["duration_ms"] >= 0


def test_post_tool_call_reports_a_failure(tmp_path):
    seen: list[dict] = []
    hooks.register("post_tool_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(
        tmp_path, [turn_with(call("read_file", {"path": "missing.txt"})), "done"]
    )
    conversation.send("go")
    assert seen[0]["status"] == "error"
    assert seen[0]["error_message"]


def test_post_tool_call_reports_a_block(tmp_path):
    seen: list[dict] = []
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "no"})
    hooks.register("post_tool_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])
    conversation.send("go")
    assert seen[0]["status"] == "blocked"


def test_post_tool_call_reports_a_policy_denial(tmp_path):
    seen: list[dict] = []
    hooks.register("post_tool_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(
        tmp_path, [turn_with(write_call("out.txt")), "done"], enabled=frozenset({"read_file"})
    )
    conversation.send("go")
    assert seen[0]["status"] == "blocked"


def test_a_transform_changes_what_the_model_reads(tmp_path):
    hooks.register("transform_tool_result", lambda **kwargs: "[redacted]")
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])

    surface: list[str] = []
    conversation.send(
        "go", Callbacks(on_tool_result=lambda spec, result: surface.append(result.content))
    )

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert tool_message["content"] == "[redacted]"
    # The surface saw the tool's own output; the rewrite was meant for the model.
    assert surface[0] != "[redacted]"


def test_the_final_text_can_be_transformed(tmp_path):
    hooks.register("transform_llm_output", lambda **kwargs: "rewritten answer")
    conversation, _ = make(tmp_path, ["the model's answer"])
    assert conversation.send("go") == "rewritten answer"


def test_a_transformed_answer_is_not_written_back_into_the_transcript(tmp_path):
    """The model has to keep seeing what it actually said, or the next turn
    reasons from a history that never happened."""
    hooks.register("transform_llm_output", lambda **kwargs: "rewritten")
    conversation, _ = make(tmp_path, ["original"])
    conversation.send("go")
    assert conversation.messages[-1]["content"] == "original"


# ---------------------------------------------------------------------------
# model turns
# ---------------------------------------------------------------------------


def test_injected_context_reaches_the_provider(tmp_path):
    hooks.register("pre_llm_call", lambda **kwargs: {"context": "today is friday"})
    conversation, provider = make(tmp_path, ["ok"])
    conversation.send("what day is it")

    request = provider.seen[0]
    assert request[-1] == {"role": "user", "content": "today is friday"}


def test_injected_context_is_never_persisted(tmp_path):
    """It would be replayed next turn as though the user had typed it."""
    hooks.register("pre_llm_call", lambda **kwargs: {"context": "ephemeral"})
    conversation, _ = make(tmp_path, ["ok"])
    conversation.send("go")
    assert all("ephemeral" not in str(m.get("content")) for m in conversation.messages)


def test_the_system_prompt_is_untouched_by_injection(tmp_path):
    """Injecting there would change the cached prefix on every turn."""
    hooks.register("pre_llm_call", lambda **kwargs: {"context": "injected"})
    conversation, provider = make(tmp_path, ["ok"])
    before = conversation.messages[0]["content"]
    conversation.send("go")
    assert provider.seen[0][0]["content"] == before


def test_the_pre_llm_payload_names_the_turn(tmp_path):
    seen: list[dict] = []
    hooks.register("pre_llm_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, ["ok"])
    conversation.send("what day is it")

    assert seen[0]["user_message"] == "what day is it"
    assert seen[0]["model"] == "test/model"
    assert seen[0]["step"] == 0


def test_post_llm_call_reports_the_turn(tmp_path):
    seen: list[dict] = []
    hooks.register("post_llm_call", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])
    conversation.send("go")

    assert len(seen) == 2
    assert seen[0]["tool_call_count"] == 1
    assert seen[1]["tool_call_count"] == 0
    assert seen[1]["content_chars"] == len("done")


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------


def test_reset_reports_the_transcript_it_cleared(tmp_path):
    seen: list[dict] = []
    hooks.register("on_session_reset", lambda **kwargs: seen.append(kwargs))
    conversation, _ = make(tmp_path, ["one"])
    conversation.send("go")
    conversation.reset()

    assert seen[0]["session_id"] == "sess-1"
    assert seen[0]["turn_count"] == 1


def test_compaction_reports_what_it_cost(tmp_path):
    seen: list[dict] = []
    hooks.register("on_compaction", lambda **kwargs: seen.append(kwargs))

    conversation, _ = make(tmp_path, ["done"], context_window=600)
    conversation.messages.extend(
        {"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 400}
        for index in range(6)
    )
    conversation.send("go")

    assert seen and seen[0]["stage"] in {"prune", "summarise"}
    assert seen[0]["before_tokens"] > seen[0]["after_tokens"]


def test_nothing_fires_when_no_hook_is_registered(tmp_path):
    """The cheap path. Every fire site checks first, so an install with no
    hooks pays one dict lookup per boundary."""
    conversation, _ = make(tmp_path, [turn_with(write_call("out.txt")), "done"])
    assert conversation.send("go") == "done"
    assert (tmp_path / "out.txt").exists()
