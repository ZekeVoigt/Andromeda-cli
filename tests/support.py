"""Test doubles shared across the suite."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generator

from andromeda_agent.providers.base import AssistantTurn, ToolCall


@dataclass
class ScriptedProvider:
    """A provider that replays a fixed list of turns.

    Each entry is either a string (plain text) or an AssistantTurn. The loop
    pulls one per model call, so a script of [tool_call, text] exercises a full
    call-and-respond exchange.
    """

    script: list[Any] = field(default_factory=list)
    name: str = "fake"
    model: str = "test/model"
    label: str = "Fake"
    # Real providers carry this and surfaces read it — the REPL banner and the
    # TUI status bar both do. A double without it fails at render time rather
    # than at the thing under test.
    thinking: str = "off"
    seen: list[list[dict[str, Any]]] = field(default_factory=list)
    seen_tools: list[list[dict[str, Any]] | None] = field(default_factory=list)
    raises: Exception | None = None
    # Real providers carry an OpenAI client, which the auxiliary model borrows.
    # None here, so a scripted session has no vision — which is the honest
    # answer for a double that cannot make a side call.
    client: Any = None

    def stream_turn(
        self, messages, *, max_tokens, temperature, tools=None
    ) -> Generator[str, None, AssistantTurn]:
        self.seen.append([dict(m) for m in messages])
        self.seen_tools.append(tools)

        if self.raises is not None:
            raise self.raises

        step = self.script.pop(0) if self.script else ""
        turn = AssistantTurn(content=step) if isinstance(step, str) else step

        if turn.content:
            yield turn.content
        return turn


def call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ToolCall:
    import json

    return ToolCall(
        id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
    )


def turn_with(*calls: ToolCall, content: str = "") -> AssistantTurn:
    return AssistantTurn(content=content, tool_calls=list(calls))
