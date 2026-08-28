"""Wrapping what happens, rather than watching it.

A hook is told what happened and may veto. **Middleware changes what happens**:
it rewrites the request on its way in, or wraps the call itself so it can retry
it, cache it, time it, or rewrite the answer on its way back.

Four kinds, and every one has a real fire site in `loop.py`. That is the rule
`hooks.py` opens with and it applies here identically — a name registered ahead
of the code that runs it is a setting that silently does nothing.

    tool_request     the arguments, before a tool runs
    tool_execution   the tool call itself, as a callable to invoke or not
    llm_request      the payload, before it goes to the provider
    llm_execution    the provider call itself

Request vs execution
--------------------
A **request** middleware is handed a payload and returns a replacement, or
None to leave it alone. They chain: each sees what the previous one produced,
so two can both add a header. First-writer-does-not-win here, because the
whole point is composition.

An **execution** middleware is handed `call`, a zero-argument callable, and is
responsible for invoking it. It may call it twice (a retry), not at all (a
cache hit), or wrap the result. They nest: the first registered is outermost,
so it sees the others' retries as one call.

    outermost ─┐
               │  first registered
               │  ┌─ second registered
               │  │  ┌─ the real call
               │  │  └─
               │  └─
               └─

Why an execution middleware is the dangerous one
------------------------------------------------
It receives every tool result and every model turn, and it decides whether the
real thing runs at all. That is the same reach as replacing `terminal`, so it
sits behind `runtime.middleware` — one capability for all four kinds, because
holding one of these and not the others is not a meaningful distinction.

Failure
-------
A middleware that raises is logged and skipped, and the chain continues with
what it had before that link. The one thing that is never swallowed is an
exception from `call` itself: that is the real failure, and hiding it would
turn a provider outage into a silent empty answer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

TOOL_REQUEST = "tool_request"
TOOL_EXECUTION = "tool_execution"
LLM_REQUEST = "llm_request"
LLM_EXECUTION = "llm_execution"

#: Kinds a plugin may register. `tests/test_plugin_middleware.py` asserts each
#: one appears in the source of the module that fires it.
VALID_KINDS: frozenset[str] = frozenset(
    {TOOL_REQUEST, TOOL_EXECUTION, LLM_REQUEST, LLM_EXECUTION}
)

REQUEST_KINDS: frozenset[str] = frozenset({TOOL_REQUEST, LLM_REQUEST})
EXECUTION_KINDS: frozenset[str] = frozenset({TOOL_EXECUTION, LLM_EXECUTION})

#: Bumped when the payload shape changes, so a middleware written against an
#: older one can refuse rather than misread it.
SCHEMA_VERSION = "andromeda.middleware.v1"


class MiddlewareError(ValueError):
    pass


def _registered(kind: str) -> tuple[Callable[..., Any], ...]:
    """Middleware for `kind`, in registration order. Never raises."""
    try:
        from . import plugins as plugins_module

        return plugins_module.middleware_for(kind)
    except Exception:  # noqa: BLE001 - the loop must not depend on plugins working
        return ()


def has(kind: str) -> bool:
    """Whether anything is registered.

    Every fire site checks this first, so an install with no plugins pays one
    tuple lookup per boundary and never builds a payload it discards.
    """
    return bool(_registered(kind))


def apply_request(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the request chain over `payload`; return what came out.

    Each link sees the previous link's output. A link that returns None, a
    non-dict, or raises leaves the payload as it was and the chain continues —
    a plugin that cannot rewrite a request must not be able to erase one.
    """
    if kind not in REQUEST_KINDS:  # pragma: no cover - programming error
        raise MiddlewareError(f"{kind!r} is not a request middleware")

    current = payload
    for callback in _registered(kind):
        try:
            produced = callback(dict(current))
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("%s middleware raised: %s", kind, exc)
            continue
        if isinstance(produced, dict):
            current = produced
        elif produced is not None:
            logger.warning(
                "%s middleware returned %s, not a dict; ignoring",
                kind,
                type(produced).__name__,
            )
    return current


def apply_execution(kind: str, call: Callable[[], Any], context: dict[str, Any]) -> Any:
    """Run `call` through the execution chain and return its result.

    Nested so the first registered is outermost. A link that raises *before*
    reaching the inner call is skipped and the chain continues without it; an
    exception from the real call propagates unchanged, because that is the
    actual failure and swallowing it would turn a provider outage into a
    silent empty answer.
    """
    if kind not in EXECUTION_KINDS:  # pragma: no cover - programming error
        raise MiddlewareError(f"{kind!r} is not an execution middleware")

    registered = _registered(kind)
    if not registered:
        return call()

    def innermost() -> Any:
        # Wrapped so the per-link `except` below can tell "the real call
        # failed" from "this middleware is broken". Without the distinction, a
        # tool that legitimately raised would be retried by every link in the
        # chain, each one believing the link beneath it was at fault.
        try:
            return call()
        except Exception as exc:
            raise _CallFailed(exc) from exc

    def wrap(index: int, inner: Callable[[], Any]) -> Callable[[], Any]:
        callback = registered[index]

        def invoke() -> Any:
            try:
                return callback(inner, dict(context))
            except _CallFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - see the docstring
                logger.warning("%s middleware raised: %s", kind, exc)
                return inner()

        return invoke

    chain: Callable[[], Any] = innermost
    for index in range(len(registered) - 1, -1, -1):
        chain = wrap(index, chain)

    try:
        return chain()
    except _CallFailed as wrapper:
        raise wrapper.cause from None


class _CallFailed(Exception):
    """The real call raised. Carried past the per-link `except`."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def payload(**fields: Any) -> dict[str, Any]:
    """A middleware payload, stamped with the schema version."""
    fields.setdefault("schema_version", SCHEMA_VERSION)
    return fields
