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

import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol

from andromeda_tools import ToolResult, ToolSpec, Workspace, build_registry
from andromeda_tools.todo import TodoList

from . import compaction, hooks, lsp as lsp_module, middleware, redact, resilience, tool_search
from . import usage as usage_module
from . import hints as hints_module
from .errors import AgentError
from .approval import Answer, ApprovalRequest, Policy
from .providers import Provider
from .providers.base import AssistantTurn, ToolCall

# A runaway loop is a bill. Reached in practice only when the model is stuck
# retrying a failing tool, which is exactly when stopping is right.
MAX_STEPS = 24

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Andromeda, running as a local-first agent in the user's terminal.

You are on the user's own machine — there is no sandbox between you and their work.
Your tools change real files and run real commands.

- Prefer reading before writing. Look at a file before you patch it.
- Use `patch` for part of a file and `write_file` only for a whole one.
- Non-zero exits and missing files come back to you as ordinary results. Read \
them and adjust rather than repeating the same call.
- Some tools stop for the user's approval. A denied call is the user's decision: \
say what you would have done and stop, do not look for another route to it.
- Answer directly and concisely. When you do not know something about their \
system, use a tool or say so — do not guess.
- When the user asks about a third-party app you have no tools for, that is \
usually a connection that has not been made yet, not a dead end. Use \
`connect_app` to see whether it can be connected and offer to do it. Never \
answer "I have no access to X" without checking first.
- When you need a credential for a service, the order is: a connected app \
first, then this workspace's own configuration, then ask the user. Never \
search the filesystem for one. Do not grep for key names, do not read \
env dumps, backups or another project's files looking for a secret, and \
never reuse a key you found that way — a key sitting in one project was not \
given to you for this one. If you cannot find a credential through a \
connection or the workspace, say what is needed and ask.

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


# Lines of `SYSTEM_PROMPT` that only make sense with a particular tool, keyed
# on a distinctive substring. A session narrowed to `safe_local` — which is
# every non-interactive run, by default — cannot call `patch` or `write_file`,
# and being told how to choose between them is an instruction the model spends
# a turn discovering it cannot follow. `tests/test_loop.py` asserts each key
# still matches exactly one line, so re-wording the prompt cannot silently
# orphan the tailoring.
_PROMPT_REQUIRES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "Your tools change real files",
        frozenset({"patch", "write_file", "terminal"}),
    ),
    ("Prefer reading before writing", frozenset({"patch", "write_file"})),
    ("When the user asks about a third-party app", frozenset({"connect_app"})),
    ("`connect_app` to see whether", frozenset({"connect_app"})),
    ("When you need a credential for a service", frozenset({"terminal", "read_file"})),
    ("search the filesystem for one", frozenset({"terminal", "read_file"})),
    ("env dumps, backups or another", frozenset({"terminal", "read_file"})),
    ("never reuse a key you found that way", frozenset({"terminal", "read_file"})),
    ("connection or the workspace, say what", frozenset({"terminal", "read_file"})),
    ("answer \"I have no access to X\"", frozenset({"connect_app"})),
    ("Use `patch` for part of a file", frozenset({"patch", "write_file"})),
    (
        "Non-zero exits and missing files",
        frozenset({"terminal", "read_file", "patch", "write_file"}),
    ),
)


def tailor_prompt(text: str, tool_names: Iterable[str] | None) -> str:
    """`text` with every line that needs a missing tool removed.

    `None` means "do not tailor" and returns the prompt whole — the right
    answer for a caller that does not yet know the session's tools, since
    guessing would drop advice the session can use.
    """
    if tool_names is None:
        return text
    names = set(tool_names)
    kept = []
    for line in text.splitlines():
        required = next(
            (needed for key, needed in _PROMPT_REQUIRES if key in line), None
        )
        if required is None or (required & names):
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


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
    # Called when a turn is about to be re-issued, with a short reason. The
    # surface prints it: a terminal that goes quiet for twenty seconds reads as
    # a hang, and "rate limited, retrying in 12s" reads as a wait.
    on_retry: Callable[[str], None] | None = None


def _approval_transport():
    """The first plugin approval transport, or None. Never raises."""
    try:
        from . import plugins as plugins_module

        transports = plugins_module.approval_transports()
    except Exception:  # noqa: BLE001 - the gate must not depend on plugins
        return None
    for _name, present in sorted(transports.items()):
        return present
    return None


def _transport_answer(present, spec: ToolSpec, arguments: dict[str, Any], summary: str) -> Answer:
    """Ask a plugin transport, and refuse on anything unexpected.

    **Fails closed, in every direction.** A transport that raises, times out,
    or returns something that is not one of the gate's answers gets "no". This
    is the one registration point where failing open would mean a tool running
    because a plugin was broken, which is indistinguishable from a plugin that
    approved it.
    """
    try:
        answer = present(
            ApprovalRequest(spec=spec, arguments=arguments, summary=summary)
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("approval transport raised, refusing the call: %s", exc)
        return "no"
    if answer not in {"yes", "no", "session", "always", "never"}:
        logger.warning(
            "approval transport answered %r, which is not an answer; refusing",
            answer,
        )
        return "no"
    return answer


def _plugin_prompt_sections() -> str:
    """The plugin block for the system prompt, or "".

    Never raises: a plugin that cannot render its own section costs its
    section, not the session. Imported inside the function because
    `andromeda_agent.plugins` imports the hook bus, which imports this
    package.
    """
    try:
        from . import plugins as plugins_module

        return plugins_module.render_prompt_sections()
    except Exception:  # noqa: BLE001 - see the docstring
        return ""


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
    # Called immediately before a summary replaces older turns, with the
    # pre-compaction transcript and the position range about to be folded away.
    # Whatever it returns is appended to the summary, which is how the surface
    # tells the model those turns are still searchable. Injected rather than
    # imported: the loop must not know about the session index.
    on_archive: (
        Callable[[list[dict[str, Any]], int, int], str] | None
    ) = None
    # Rebuilds the registry for a fresh todo list, with this session's skills
    # and memory still bound. Supplied by the surface; see `reset`.
    rebuild_registry: Callable[[TodoList], dict[str, ToolSpec]] | None = None
    # Model turns taken in the most recent exchange. Not the same as
    # `turn_count`, which counts what the *user* said — a lane sends one user
    # message and may take a dozen steps answering it.
    steps_taken: int = 0
    # Set by `reload_tools`, cleared by the step loop once it has picked the
    # new catalogue up. A flag rather than a direct write, because `send` holds
    # `schemas` as a local and a tool running underneath it cannot reach that.
    _tools_changed: bool = False
    # Set by the surface when this conversation can start background lanes.
    # Typed loosely on purpose: the loop must not import the lane machinery,
    # which imports tools, which would close a cycle.
    lane_registry: Any = None
    process_registry: Any = None
    mcp_servers: Any = None
    # Discovers a directory's own AGENTS.md as the model reaches it and appends
    # it to that tool's result. `None` disables it entirely, which is what a
    # session outside a workspace gets — there is nothing to discover.
    hints: hints_module.Hints | None = None
    # Language-server diagnostics after an edit. `None` outside a workspace and
    # whenever no server for the project's languages is installed — see
    # `andromeda_agent.lsp`, which never installs one.
    lsp: Any = None
    # Consecutive empty completions in this exchange. Held on the conversation
    # rather than passed down, because the streak has to survive the retry loop
    # and die on the first turn that says something.
    _empties: resilience.Empties = field(default_factory=resilience.Empties)
    # What this conversation has spent, in tokens. Accumulated here and written
    # onto the transcript by the surface's `on_persist`, because the transcript
    # is this harness's source of truth and a token count is the one thing that
    # cannot be recovered from one.
    usage: usage_module.Usage = field(default_factory=usage_module.Usage)
    # Identity, for the hook payloads. A conversation that nobody named still
    # fires its hooks — with an empty id rather than none at all, so a script
    # can tell "no session" from "key missing".
    session_id: str = ""
    surface: str = "repl"
    # How MCP tools are offered. `auto` and `on` put them behind the search
    # bridge; `off` lists every one of them on every request.
    tool_search_mode: str = "auto"
    tool_search_listing_tokens: int = 4000
    # This exchange's tool array, rebuilt at the top of every `send`. Held so
    # the bridge calls can be answered from the same catalogue the model was
    # shown, rather than from one assembled a second time.
    assembly: Any = None

    def __post_init__(self) -> None:
        if not self.registry:
            self.registry = build_registry(self.workspace, self.todos)
        if not self.messages:
            self.messages.append({"role": "system", "content": self._system_message()})

    def _system_message(self) -> str:
        """The system prompt, tailored to the tools this session actually has.

        A lane's brief arrives already tailored, so only the default prompt is
        rewritten here — and only when the registry is populated, which is the
        one moment the offered set is knowable.
        """
        prompt = self.system_prompt
        if prompt is SYSTEM_PROMPT and self.registry:
            prompt = tailor_prompt(prompt, self._offered())
        parts = [prompt, f"Workspace root: {self.workspace.root}"]
        # Plugin sections before the caller's context blocks, and inside their
        # own markers. They are the stable part — the same set of plugins gives
        # the same bytes every turn — so putting them ahead of blocks that
        # change keeps more of the cached prefix intact.
        plugin_block = _plugin_prompt_sections()
        if plugin_block:
            parts.append(plugin_block)
        parts.extend(block for block in self.context_blocks if block.strip())
        return "\n\n".join(parts)

    def _offered(self) -> set[str]:
        """The tools the model is actually told about."""
        return {spec.name for spec in self.available}

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

        # Rebuilt every exchange from the live registry. A catalogue carried
        # across turns drifts out of step with the tools that actually exist,
        # and the failure is silent — a tool the model can see and cannot call.
        self.assembly = tool_search.assemble(
            self.available,
            context_window=self.context_window,
            mode=self.tool_search_mode,
            listing_max_tokens=self.tool_search_listing_tokens,
        )
        schemas = self.assembly.schemas
        last_text = ""
        self.steps_taken = 0

        for step in range(self.max_steps):
            self._compact_if_needed(callbacks)
            # A tool call in the previous step may have added tools — that is
            # what `connect_app` does. Picking the catalogue back up here is
            # what lets the model use them on the very next step instead of
            # after a restart.
            if self._tools_changed:
                schemas = self.assembly.schemas
                self._tools_changed = False
            turn = self._model_turn(schemas, callbacks, step=step, user_message=prompt)
            self.steps_taken = step + 1
            self.messages.append(turn.to_message())

            if turn.content:
                last_text = turn.content

            if not turn.tool_calls:
                self._persist()
                return self._final_text(last_text)

            for call in turn.tool_calls:
                self.messages.append(self._dispatch(call, callbacks, step=step))

        # The ceiling is reported into the transcript, so the model's next turn
        # knows why its tools stopped answering rather than trying again.
        note = f"Stopped after {self.max_steps} steps without finishing."
        self.messages.append({"role": "user", "content": note})
        self._persist()
        return self._final_text(last_text or note)

    def _final_text(self, text: str) -> str:
        """The last word of an exchange, after any `transform_llm_output` hook.

        Transformed on the way out and *not* written back into the transcript:
        the model has to keep seeing what it actually said, or the next turn
        reasons from a version of its own history that never happened.
        """
        return hooks.transform(
            "transform_llm_output",
            text,
            session_id=self.session_id,
            model=getattr(self.provider, "model", ""),
            steps_taken=self.steps_taken,
        )

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

        # Archived *after* the summary succeeded and before the transcript is
        # replaced. Either order of the two failures matters: archiving first
        # and then failing to summarise would mark turns as folded away while
        # they were still in the conversation, and replacing first would leave
        # nothing to point the archive at.
        recall = self._archive(system, older)

        self.messages = [
            *system,
            compaction.render_summary(summary, recall),
            *recent,
        ]
        return compaction.CompactionResult(
            happened=True,
            stage="summarise",
            before_tokens=before,
            after_tokens=compaction.estimate_tokens(self.messages),
            pruned_results=pruned,
            summarised_messages=len(older),
        )

    def _archive(
        self, system: list[dict[str, Any]], older: list[dict[str, Any]]
    ) -> str:
        """Hand the turns about to be discarded to the surface, for keeping.

        Returns the note to append to the summary, or "" when there is no
        surface, no index, or the archive failed — in which case the summary
        makes no promise it cannot keep.
        """
        if self.on_archive is None or not older:
            return ""
        first = len(system)
        try:
            return self.on_archive(self.messages, first, first + len(older) - 1) or ""
        except Exception:  # noqa: BLE001 - compaction must not fail over an index
            return ""

    def _report_compaction(
        self, callbacks: Callbacks, result: compaction.CompactionResult
    ) -> None:
        if callbacks.on_compaction is not None:
            callbacks.on_compaction(result)
        hooks.fire(
            "on_compaction",
            session_id=self.session_id,
            stage=result.stage,
            before_tokens=result.before_tokens,
            after_tokens=result.after_tokens,
            pruned_results=result.pruned_results,
            summarised_messages=result.summarised_messages,
        )

    @property
    def context_used(self) -> float:
        """Fraction of the window in use. Shown in the REPL."""
        return compaction.usage_fraction(self.messages, self.context_window)

    def _model_turn(
        self,
        schemas: list[dict[str, Any]],
        callbacks: Callbacks,
        *,
        step: int = 0,
        user_message: str = "",
    ) -> AssistantTurn:
        """One model turn, through the transport and the two content guards.

        The retry loop counts *attempts at the same turn*, which is not the
        same as the step loop above: a rate limit and a nudged empty both
        re-issue this call without the conversation moving on.

        The invariant that shapes it: nothing is retried once text has reached
        the terminal. A stream cannot be unprinted, so re-issuing a half-shown
        answer produces two half-answers stitched together — worse than one
        failure that says what happened and keeps what arrived.
        """
        model = getattr(self.provider, "model", "")
        nudge = ""
        attempt = 0

        while True:
            attempt += 1
            request = self._request_messages(
                step=step, user_message=user_message, model=model, nudge=nudge
            )
            max_tokens = self.max_tokens
            temperature = self.temperature
            tools = schemas or None

            # `llm_request` middleware. The last thing that touches a request
            # before it leaves — after compaction, after the nudge, after the
            # tool assembly — because a rewrite that happened earlier would be
            # undone by any of them.
            if middleware.has(middleware.LLM_REQUEST):
                rewritten = middleware.apply_request(
                    middleware.LLM_REQUEST,
                    middleware.payload(
                        messages=request,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        model=model,
                        session_id=self.session_id,
                        step=step,
                        attempt=attempt,
                    ),
                )
                if isinstance(rewritten.get("messages"), list):
                    request = rewritten["messages"]
                if isinstance(rewritten.get("max_tokens"), int):
                    max_tokens = rewritten["max_tokens"]
                if isinstance(rewritten.get("temperature"), (int, float)):
                    temperature = float(rewritten["temperature"])
                if rewritten.get("tools") is None or isinstance(rewritten.get("tools"), list):
                    tools = rewritten.get("tools")

            streamed = False

            def _one_turn() -> AssistantTurn:
                """Drive the provider's generator to completion.

                Pulled out so `llm_execution` middleware can wrap it. `streamed`
                is set from in here on purpose: `resilience.plan_retry` refuses
                to retry once text has reached the terminal, and that has to
                stay true across a middleware retry — the text cannot be
                unprinted just because a wrapper decided to try again.
                """
                nonlocal streamed
                generator = self.provider.stream_turn(
                    request,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                )
                while True:
                    try:
                        text = next(generator)
                    except StopIteration as stop:
                        return stop.value
                    streamed = True
                    if callbacks.on_text is not None:
                        callbacks.on_text(text)

            try:
                if middleware.has(middleware.LLM_EXECUTION):
                    turn = middleware.apply_execution(
                        middleware.LLM_EXECUTION,
                        _one_turn,
                        middleware.payload(
                            model=model,
                            session_id=self.session_id,
                            step=step,
                            attempt=attempt,
                            message_count=len(request),
                        ),
                    )
                    if not isinstance(turn, AssistantTurn):
                        raise AgentError(
                            f"An llm_execution middleware returned "
                            f"{type(turn).__name__}, not an AssistantTurn.",
                            hint="`andromeda plugins list` shows what is loaded.",
                        )
                else:
                    turn = _one_turn()
            except AgentError as exc:
                plan = resilience.plan_retry(exc, attempt, streamed=streamed)
                if not plan:
                    # Whatever arrived before the failure is kept rather than
                    # discarded: the model said it, the user saw it, and the
                    # next turn has to reason from the same history they do.
                    partial = getattr(exc, "partial", "") or ""
                    if partial.strip():
                        self.messages.append(
                            AssistantTurn(content=partial).to_message()
                        )
                        self._persist()
                    raise
                if callbacks.on_retry is not None:
                    callbacks.on_retry(plan.reason)
                time.sleep(plan.delay)
                continue

            # Recorded for every response, including the empty ones and the
            # ones about to be retried: they were billed, and a total that
            # counts only the answers people liked is not a total.
            if turn.usage:
                self.usage.record(
                    model,
                    input=turn.usage.get("input", 0),
                    output=turn.usage.get("output", 0),
                    cached=turn.usage.get("cached", 0),
                    reasoning=turn.usage.get("reasoning", 0),
                )

            hooks.fire(
                "post_llm_call",
                session_id=self.session_id,
                model=model,
                step=step,
                content_chars=len(turn.content or ""),
                tool_call_count=len(turn.tool_calls or ()),
                input_tokens=(turn.usage or {}).get("input", 0),
                output_tokens=(turn.usage or {}).get("output", 0),
            )

            if not resilience.is_empty_turn(turn):
                self._empties.reset()
                return self._guard_repetition(turn)

            # An empty completion. One is a flaky decode and worth another
            # request; the same emptiness twice from the same model with the
            # same finish reason will not become an answer on the third
            # attempt, and each attempt re-sends the whole conversation.
            self._empties.record(model, turn.finish_reason)
            if self._empties.should_retry() and not streamed:
                nudge = resilience.EMPTY_NUDGE
                if callbacks.on_retry is not None:
                    callbacks.on_retry("the model returned nothing, asking again")
                continue

            self._empties.reset()
            return replace(
                turn,
                content=(
                    "The model returned an empty response twice in a row. This "
                    "usually means the request was refused without saying so, "
                    "or the conversation is in a state it will not answer. Try "
                    "rephrasing, or `/new` to start a fresh session."
                ),
            )

    def _guard_repetition(self, turn: AssistantTurn) -> AssistantTurn:
        """Stop a length-truncated answer that is just one fragment repeating.

        The natural response to `finish_reason="length"` is to ask for the
        rest. Asking a model that has fallen into a loop for the rest buys more
        of the loop at full price, so the loop is named instead — the text
        already produced is kept, because the beginning of it is usually a real
        answer that went wrong partway through.
        """
        if turn.finish_reason != "length":
            return turn
        if not resilience.is_repetition_dominated(turn.content):
            return turn
        return replace(
            turn,
            content=(
                turn.content.rstrip()
                + "\n\n[Stopped: the response ran to the output limit repeating "
                "itself, so it was not continued.]"
            ),
        )

    def _request_messages(
        self, *, step: int, user_message: str, model: str, nudge: str = ""
    ) -> list[dict[str, Any]]:
        """The transcript as this request sees it, plus any injected context.

        Injection lands in a trailing *user* message and never in the system
        prompt: the system prompt has to stay byte-identical between turns or
        the provider's cached prefix is thrown away on every request. It is
        also never appended to `self.messages` — an injection that persisted
        would be replayed next turn as though the user had typed it.
        """
        extra = hooks.injected_context(
            session_id=self.session_id,
            model=model,
            message_count=len(self.messages),
            step=step,
            user_message=user_message,
        )
        # The nudge rides in the same trailing slot and for the same reason:
        # a retry after an empty response has to tell the model what went
        # wrong, and writing that into the transcript would replay it next
        # turn as though the user had typed it.
        trailing = "\n\n".join(part for part in (extra, nudge) if part)
        if not trailing:
            return self.messages
        return [*self.messages, {"role": "user", "content": trailing}]

    def _dispatch(
        self, call: ToolCall, callbacks: Callbacks, *, step: int = 0
    ) -> dict[str, Any]:
        if tool_search.is_bridge(call.name):
            return self._bridge(call, callbacks, step=step)

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
            self._after_tool(
                spec, call, call.arguments, step, "blocked", reason, 0.0
            )
            return _tool_message(call, f"Denied: {reason}")

        # Hooks run *before* the approval prompt, deliberately. A hook that
        # blocks should not first make the user answer a question about a call
        # that was never going to run, and a hook that rewrites the arguments
        # has to do it before consent is asked — the prompt states what will
        # actually happen, so it must be shown the final arguments.
        directive = hooks.pre_tool_directive(
            spec.name,
            call.arguments,
            session_id=self.session_id,
            tool_call_id=call.id,
            risk_tier=spec.risk_tier,
            step=step,
        )
        arguments = (
            directive.modified_args
            if directive.modified_args is not None
            else call.arguments
        )

        # `tool_request` middleware, in the same window as a hook's `modify`
        # and for the same reason: the approval prompt states what will
        # actually happen, so anything that rewrites the arguments has to have
        # finished before consent is asked. After the hooks, because a hook can
        # block and there is no point rewriting a call that is about to be
        # refused.
        if middleware.has(middleware.TOOL_REQUEST):
            rewritten = middleware.apply_request(
                middleware.TOOL_REQUEST,
                middleware.payload(
                    tool_name=spec.name,
                    args=dict(arguments) if isinstance(arguments, dict) else {},
                    risk_tier=spec.risk_tier,
                    session_id=self.session_id,
                    tool_call_id=call.id,
                    step=step,
                ),
            )
            replacement = rewritten.get("args")
            if isinstance(replacement, dict):
                arguments = replacement

        if directive.action == "block":
            reason = directive.message or "A hook blocked this call."
            if callbacks.on_tool_denied is not None:
                callbacks.on_tool_denied(spec, reason)
            self._after_tool(spec, call, arguments, step, "blocked", reason, 0.0)
            return _tool_message(call, f"Denied: {reason}")

        if directive.action == "approve" and decision == "allowed":
            # An escalation only ever adds a gate; it can never remove one.
            decision = "needs_approval"

        if decision == "needs_approval":
            answer = self._ask(spec, call, arguments, callbacks, directive)
            if answer == "no":
                reason = "The user declined this."
                if callbacks.on_tool_denied is not None:
                    callbacks.on_tool_denied(spec, reason)
                self._after_tool(
                    spec, call, arguments, step, "blocked", reason, 0.0
                )
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
                    self._after_tool(
                        spec, call, arguments, step, "blocked", reason, 0.0
                    )
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
            callbacks.on_tool_start(spec, arguments)

        # Taken before the edit, because a diagnostic is only useful as a
        # delta and a delta needs a baseline. Cheap to ask for: outside a
        # workspace, for a language nobody has a server for, or for a tool that
        # does not edit, this is a dictionary lookup and a `None`.
        snapshot = self._baseline(spec.name, arguments)

        started = time.monotonic()
        # `tool_execution` middleware wraps this, so a plugin can retry it,
        # cache it, or answer without running it. Inside the timing, so a
        # retry's cost shows up in `duration_ms` — a wrapper that hid its own
        # latency would make the slow tool look fast and the loop look broken.
        if middleware.has(middleware.TOOL_EXECUTION):
            result = middleware.apply_execution(
                middleware.TOOL_EXECUTION,
                lambda: self._run(spec, arguments),
                middleware.payload(
                    tool_name=spec.name,
                    args=dict(arguments) if isinstance(arguments, dict) else {},
                    risk_tier=spec.risk_tier,
                    session_id=self.session_id,
                    tool_call_id=call.id,
                    step=step,
                ),
            )
            if not isinstance(result, ToolResult):
                # A middleware that returned the wrong shape would otherwise
                # reach the transcript as a repr. Reported to the model as an
                # ordinary tool error, which it can recover from.
                result = ToolResult(
                    content=(
                        f"Error: a tool_execution middleware returned "
                        f"{type(result).__name__}, not a ToolResult."
                    ),
                    ok=False,
                )
        else:
            result = self._run(spec, arguments)
        duration_ms = (time.monotonic() - started) * 1000

        # The one place secrets are removed. Everything downstream of this line
        # — the terminal, the transcript, the search index, an export, a hook,
        # the model — reads the scrubbed result, so none of them can disagree
        # about what a secret is. Before the surface callback, deliberately: a
        # secret that reaches the scrollback has already been read.
        result = self._scrub(spec, arguments, result)

        if callbacks.on_tool_result is not None:
            callbacks.on_tool_result(spec, result)

        content = hooks.transform(
            "transform_tool_result",
            result.content,
            tool_name=spec.name,
            args=arguments,
            session_id=self.session_id,
            tool_call_id=call.id,
            risk_tier=spec.risk_tier,
            step=step,
            status="ok" if result.ok else "error",
        )
        self._after_tool(
            spec,
            call,
            arguments,
            step,
            "ok" if result.ok else "error",
            None if result.ok else result.content,
            duration_ms,
            result=content,
        )

        # A context file for the part of the tree this call just reached, if
        # the model has not been here before. After the transform, so a hook
        # that rewrites a result does not rewrite the project's own
        # instructions; and only on success, because a failing call is about to
        # be retried and burying its error under a page of conventions is the
        # wrong thing to read next. The directory stays unvisited on a failure,
        # so the next call that lands there still finds it.
        if result.ok and self.hints is not None:
            extra = self.hints.for_call(spec.name, arguments)
            if extra:
                content = f"{content}{extra}"

        # What this edit broke, if anything. Only on success: a patch that did
        # not apply changed nothing, and running a type checker to prove it is
        # a wasted round trip.
        if result.ok and snapshot is not None:
            diagnostics = self._diagnostics(snapshot)
            if diagnostics:
                content = f"{content}{diagnostics}"

        # The surface saw the tool's own output; the model sees the transformed
        # text. Whoever rewrote it meant it for the model.
        return _tool_message(call, content)

    def _baseline(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """What the file being edited looked like before, or `None`.

        Wrapped rather than called directly so the language-server layer can
        never end a turn: the edit is about to happen either way, and a
        baseline that raised would take the tool call down with it.
        """
        if self.lsp is None or not lsp_module.watches(tool_name):
            return None
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        try:
            return self.lsp.before(self.workspace.resolve(path))
        except Exception:  # noqa: BLE001 - diagnostics are never load-bearing
            return None

    def _diagnostics(self, snapshot: Any) -> str:
        if self.lsp is None:
            return ""
        try:
            return self.lsp.after(snapshot)
        except Exception:  # noqa: BLE001 - see `_baseline`
            return ""

    def _bridge(
        self, call: ToolCall, callbacks: Callbacks, *, step: int
    ) -> dict[str, Any]:
        """Answer one of the three tools that stand in for the deferred ones.

        `tool_search` and `tool_describe` read an in-memory catalogue and
        change nothing, so they do not go near the gate. `tool_call` does not
        answer anything itself — it resolves the real tool and hands it to the
        ordinary dispatch path, so a call through the bridge meets exactly the
        policy, the prompt and the hooks a direct call would.
        """
        assembly = getattr(self, "assembly", None)
        if assembly is None or not assembly.activated:
            return _tool_message(
                call, f"Error: no tool named {call.name!r}."
            )

        if call.parse_error:
            return _tool_message(call, f"Error: {call.parse_error}")

        if call.name == tool_search.SEARCH:
            self._note_bridge(callbacks, call, "searching for a tool")
            return _tool_message(
                call, tool_search.dispatch_search(assembly, call.arguments)
            )

        if call.name == tool_search.DESCRIBE:
            self._note_bridge(callbacks, call, "loading a tool's parameters")
            return _tool_message(
                call, tool_search.dispatch_describe(assembly, call.arguments)
            )

        spec, arguments, error = tool_search.resolve_call(assembly, call.arguments)
        if spec is None:
            return _tool_message(call, f"Error: {error}")

        blind = tool_search.missing_arguments(spec, arguments)
        if blind:
            # The schema back, rather than a failure from inside the tool that
            # says nothing about what was expected.
            return _tool_message(call, blind)

        # Re-entered as though the model had named the tool directly. Every
        # gate below this line is the one and only implementation of itself.
        return self._dispatch(
            replace(call, name=spec.name, arguments=arguments), callbacks, step=step
        )

    def _note_bridge(self, callbacks: Callbacks, call: ToolCall, label: str) -> None:
        """Show bridge activity as itself.

        A person watching a turn should see the lookup happen; showing nothing
        makes a searching model look like a stalled one.
        """
        if callbacks.on_tool_start is None:
            return
        callbacks.on_tool_start(
            ToolSpec(
                name=call.name,
                description=label,
                parameters={},
                risk_tier="safe_local",
                category="read",
                run=lambda **_kwargs: ToolResult(content=""),
                summarize=lambda arguments: f"{call.name}: {label}",
            ),
            call.arguments,
        )

    def _scrub(
        self, spec: ToolSpec, arguments: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """Remove secrets from a tool result, in place of the original.

        `display` is scrubbed against the same policy but separately, because
        it is usually a summary rather than a slice of `content` — scrubbing
        one and copying it into the other would either lose the summary or
        leak whatever the summary quoted.

        A file read that lost something says so. The sentinel is unusable by
        construction, but only a reader who knows that treats it as unusable;
        without the note the model has been observed writing it onward as
        though it were the value.
        """
        scrubbed = redact.scrub_tool_result(spec.name, arguments, result.content)
        content = scrubbed.text
        if scrubbed.changed and spec.name in {"read_file", "search_files"}:
            content += redact.notice(scrubbed)

        display = result.display
        if display and display != result.content:
            display = redact.scrub_tool_result(spec.name, arguments, display).text
        elif display:
            display = scrubbed.text

        if content == result.content and display == result.display:
            return result
        return replace(result, content=content, display=display)

    def _after_tool(
        self,
        spec: ToolSpec,
        call: ToolCall,
        arguments: dict[str, Any],
        step: int,
        status: str,
        error_message: str | None,
        duration_ms: float,
        result: str = "",
    ) -> None:
        hooks.fire(
            "post_tool_call",
            tool_name=spec.name,
            args=arguments,
            session_id=self.session_id,
            tool_call_id=call.id,
            risk_tier=spec.risk_tier,
            step=step,
            status=status,
            result=result,
            error_message=error_message,
            duration_ms=round(duration_ms, 3),
        )

    def _ask(
        self,
        spec: ToolSpec,
        call: ToolCall,
        arguments: dict[str, Any],
        callbacks: Callbacks,
        directive: hooks.Directive | None = None,
    ) -> Answer:
        # Only the echo of the call, never the call. The arguments themselves go
        # to the tool and to the transcript unchanged: a scrubbed argument would
        # run a different command than the one consented to, and a scrubbed
        # transcript would replay a call the model never made. The model can
        # only be holding a secret it was given, and everything it is given
        # comes through `_scrub` — so this is a belt for the one case that
        # bypasses it, a key the user typed themselves.
        summary = redact.scrub(spec.summary(arguments), code_file=False).text
        hooks.fire(
            "pre_approval_request",
            tool_name=spec.name,
            summary=summary,
            risk_tier=spec.risk_tier,
            session_id=self.session_id,
            surface=self.surface,
        )
        transport = _approval_transport()
        if transport is not None:
            # A plugin transport takes precedence over "nobody is watching",
            # which is the whole point of one: it reaches a person who is not
            # at this terminal. It does *not* take precedence over a surface
            # that has a live prompt — someone sitting here answering is the
            # better authority than a message sent somewhere else.
            if callbacks.ask_approval is None:
                answer: Answer = _transport_answer(transport, spec, arguments, summary)
            else:
                answer = callbacks.ask_approval(
                    ApprovalRequest(
                        spec=spec,
                        arguments=arguments,
                        summary=summary,
                        allowlist=self.policy.allowlist,
                        reason=directive.message if directive is not None else None,
                    )
                )
        elif callbacks.ask_approval is None:
            # No one is watching. A tool that needs a person and has none is
            # refused — never auto-approved.
            answer = "no"
        else:
            answer = callbacks.ask_approval(
                ApprovalRequest(
                    spec=spec,
                    arguments=arguments,
                    summary=summary,
                    allowlist=self.policy.allowlist,
                    reason=directive.message if directive is not None else None,
                )
            )
        hooks.fire(
            "post_approval_response",
            tool_name=spec.name,
            summary=summary,
            risk_tier=spec.risk_tier,
            session_id=self.session_id,
            surface=self.surface,
            answer=answer,
        )
        return answer

    def _run(self, spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        try:
            return spec.run(**arguments)
        except TypeError as exc:
            # Wrong or missing arguments — the model's mistake, and one it can
            # correct if it is told which call was wrong.
            return ToolResult(content=f"Error: {spec.name} rejected its arguments: {exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 - a failing tool must not end the session
            return ToolResult(content=f"Error: {spec.name} failed: {exc}", ok=False)

    def reload_tools(self) -> list[str]:
        """Rebuild the toolset mid-session and return the names that are new.

        The toolset was chosen once, at session start, which was right until
        the agent gained the ability to *change* what tools exist. Connecting an
        app and then telling the person to restart is the harness admitting it
        cannot use the thing it just did — and the restart throws away the
        conversation that led to the connection.

        Keeps the transcript and the todos: this is not a reset. Only the
        registry and the schemas derived from it are rebuilt.
        """
        if self.rebuild_registry is None:
            return []
        before = set(self.registry)
        self.registry = self.rebuild_registry(self.todos)

        # Re-assembled here rather than left to the next exchange. `send`
        # builds the catalogue once and hands the same list to every step, so a
        # registry that grew mid-turn would be invisible until the person spoke
        # again — which is the restart this exists to avoid, one turn later.
        self.assembly = tool_search.assemble(
            self.available,
            context_window=self.context_window,
            mode=self.tool_search_mode,
            listing_max_tokens=self.tool_search_listing_tokens,
        )
        self._tools_changed = True
        return sorted(set(self.registry) - before)

    def reset(self) -> None:
        """Start over, keeping the session's bindings.

        The todo list is per-conversation and is discarded. Skills and memory
        are not — they are bound to this machine, not to this transcript — so
        the registry is rebuilt through `rebuild_registry`, which the surface
        supplies with all of those bindings closed over. Without it the rebuild
        would silently drop skill_load and the memory tools.
        """
        turns = self.turn_count
        self.messages = [{"role": "system", "content": self._system_message()}]
        self.todos = TodoList()
        self.registry = (
            self.rebuild_registry(self.todos)
            if self.rebuild_registry is not None
            else build_registry(self.workspace, self.todos)
        )
        hooks.fire(
            "on_session_reset",
            session_id=self.session_id,
            model=getattr(self.provider, "model", ""),
            surface=self.surface,
            turn_count=turns,
        )

    @property
    def turn_count(self) -> int:
        return sum(1 for message in self.messages if message["role"] == "user")


def _tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}
