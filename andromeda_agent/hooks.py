"""The hook bus.

A hook is a callback the harness fires at a named lifecycle boundary. Every
event below has a *real fire site* in this codebase — there is no inert
vocabulary registered ahead of the code that fires it, because an event that
looks configurable and never fires is worse than one that does not exist: it
fails silently, at the moment someone is relying on it.

Three kinds of event, and the difference is the whole design:

**Observers** are told what happened and cannot change it. Their return value
is discarded. `on_session_start`, `post_tool_call`, `on_job_end`.

**Directives** can change what happens next. Only `pre_tool_call` carries one,
and only three shapes are honoured — `block`, `approve`, `modify`. Keeping the
blocking surface to a single event is deliberate: every place a hook can veto
is a place a broken hook can wedge the agent.

**Transforms** replace a value on its way past. `transform_tool_result` and
`transform_llm_output` take text and hand back text; the first callback to
return a non-empty string wins, and the rest are reported rather than silently
skipped.

Failures are isolated per callback. A hook that raises is logged and
contributes nothing — one bad callback can never break the loop it was
watching. The one exception is a `fail_closed` shell hook on `pre_tool_call`,
which is the point of `fail_closed`: see `shell_hooks`.

This module owns the vocabulary and the dispatch. Where the callbacks come
from is somebody else's problem — today it is `shell_hooks`, reading the
`hooks:` block of the config.
"""

from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# Every event, with the site that fires it. Keep this list and the fire sites
# in step: `tests/test_hooks.py` asserts that each name here appears in the
# source of the module that fires it.
VALID_HOOKS: frozenset[str] = frozenset(
    {
        # --- tool dispatch (andromeda_agent/loop.py) -----------------------
        # Before a tool runs, after the policy allowed it. The one event that
        # can return a directive: block / approve / modify.
        #   kwargs: tool_name, args, session_id, tool_call_id, risk_tier, step
        "pre_tool_call",
        # After a tool ran, whatever the outcome.
        #   kwargs: the above, plus result, status ("ok"|"error"|"blocked"),
        #   error_message, duration_ms
        "post_tool_call",
        # The tool's text on its way back to the model. Return replacement
        # text to rewrite what the model sees.
        #   kwargs: as post_tool_call
        "transform_tool_result",
        # --- model turns (andromeda_agent/loop.py) -------------------------
        # Before a request goes to the provider. May return context to inject
        # into the *user* message for this turn only — never the system
        # prompt, which would break the cached prefix, and never persisted.
        #   kwargs: session_id, model, message_count, step, user_message
        "pre_llm_call",
        # After a turn comes back.
        #   kwargs: session_id, model, step, content_chars, tool_call_count
        "post_llm_call",
        # The assistant's final text for an exchange, on its way to the
        # surface. Return replacement text to rewrite it.
        #   kwargs: session_id, model, text, steps_taken
        "transform_llm_output",
        # --- session lifecycle (andromeda_cli/repl.py, session.py) ---------
        #   kwargs: session_id, model, surface ("repl"|"tui"|"once"|"cron")
        "on_session_start",
        #   kwargs: session_id, model, surface, turn_count, completed
        "on_session_end",
        # `/new` — the transcript was cleared, the session lives on.
        #   kwargs: session_id, model, surface, turn_count
        "on_session_reset",
        # --- compaction (andromeda_agent/loop.py) --------------------------
        # Fired after the transcript was shortened, with what it cost.
        #   kwargs: session_id, stage ("prune"|"summarise"), before_tokens,
        #   after_tokens, pruned_results, summarised_messages
        "on_compaction",
        # --- the approval gate (andromeda_agent/loop.py) -------------------
        # Observers only, both of them. A hook cannot pre-answer or veto an
        # approval from here — blocking a tool is `pre_tool_call`'s job, and
        # letting a hook answer the gate would put a script between a person
        # and their own consent.
        #   kwargs: tool_name, summary, risk_tier, session_id, surface
        "pre_approval_request",
        #   kwargs: the above, plus answer
        #   ("once"|"session"|"always"|"never"|"no")
        "post_approval_response",
        # --- delegated lanes (andromeda_agent/delegation.py) ---------------
        #   kwargs: parent_session_id, specialist_id, run_id, task
        "subagent_start",
        #   kwargs: the above, plus status, summary, tool_call_history,
        #   duration_ms
        "subagent_stop",
        # --- slash commands (andromeda_cli/repl.py) ------------------------
        # Observer. Deliberately NOT able to block: /stop and /exit are the
        # escape hatches, and a hook that can veto them is a hook that can
        # take the terminal away from its owner.
        #   kwargs: surface, command, args_raw
        "pre_command",
        # --- scheduled jobs (andromeda_agent/runner.py) --------------------
        #   kwargs: job_id, job_name, kind ("agent"|"script"), scheduled_for
        "on_job_start",
        #   kwargs: the above, plus status, duration_ms, output_chars
        "on_job_end",
    }
)

# Events whose return value has no channel in the shell-hook wire protocol.
# `VALID_HOOKS` doubles as the config allow-list, so without this an entry
# could register and have its output silently dropped — registration is
# refused loudly instead. Empty today: every event above either takes a
# directive, takes a transform, or is an observer whose return is discarded
# by design.
SHELL_UNSUPPORTED_HOOKS: frozenset[str] = frozenset()

# Events whose block directive is honoured downstream. Exit-code-2 blocking
# and `fail_closed` only mean anything for these.
BLOCKING_EVENTS: frozenset[str] = frozenset({"pre_tool_call"})

# Events that replace a string rather than observe one.
TRANSFORM_EVENTS: frozenset[str] = frozenset(
    {"transform_tool_result", "transform_llm_output"}
)

# Events scoped to one tool, where a `matcher` is honoured.
TOOL_SCOPED_EVENTS: frozenset[str] = frozenset({"pre_tool_call", "post_tool_call", "transform_tool_result"})


class HookError(ValueError):
    pass


@dataclass(frozen=True)
class Directive:
    """What `pre_tool_call` hooks decided about one call."""

    action: str | None = None  # "block" | "approve" | None
    message: str | None = None
    rule_key: str | None = None
    modified_args: dict[str, Any] | None = None


class HookManager:
    """Callbacks by event, in registration order.

    Order is the tie-break everywhere below — the first directive wins, the
    first transform wins — so registration order is part of the contract and
    is never sorted or deduplicated.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable[..., Any]]] = {}
        self._lock = threading.RLock()

    def register(self, event: str, callback: Callable[..., Any]) -> None:
        if event not in VALID_HOOKS:
            raise HookError(f"unknown hook event {event!r}")
        if not callable(callback):
            raise HookError("hook callback must be callable")
        with self._lock:
            self._hooks.setdefault(event, []).append(callback)

    def callbacks(self, event: str) -> tuple[Callable[..., Any], ...]:
        with self._lock:
            return tuple(self._hooks.get(event, ()))

    def has(self, event: str) -> bool:
        """Whether anything is listening.

        Every fire site checks this first, so an install with no hooks
        configured pays one dict lookup per boundary and never builds a
        payload it is about to throw away.
        """
        with self._lock:
            return bool(self._hooks.get(event))

    def clear(self) -> None:
        with self._lock:
            self._hooks.clear()

    def invoke(self, event: str, **kwargs: Any) -> list[Any]:
        """Call every callback for `event`; return the non-None results.

        Each callback is isolated: one that raises is logged and skipped, and
        the ones after it still run.
        """
        results: list[Any] = []
        for callback in self.callbacks(event):
            try:
                value = _call_narrowed(callback, kwargs)
            except Exception as exc:  # noqa: BLE001 - a hook must not break the loop
                logger.warning(
                    "hook %s callback %s raised: %s",
                    event,
                    getattr(callback, "__name__", repr(callback)),
                    exc,
                )
                continue
            if value is not None:
                results.append(value)
        return results


def _call_narrowed(callback: Callable[..., Any], payload: dict[str, Any]) -> Any:
    """Call `callback` with only the payload keys it declares.

    Payloads grow over time. A callback written against three kwargs must not
    start failing the day a fourth is added, so anything that does not take
    `**kwargs` is handed the intersection instead.
    """
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        # Builtins and some C callables have no introspectable signature.
        return callback(**payload)

    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return callback(**payload)

    accepted = {
        name: value
        for name, value in payload.items()
        if name in parameters
        and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return callback(**accepted)


_manager = HookManager()


def manager() -> HookManager:
    return _manager


def register(event: str, callback: Callable[..., Any]) -> None:
    _manager.register(event, callback)


def has_hook(event: str) -> bool:
    return _manager.has(event)


def invoke_hook(event: str, **kwargs: Any) -> list[Any]:
    return _manager.invoke(event, **kwargs)


def reset() -> None:
    """Drop every registered callback. Used by tests and by a config reload."""
    _manager.clear()


# ---------------------------------------------------------------------------
# Aggregators — how each family of return value becomes one answer
# ---------------------------------------------------------------------------


def pre_tool_directive(
    tool_name: str, args: dict[str, Any] | None, **context: Any
) -> Directive:
    """Ask `pre_tool_call` hooks what should happen to this call.

    `modify` accumulates and `block`/`approve` short-circuit, in that order:
    modifications from hooks that ran before a block are still reported, so a
    caller that logs the directive sees what each hook wanted rather than only
    what the last one decided.

    The first valid `block` or `approve` wins. Malformed returns are ignored —
    an observer that happens to return a dict must not be read as a veto.
    """
    if not has_hook("pre_tool_call"):
        return Directive()

    results = invoke_hook(
        "pre_tool_call",
        tool_name=tool_name,
        args=dict(args) if isinstance(args, dict) else {},
        **context,
    )

    modified: dict[str, Any] | None = None

    for result in results:
        if not isinstance(result, dict):
            continue

        if result.get("action") == "modify":
            partial = result.get("args")
            if isinstance(partial, dict) and partial:
                if modified is None:
                    modified = dict(args) if isinstance(args, dict) else {}
                modified.update(partial)
            continue

        action = result.get("action")
        if action not in {"block", "approve"}:
            continue

        message = result.get("message")
        message = message if isinstance(message, str) and message else None
        # A block message becomes the tool result the model reads, so a block
        # without one says nothing and is discarded. An approve may be silent.
        if action == "block" and not message:
            continue

        rule_key = result.get("rule_key") if action == "approve" else None
        rule_key = rule_key.strip() if isinstance(rule_key, str) else None

        return Directive(
            action=action,
            message=message,
            rule_key=rule_key or None,
            modified_args=modified,
        )

    return Directive(modified_args=modified)


def injected_context(**context: Any) -> str:
    """Extra context for this turn, from `pre_llm_call` hooks.

    Joined in registration order. Always destined for the user message and
    never persisted: the system prompt has to stay byte-identical across turns
    or every cached prefix is thrown away, and an injection that lands in the
    transcript would be replayed on the next turn as though the user had said
    it.
    """
    if not has_hook("pre_llm_call"):
        return ""

    parts: list[str] = []
    for result in invoke_hook("pre_llm_call", **context):
        text = result if isinstance(result, str) else None
        if isinstance(result, dict):
            candidate = result.get("context")
            text = candidate if isinstance(candidate, str) else None
        if text and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def transform(event: str, text: str, **context: Any) -> str:
    """Run a transform event and return the (possibly replaced) text.

    First non-empty replacement wins. Later ones are *reported* rather than
    dropped silently — two hooks fighting over the same string is a
    misconfiguration, and the symptom otherwise is one of them appearing not
    to run at all.
    """
    if event not in TRANSFORM_EVENTS:
        raise HookError(f"{event!r} is not a transform event")
    if not has_hook(event):
        return text

    winner: str | None = None
    for result in invoke_hook(event, text=text, **context):
        replacement = result if isinstance(result, str) else None
        if isinstance(result, dict):
            candidate = result.get("output")
            replacement = candidate if isinstance(candidate, str) else None
        if replacement is None or not replacement.strip():
            continue
        if winner is None:
            winner = replacement
        else:
            logger.warning(
                "hook %s: more than one replacement offered; keeping the first",
                event,
            )
    return winner if winner is not None else text


def fire(event: str, **kwargs: Any) -> None:
    """Fire an observer event, cheaply.

    The `has_hook` check is the point: observers sit on hot paths (every tool
    call, every model turn), and building a payload for nobody is the cost
    that makes people turn a feature off.
    """
    if not has_hook(event):
        return
    invoke_hook(event, **kwargs)


def describe(events: Iterable[str] | None = None) -> list[tuple[str, int]]:
    """(event, callback count) for everything registered. Used by `doctor`."""
    names = sorted(events) if events is not None else sorted(VALID_HOOKS)
    return [(name, len(_manager.callbacks(name))) for name in names]
