"""The hook bus: vocabulary, dispatch, and the aggregators."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from andromeda_agent import hooks

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_bus():
    hooks.reset()
    yield
    hooks.reset()


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


# Events fired through an aggregator rather than by name, so the string never
# appears at the call site. The aggregator's own name is what to look for.
AGGREGATED = {
    "pre_llm_call": "injected_context(",
    "pre_tool_call": "pre_tool_directive(",
}


def test_every_event_is_fired_somewhere():
    """An event nobody fires is a setting that silently does nothing.

    The docstring in `hooks.py` promises each name has a real fire site; this
    is what keeps that true when someone adds the next one.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for package in ("andromeda_agent", "andromeda_cli", "andromeda_tui")
        for path in (PACKAGE_ROOT / package).rglob("*.py")
        if path.name not in {"hooks.py", "shell_hooks.py", "hooks_cmd.py"}
    )
    missing = [
        event
        for event in sorted(hooks.VALID_HOOKS)
        if f'"{event}"' not in sources and AGGREGATED.get(event, "\0") not in sources
    ]
    assert missing == [], f"declared but never fired: {missing}"


def test_the_blocking_and_transform_sets_are_subsets_of_the_vocabulary():
    assert hooks.BLOCKING_EVENTS <= hooks.VALID_HOOKS
    assert hooks.TRANSFORM_EVENTS <= hooks.VALID_HOOKS
    assert hooks.TOOL_SCOPED_EVENTS <= hooks.VALID_HOOKS
    assert hooks.SHELL_UNSUPPORTED_HOOKS <= hooks.VALID_HOOKS


def test_registering_an_unknown_event_is_refused():
    with pytest.raises(hooks.HookError):
        hooks.register("on_full_moon", lambda: None)


def test_registering_a_non_callable_is_refused():
    with pytest.raises(hooks.HookError):
        hooks.register("on_session_start", "not a function")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_a_raising_callback_does_not_stop_the_ones_after_it():
    seen: list[str] = []

    def broken(**_kwargs):
        raise RuntimeError("boom")

    hooks.register("on_session_start", broken)
    hooks.register("on_session_start", lambda **kwargs: seen.append("ran"))

    hooks.invoke_hook("on_session_start", session_id="s")

    assert seen == ["ran"]


def test_a_narrow_signature_is_handed_only_what_it_declares():
    """Payloads grow. A callback written against two kwargs must not start
    failing the day a third is added."""
    seen: list[dict] = []

    def narrow(session_id: str):
        seen.append({"session_id": session_id})

    hooks.register("on_session_start", narrow)
    hooks.invoke_hook("on_session_start", session_id="s", model="m", surface="repl")

    assert seen == [{"session_id": "s"}]


def test_a_kwargs_callback_receives_the_whole_payload():
    seen: list[dict] = []
    hooks.register("on_session_start", lambda **kwargs: seen.append(kwargs))
    hooks.invoke_hook("on_session_start", session_id="s", model="m")
    assert seen == [{"session_id": "s", "model": "m"}]


def test_a_callback_with_no_introspectable_signature_still_runs():
    """Some callables refuse introspection. That is a reason to hand them the
    whole payload, not a reason to skip them."""
    seen: list[dict] = []

    class Opaque:
        @property
        def __signature__(self):
            raise ValueError("no signature here")

        def __call__(self, **kwargs):
            seen.append(kwargs)

    opaque = Opaque()
    with pytest.raises(ValueError):
        inspect.signature(opaque)

    hooks.register("on_session_start", opaque)
    hooks.invoke_hook("on_session_start", session_id="s", model="m")

    assert seen == [{"session_id": "s", "model": "m"}]


def test_has_hook_is_false_until_something_registers():
    assert hooks.has_hook("post_tool_call") is False
    hooks.register("post_tool_call", lambda **kwargs: None)
    assert hooks.has_hook("post_tool_call") is True


def test_fire_does_nothing_when_nothing_listens():
    # No exception, no payload built. The cheap path on every tool call.
    hooks.fire("post_tool_call", tool_name="terminal")


def test_callbacks_run_in_registration_order():
    order: list[int] = []
    for index in range(3):
        hooks.register("on_session_start", lambda index=index, **kwargs: order.append(index))
    hooks.invoke_hook("on_session_start", session_id="s")
    assert order == [0, 1, 2]


# ---------------------------------------------------------------------------
# pre_tool_call directives
# ---------------------------------------------------------------------------


def test_no_hooks_means_no_directive():
    assert hooks.pre_tool_directive("terminal", {"command": "ls"}) == hooks.Directive()


def test_a_block_directive_wins():
    hooks.register(
        "pre_tool_call", lambda **kwargs: {"action": "block", "message": "nope"}
    )
    directive = hooks.pre_tool_directive("terminal", {"command": "ls"})
    assert directive.action == "block"
    assert directive.message == "nope"


def test_a_block_without_a_message_is_ignored():
    """The message becomes the tool result the model reads. A silent block
    would look to the model like the tool returned nothing."""
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block"})
    assert hooks.pre_tool_directive("terminal", {}).action is None


def test_the_first_directive_wins():
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "first"})
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "second"})
    assert hooks.pre_tool_directive("terminal", {}).message == "first"


def test_an_observer_returning_a_dict_is_not_read_as_a_veto():
    hooks.register("pre_tool_call", lambda **kwargs: {"logged": True})
    assert hooks.pre_tool_directive("terminal", {}).action is None


def test_modify_directives_accumulate_over_the_original_args():
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "modify", "args": {"a": 1}})
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "modify", "args": {"b": 2}})
    directive = hooks.pre_tool_directive("terminal", {"a": 0, "c": 3})
    assert directive.modified_args == {"a": 1, "b": 2, "c": 3}


def test_a_modification_survives_a_later_block():
    """So a caller that logs the directive sees what each hook wanted, not
    only what the last one decided."""
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "modify", "args": {"a": 1}})
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "block", "message": "no"})
    directive = hooks.pre_tool_directive("terminal", {})
    assert directive.action == "block"
    assert directive.modified_args == {"a": 1}


def test_an_empty_modify_changes_nothing():
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "modify", "args": {}})
    assert hooks.pre_tool_directive("terminal", {"a": 1}).modified_args is None


def test_an_approve_directive_carries_its_rule_key():
    hooks.register(
        "pre_tool_call",
        lambda **kwargs: {
            "action": "approve",
            "message": "confirm",
            "rule_key": " terminal:push ",
        },
    )
    directive = hooks.pre_tool_directive("terminal", {})
    assert (directive.action, directive.message, directive.rule_key) == (
        "approve",
        "confirm",
        "terminal:push",
    )


def test_an_approve_may_be_silent():
    hooks.register("pre_tool_call", lambda **kwargs: {"action": "approve"})
    assert hooks.pre_tool_directive("terminal", {}).action == "approve"


def test_the_hook_sees_a_copy_of_the_arguments():
    """A hook that mutates its payload must not reach the real call."""
    seen: list[dict] = []

    def grabby(**kwargs):
        kwargs["args"]["command"] = "rm -rf /"
        seen.append(kwargs["args"])

    hooks.register("pre_tool_call", grabby)
    args = {"command": "ls"}
    hooks.pre_tool_directive("terminal", args)
    assert args == {"command": "ls"}
    assert seen[0]["command"] == "rm -rf /"


# ---------------------------------------------------------------------------
# injected context
# ---------------------------------------------------------------------------


def test_context_is_joined_in_order():
    hooks.register("pre_llm_call", lambda **kwargs: {"context": "first"})
    hooks.register("pre_llm_call", lambda **kwargs: "second")
    assert hooks.injected_context(session_id="s") == "first\n\nsecond"


def test_blank_context_contributes_nothing():
    hooks.register("pre_llm_call", lambda **kwargs: {"context": "   "})
    hooks.register("pre_llm_call", lambda **kwargs: {"unrelated": True})
    assert hooks.injected_context(session_id="s") == ""


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------


def test_a_transform_replaces_the_text():
    hooks.register("transform_llm_output", lambda **kwargs: "replaced")
    assert hooks.transform("transform_llm_output", "original") == "replaced"


def test_the_dict_form_of_a_transform_is_accepted():
    hooks.register("transform_tool_result", lambda **kwargs: {"output": "replaced"})
    assert hooks.transform("transform_tool_result", "original") == "replaced"


def test_the_first_replacement_wins_and_the_second_is_reported(caplog):
    hooks.register("transform_llm_output", lambda **kwargs: "first")
    hooks.register("transform_llm_output", lambda **kwargs: "second")
    with caplog.at_level("WARNING"):
        assert hooks.transform("transform_llm_output", "original") == "first"
    assert "more than one replacement" in caplog.text


def test_an_empty_replacement_leaves_the_text_alone():
    hooks.register("transform_llm_output", lambda **kwargs: "   ")
    assert hooks.transform("transform_llm_output", "original") == "original"


def test_a_transform_receives_the_text_it_may_replace():
    seen: list[str] = []
    hooks.register("transform_llm_output", lambda text, **kwargs: seen.append(text))
    hooks.transform("transform_llm_output", "original")
    assert seen == ["original"]


def test_transform_refuses_a_non_transform_event():
    with pytest.raises(hooks.HookError):
        hooks.transform("post_tool_call", "text")


def test_describe_counts_what_is_registered():
    hooks.register("on_session_start", lambda **kwargs: None)
    counts = dict(hooks.describe())
    assert counts["on_session_start"] == 1
    assert counts["on_session_end"] == 0
