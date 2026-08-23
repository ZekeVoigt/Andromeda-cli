"""The turn loop.

One user message can become many model turns: the model asks for a tool, the
tool runs, its result goes back, and the model speaks again. The loop ends when
a turn comes back with no tool calls — or when the step ceiling is hit, which is
reported rather than silently obeyed.

Every tool call passes the approval gate before it runs. Consent is established
*before* the call is made and it is *stated*: the prompt shows what will happen,
not a paraphrase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from andromeda_tools import ToolResult, ToolSpec, Workspace, build_registry
from andromeda_tools.todo import TodoList

from . import compaction
from .approval import Answer, ApprovalRequest, Policy
from .providers import Provider
from .providers.base import AssistantTurn, ToolCall

# A runaway loop is a bill. Reached in practice only when the model is stuck
# retrying a failing tool, which is exactly when stopping is right.
MAX_STEPS = 24

SYSTEM_PROMPT = """You are Andromeda, running as a local-first agent in the user's terminal.

You are on the user's own machine. You have tools that read and change real \
files and run real commands — there is no sandbox between you and their work.

- Prefer reading before writing. Look at a file before you patch it.
- Use `patch` for part of a file and `write_file` only for a whole one.
- Non-zero exits and missing files come back to you as ordinary results. Read \
them and adjust rather than repeating the same call.
- Some tools stop for the user's approval. A denied call is the user's decision: \
say what you would have done and stop, do not look for another route to it.
- Answer directly and concisely. When you do not know something about their \
system, use a tool or say so — do not guess.

Your output is rendered as markdown in a terminal, so structure is free and \
worth using: headings, **bold** for the thing that matters, tables for anything \
with more than one column, and fenced code blocks with a language tag. Do not \
pad — a one-line answer stays one line.

For numbers worth comparing, emit a chart block and the terminal draws it:

```chart
read_file: 1240
terminal: 890
browser_navigate: 412
```

One `label: value` per line. Use it for counts, sizes, durations and shares — \
anything where the relative size is the point. Use a table when the exact \
figures are the point, and neither when there are only two numbers."""


class ApprovalPrompt(Protocol):
    def __call__(self, request: ApprovalRequest) -> Answer: ...


@dataclass
class Callbacks:
    """How the surface above watches a turn. All optional."""

    on_text: Callable[[str], None] | None = None
    on_tool_start: Callable[[ToolSpec, dict[str, Any]], None] | None = None
    on_tool_result: Callable[[ToolSpec, ToolResult], None] | None = None
    on_tool_denied: Callable[[ToolSpec, str], None] | None = None
    ask_approval: ApprovalPrompt | None = None
    on_compaction: Callable[[compaction.CompactionResult], None] | None = None


@dataclass
class Conversation:
    provider: Provider
    policy: Policy
    workspace: Workspace
    max_tokens: int = 8192
    temperature: float = 0.7
    # Per-conversation, so a delegated lane can carry its specialist's budget
    # rather than the session default.
    max_steps: int = MAX_STEPS
    # The model's context window, in tokens. Compaction starts at
    # `compaction.COMPACT_AT` of it — well before the wall, because the
    # summarisation call needs room to run.
    context_window: int = 128_000
    system_prompt: str = SYSTEM_PROMPT
    messages: list[dict[str, Any]] = field(default_factory=list)
    todos: TodoList = field(default_factory=TodoList)
    registry: dict[str, ToolSpec] = field(default_factory=dict)
    # Blocks appended to the system prompt: the skills manifest, standing
    # memories. Rebuilt on reset, so a `/new` picks up a skill installed since
    # the session started.
    context_blocks: list[str] = field(default_factory=list)
    # Called after every exchange with the full transcript. The surface uses it
    # to persist; the loop does not know or care where.
    on_persist: Callable[[list[dict[str, Any]]], None] | None = None
    # Rebuilds the registry for a fresh todo list, with this session's skills
    # and memory still bound. Supplied by the surface; see `reset`.
    rebuild_registry: Callable[[TodoList], dict[str, ToolSpec]] | None = None
    # Model turns taken in the most recent exchange. Not the same as
    # `turn_count`, which counts what the *user* said — a lane sends one user
    # message and may take a dozen steps answering it.
    steps_taken: int = 0
    # Set by the surface when this conversation can start background lanes.
    # Typed loosely on purpose: the loop must not import the lane machinery,
    # which imports tools, which would close a cycle.
    lane_registry: Any = None
    process_registry: Any = None
    mcp_servers: Any = None

    def __post_init__(self) -> None:
        if not self.registry:
            self.registry = build_registry(self.workspace, self.todos)
        if not self.messages:
            self.messages.append({"role": "system", "content": self._system_message()})

    def _system_message(self) -> str:
        parts = [self.system_prompt, f"Workspace root: {self.workspace.root}"]
        parts.extend(block for block in self.context_blocks if block.strip())
        return "\n\n".join(parts)

    @property
    def available(self) -> list[ToolSpec]:
        """Tools the model is told about.

        A tool the policy would deny outright is not advertised. Offering a
        capability that can only ever be refused wastes a turn and teaches the
        model to keep asking.
        """
        return [
            spec
            for spec in self.registry.values()
            if self.policy.decide(spec) != "denied"
        ]

    def send(self, prompt: str, callbacks: Callbacks | None = None) -> str:
        """Run one exchange to completion and return the final assistant text."""
        callbacks = callbacks or Callbacks()
        self.messages.append({"role": "user", "content": prompt})

        schemas = [spec.to_openai() for spec in self.available]
        last_text = ""
        self.steps_taken = 0

        for step in range(self.max_steps):
            self._compact_if_needed(callbacks)
            turn = self._model_turn(schemas, callbacks)
            self.steps_taken = step + 1
            self.messages.append(turn.to_message())

            if turn.content:
                last_text = turn.content

            if not turn.tool_calls:
                self._persist()
                return last_text

            for call in turn.tool_calls:
                self.messages.append(self._dispatch(call, callbacks))

        # The ceiling is reported into the transcript, so the model's next turn
        # knows why its tools stopped answering rather than trying again.
        note = f"Stopped after {self.max_steps} steps without finishing."
        self.messages.append({"role": "user", "content": note})
        self._persist()
        return last_text or note

    def _persist(self) -> None:
        if self.on_persist is None:
            return
        try:
            self.on_persist(self.messages)
        except Exception:  # noqa: BLE001 - a failed save must not lose the turn
            pass

    def _compact_if_needed(self, callbacks: Callbacks) -> None:
        """Make room before the request, not after it is refused.

        Pruning first because it is free. Summarising costs a model call, so it
        is only reached when pruning did not get under the line — which for an
        ordinary session it almost always does.
        """
        if not compaction.needs_compaction(self.messages, self.context_window):
            return

        before = compaction.estimate_tokens(self.messages)
        pruned_messages, pruned = compaction.micro_compact(self.messages)

        if pruned and not compaction.needs_compaction(pruned_messages, self.context_window):
            self.messages = pruned_messages
            self._report_compaction(
                callbacks,
                compaction.CompactionResult(
                    happened=True,
                    stage="prune",
                    before_tokens=before,
                    after_tokens=compaction.estimate_tokens(self.messages),
                    pruned_results=pruned,
                ),
            )
            return

        self.messages = pruned_messages
        result = self._summarise(before, pruned)
        if result is not None:
            self._report_compaction(callbacks, result)

    def _summarise(self, before: int, pruned: int) -> compaction.CompactionResult | None:
        system, older, recent = compaction.plan_summarisation(
            self.messages, self.context_window
        )
        if not older:
            # Nothing old enough to fold away. The transcript is one enormous
            # recent exchange, and compacting it would destroy the turn in
            # progress — better to let the provider refuse and say why.
            return None

        request = [*system, *older, {"role": "user", "content": compaction.SUMMARY_INSTRUCTION}]
        try:
            generator = self.provider.stream_turn(
                request,
                max_tokens=compaction.summary_budget(self.context_window),
                temperature=0.2,
                tools=None,
            )
            while True:
                try:
                    next(generator)
                except StopIteration as stop:
                    summary = stop.value.content
                    break
        except Exception:  # noqa: BLE001 - a failed summary must not end the turn
            # Pruning already happened and is kept. The provider will refuse the
            # next request if that was not enough, with its own message.
            return None

        if not summary.strip():
            return None

        self.messages = [*system, compaction.render_summary(summary), *recent]
        return compaction.CompactionResult(
            happened=True,
            stage="summarise",
            before_tokens=before,
            after_tokens=compaction.estimate_tokens(self.messages),
            pruned_results=pruned,
            summarised_messages=len(older),
        )

    def _report_compaction(
        self, callbacks: Callbacks, result: compaction.CompactionResult
    ) -> None:
        if callbacks.on_compaction is not None:
            callbacks.on_compaction(result)

    @property
    def context_used(self) -> float:
        """Fraction of the window in use. Shown in the REPL."""
        return compaction.usage_fraction(self.messages, self.context_window)

    def _model_turn(
        self, schemas: list[dict[str, Any]], callbacks: Callbacks
    ) -> AssistantTurn:
        generator = self.provider.stream_turn(
            self.messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            tools=schemas or None,
        )
        while True:
            try:
                text = next(generator)
            except StopIteration as stop:
                return stop.value
            if callbacks.on_text is not None:
                callbacks.on_text(text)

    def _dispatch(self, call: ToolCall, callbacks: Callbacks) -> dict[str, Any]:
        spec = self.registry.get(call.name)
        if spec is None:
            return _tool_message(call, f"Error: no tool named {call.name!r}.")

        if call.parse_error:
            # Handed back rather than dropped: an unanswered tool_call id makes
            # the next request malformed, and the model can usually fix its own
            # JSON when it is shown the error.
            return _tool_message(call, f"Error: {call.parse_error}")

        decision = self.policy.decide(spec)

        if decision == "denied":
            reason = f"{spec.name} is not available in this session."
            if callbacks.on_tool_denied is not None:
                callbacks.on_tool_denied(spec, reason)
            return _tool_message(call, f"Denied: {reason}")

        if decision == "needs_approval":
            answer = self._ask(spec, call, callbacks)
            if answer == "no":
                reason = "The user declined this."
                if callbacks.on_tool_denied is not None:
                    callbacks.on_tool_denied(spec, reason)
                return _tool_message(
                    call,
                    "Denied: the user declined this call. Do not retry it or look "
                    "for another way to do the same thing.",
                )
            if answer == "session":
                self.policy = self.policy.grant_for_session(spec.name)
            elif answer in {"always", "never"} and self.policy.allowlist is not None:
                self.policy.allowlist.trust(
                    spec.name,
                    spec.risk_tier,
                    "always_allow" if answer == "always" else "always_deny",
                )
                if answer == "never":
                    reason = "The user chose never to allow this."
                    if callbacks.on_tool_denied is not None:
                        callbacks.on_tool_denied(spec, reason)
                    return _tool_message(
                        call,
                        "Denied: the user has refused this tool permanently. Do not "
                        "retry it or look for another way to do the same thing.",
                    )

            if self.policy.allowlist is not None:
                self.policy.allowlist.record(
                    spec.name, spec.risk_tier, approved=answer != "no"
                )

        if callbacks.on_tool_start is not None:
            callbacks.on_tool_start(spec, call.arguments)

        result = self._run(spec, call.arguments)

        if callbacks.on_tool_result is not None:
            callbacks.on_tool_result(spec, result)

        return _tool_message(call, result.content)

    def _ask(self, spec: ToolSpec, call: ToolCall, callbacks: Callbacks) -> Answer:
        if callbacks.ask_approval is None:
            # No one is watching. A tool that needs a person and has none is
            # refused — never auto-approved.
            return "no"
        return callbacks.ask_approval(
            ApprovalRequest(
                spec=spec,
                arguments=call.arguments,
                summary=spec.summary(call.arguments),
                allowlist=self.policy.allowlist,
            )
        )

    def _run(self, spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        try:
            return spec.run(**arguments)
        except TypeError as exc:
            # Wrong or missing arguments — the model's mistake, and one it can
            # correct if it is told which call was wrong.
            return ToolResult(content=f"Error: {spec.name} rejected its arguments: {exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 - a failing tool must not end the session
            return ToolResult(content=f"Error: {spec.name} failed: {exc}", ok=False)

    def reset(self) -> None:
        """Start over, keeping the session's bindings.

        The todo list is per-conversation and is discarded. Skills and memory
        are not — they are bound to this machine, not to this transcript — so
        the registry is rebuilt through `rebuild_registry`, which the surface
        supplies with all of those bindings closed over. Without it the rebuild
        would silently drop skill_load and the memory tools.
        """
        self.messages = [{"role": "system", "content": self._system_message()}]
        self.todos = TodoList()
        self.registry = (
            self.rebuild_registry(self.todos)
            if self.rebuild_registry is not None
            else build_registry(self.workspace, self.todos)
        )

    @property
    def turn_count(self) -> int:
        return sum(1 for message in self.messages if message["role"] == "user")


def _tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}
