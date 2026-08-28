"""Middleware: the four kinds, their fire sites, and the nesting order."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from andromeda_agent import hooks, middleware
from andromeda_agent import plugin_capabilities as caps
from andromeda_agent import plugin_store, plugins

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_state():
    plugins.reset()
    hooks.reset()
    yield
    plugins.reset()
    hooks.reset()


def load(tmp_path: Path, plugin_id: str, body: str):
    directory = tmp_path / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(
        f"name: {plugin_id}\nversion: 1.0.0\ncapabilities: [runtime.middleware]\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
    caps.grant(plugin_id, ["runtime.middleware"])
    manifest = plugins.read_manifest(directory, "user")
    plugin_store.update(plugin_id, enabled=True)
    plugins.load({plugin_id: manifest})
    entry = plugins.manager().loaded[plugin_id]
    assert entry.ok, entry.error
    return entry


# ---------------------------------------------------------------------------
# the vocabulary is not inert
# ---------------------------------------------------------------------------


def test_every_kind_has_a_real_fire_site():
    """The rule `hooks.py` opens with, applied here. A middleware kind that is
    registrable and never invoked is a setting that silently does nothing."""
    source = (PACKAGE_ROOT / "andromeda_agent" / "loop.py").read_text(encoding="utf-8")
    for kind in sorted(middleware.VALID_KINDS):
        constant = f"middleware.{kind.upper()}"
        assert constant in source, f"{kind} has no fire site in loop.py"


def test_the_two_families_do_not_overlap():
    assert not (middleware.REQUEST_KINDS & middleware.EXECUTION_KINDS)
    assert middleware.REQUEST_KINDS | middleware.EXECUTION_KINDS == middleware.VALID_KINDS


def test_registering_needs_the_capability(tmp_path):
    directory = tmp_path / "nope"
    directory.mkdir()
    (directory / "plugin.yaml").write_text("name: nope\n", encoding="utf-8")
    (directory / "__init__.py").write_text(
        'def register(ctx):\n    ctx.register_middleware("tool_request", lambda p: p)\n',
        encoding="utf-8",
    )
    manifest = plugins.read_manifest(directory, "user")
    plugin_store.update("nope", enabled=True)
    plugins.load({"nope": manifest})
    assert "runtime.middleware" in plugins.manager().loaded["nope"].error


def test_an_unknown_kind_is_refused(tmp_path):
    directory = tmp_path / "typo"
    directory.mkdir()
    (directory / "plugin.yaml").write_text(
        "name: typo\ncapabilities: [runtime.middleware]\n", encoding="utf-8"
    )
    (directory / "__init__.py").write_text(
        'def register(ctx):\n    ctx.register_middleware("tool_reqest", lambda p: p)\n',
        encoding="utf-8",
    )
    caps.grant("typo", ["runtime.middleware"])
    manifest = plugins.read_manifest(directory, "user")
    plugin_store.update("typo", enabled=True)
    plugins.load({"typo": manifest})
    assert "unknown middleware kind" in plugins.manager().loaded["typo"].error


# ---------------------------------------------------------------------------
# request middleware
# ---------------------------------------------------------------------------


def test_a_request_chain_composes(tmp_path):
    """Each link sees the previous link's output, so two can both add a field.
    First-writer-wins would make the second one pointless."""
    load(
        tmp_path,
        "adder",
        """
        def one(payload):
            payload["seen"] = payload.get("seen", []) + ["one"]
            return payload

        def two(payload):
            payload["seen"] = payload.get("seen", []) + ["two"]
            return payload

        def register(ctx):
            ctx.register_middleware("tool_request", one)
            ctx.register_middleware("tool_request", two)
        """,
    )
    result = middleware.apply_request("tool_request", {"args": {}})
    assert result["seen"] == ["one", "two"]


def test_returning_none_leaves_the_payload_alone(tmp_path):
    load(
        tmp_path,
        "shy",
        """
        def register(ctx):
            ctx.register_middleware("tool_request", lambda payload: None)
        """,
    )
    assert middleware.apply_request("tool_request", {"a": 1}) == {"a": 1}


def test_a_raising_request_link_is_skipped(tmp_path, caplog):
    """A plugin that cannot rewrite a request must not be able to erase one."""
    load(
        tmp_path,
        "broken",
        """
        def boom(payload):
            raise RuntimeError("nope")

        def fine(payload):
            payload["kept"] = True
            return payload

        def register(ctx):
            ctx.register_middleware("tool_request", boom)
            ctx.register_middleware("tool_request", fine)
        """,
    )
    result = middleware.apply_request("tool_request", {"a": 1})
    assert result == {"a": 1, "kept": True}
    assert "nope" in caplog.text


def test_a_non_dict_return_is_ignored(tmp_path, caplog):
    load(
        tmp_path,
        "wrong",
        """
        def register(ctx):
            ctx.register_middleware("llm_request", lambda payload: "not a dict")
        """,
    )
    assert middleware.apply_request("llm_request", {"a": 1}) == {"a": 1}
    assert "not a dict" in caplog.text


def test_an_execution_kind_is_refused_by_apply_request():
    with pytest.raises(middleware.MiddlewareError):
        middleware.apply_request("tool_execution", {})


# ---------------------------------------------------------------------------
# execution middleware
# ---------------------------------------------------------------------------


def test_no_middleware_calls_straight_through():
    assert middleware.apply_execution("tool_execution", lambda: "value", {}) == "value"


def test_the_first_registered_is_outermost(tmp_path):
    load(
        tmp_path,
        "nester",
        """
        ORDER = []

        def outer(call, context):
            ORDER.append("outer-in")
            result = call()
            ORDER.append("outer-out")
            return result

        def inner(call, context):
            ORDER.append("inner-in")
            result = call()
            ORDER.append("inner-out")
            return result

        def register(ctx):
            ctx.register_middleware("tool_execution", outer)
            ctx.register_middleware("tool_execution", inner)
        """,
    )
    module = plugins.manager().loaded["nester"].module
    middleware.apply_execution("tool_execution", lambda: "x", {})
    assert module.ORDER == ["outer-in", "inner-in", "inner-out", "outer-out"]


def test_a_middleware_may_answer_without_calling(tmp_path):
    """A cache hit is a legitimate reason not to run the real thing."""
    load(
        tmp_path,
        "cacher",
        """
        def register(ctx):
            ctx.register_middleware("tool_execution", lambda call, context: "cached")
        """,
    )
    ran = []
    result = middleware.apply_execution(
        "tool_execution", lambda: ran.append(1) or "real", {}
    )
    assert result == "cached"
    assert ran == []


def test_a_middleware_may_retry(tmp_path):
    load(
        tmp_path,
        "retrier",
        """
        def retry(call, context):
            try:
                return call()
            except Exception:
                return call()

        def register(ctx):
            ctx.register_middleware("tool_execution", retry)
        """,
    )
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")
        return "second"

    assert middleware.apply_execution("tool_execution", flaky, {}) == "second"
    assert len(attempts) == 2


def test_the_real_calls_failure_is_not_mistaken_for_a_broken_link(tmp_path):
    """Without the distinction, a tool that legitimately raised would be
    retried by every link, each believing the one beneath it was at fault."""
    load(
        tmp_path,
        "passthrough",
        """
        def register(ctx):
            ctx.register_middleware("tool_execution", lambda call, context: call())
            ctx.register_middleware("tool_execution", lambda call, context: call())
        """,
    )
    attempts = []

    def always_fails():
        attempts.append(1)
        raise ValueError("the tool itself failed")

    with pytest.raises(ValueError, match="the tool itself failed"):
        middleware.apply_execution("tool_execution", always_fails, {})
    assert len(attempts) == 1


def test_a_link_that_raises_before_calling_is_stepped_over(tmp_path, caplog):
    load(
        tmp_path,
        "broken",
        """
        def boom(call, context):
            raise RuntimeError("nope")

        def register(ctx):
            ctx.register_middleware("tool_execution", boom)
        """,
    )
    assert middleware.apply_execution("tool_execution", lambda: "real", {}) == "real"
    assert "nope" in caplog.text


def test_a_request_kind_is_refused_by_apply_execution():
    with pytest.raises(middleware.MiddlewareError):
        middleware.apply_execution("tool_request", lambda: None, {})


def test_each_link_gets_its_own_copy_of_the_context(tmp_path):
    load(
        tmp_path,
        "mutator",
        """
        SEEN = []

        def first(call, context):
            context["mine"] = True
            return call()

        def second(call, context):
            SEEN.append(dict(context))
            return call()

        def register(ctx):
            ctx.register_middleware("tool_execution", first)
            ctx.register_middleware("tool_execution", second)
        """,
    )
    module = plugins.manager().loaded["mutator"].module
    middleware.apply_execution("tool_execution", lambda: None, {"tool_name": "x"})
    assert module.SEEN == [{"tool_name": "x"}]


# ---------------------------------------------------------------------------
# the fire sites, driven for real
# ---------------------------------------------------------------------------


def _conversation(tmp_path, script):
    from andromeda_agent.approval import Policy
    from andromeda_agent.loop import Conversation
    from andromeda_tools.workspace import Workspace

    from tests.support import ScriptedProvider

    return Conversation(
        provider=ScriptedProvider(script=list(script)),
        policy=Policy(mode="auto", enabled=frozenset({"read_file", "list_dir"})),
        workspace=Workspace(str(tmp_path)),
    )


def test_tool_request_rewrites_the_arguments(tmp_path):
    from andromeda_agent.providers.base import AssistantTurn, ToolCall

    (tmp_path / "right.txt").write_text("the right file", encoding="utf-8")
    (tmp_path / "wrong.txt").write_text("the wrong file", encoding="utf-8")

    load(
        tmp_path,
        "redirect",
        """
        def swap(payload):
            args = dict(payload.get("args") or {})
            if args.get("path") == "wrong.txt":
                args["path"] = "right.txt"
            payload["args"] = args
            return payload

        def register(ctx):
            ctx.register_middleware("tool_request", swap)
        """,
    )

    conversation = _conversation(
        tmp_path,
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "wrong.txt"})]
            ),
            "done",
        ],
    )
    conversation.send("read it")
    transcript = "\n".join(str(message.get("content")) for message in conversation.messages)
    assert "the right file" in transcript


def test_tool_execution_can_answer_without_running_the_tool(tmp_path):
    from andromeda_agent.providers.base import AssistantTurn, ToolCall

    load(
        tmp_path,
        "cacher",
        """
        from andromeda_tools.spec import ToolResult

        def register(ctx):
            ctx.register_middleware(
                "tool_execution",
                lambda call, context: ToolResult(content="from the cache"),
            )
        """,
    )
    conversation = _conversation(
        tmp_path,
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "absent.txt"})]
            ),
            "done",
        ],
    )
    conversation.send("read it")
    transcript = "\n".join(str(message.get("content")) for message in conversation.messages)
    assert "from the cache" in transcript


def test_a_tool_execution_middleware_returning_the_wrong_shape_is_reported(tmp_path):
    """It would otherwise reach the transcript as a repr."""
    from andromeda_agent.providers.base import AssistantTurn, ToolCall

    load(
        tmp_path,
        "wrong",
        """
        def register(ctx):
            ctx.register_middleware("tool_execution", lambda call, context: 42)
        """,
    )
    conversation = _conversation(
        tmp_path,
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="list_dir", arguments={"path": "."})]
            ),
            "done",
        ],
    )
    conversation.send("look")
    transcript = "\n".join(str(message.get("content")) for message in conversation.messages)
    assert "not a ToolResult" in transcript


def test_llm_request_can_change_the_temperature(tmp_path):
    load(
        tmp_path,
        "cooler",
        """
        SEEN = []

        def cool(payload):
            SEEN.append(payload.get("temperature"))
            payload["temperature"] = 0.0
            return payload

        def register(ctx):
            ctx.register_middleware("llm_request", cool)
        """,
    )
    conversation = _conversation(tmp_path, ["hello"])
    conversation.temperature = 0.7
    conversation.send("hi")
    assert plugins.manager().loaded["cooler"].module.SEEN == [0.7]


def test_llm_execution_wraps_the_provider_call(tmp_path):
    load(
        tmp_path,
        "counter",
        """
        CALLS = []

        def count(call, context):
            CALLS.append(context.get("model"))
            return call()

        def register(ctx):
            ctx.register_middleware("llm_execution", count)
        """,
    )
    conversation = _conversation(tmp_path, ["hello"])
    assert conversation.send("hi") == "hello"
    assert plugins.manager().loaded["counter"].module.CALLS == ["test/model"]


def test_an_llm_execution_returning_the_wrong_shape_raises_a_clear_error(tmp_path):
    from andromeda_agent.errors import AgentError

    load(
        tmp_path,
        "wrong",
        """
        def register(ctx):
            ctx.register_middleware("llm_execution", lambda call, context: "text")
        """,
    )
    conversation = _conversation(tmp_path, ["hello"])
    with pytest.raises(AgentError, match="not an AssistantTurn"):
        conversation.send("hi")


def test_no_middleware_means_no_payload_is_built():
    """Every fire site checks `has` first, so an install with no plugins pays
    one tuple lookup per boundary."""
    source = (PACKAGE_ROOT / "andromeda_agent" / "loop.py").read_text(encoding="utf-8")
    for kind in sorted(middleware.VALID_KINDS):
        guard = f"middleware.has(middleware.{kind.upper()})"
        assert guard in source, f"{kind} is fired without a `has` guard"
