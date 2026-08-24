"""The interactive loop.

`prompt_toolkit` rather than `input()` for history, multi-line paste handling
and a stable prompt while text streams above it.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import prompt as ask_line
from prompt_toolkit.styles import Style

from andromeda_agent import AgentError, ApprovalRequest, Callbacks, build_provider
from andromeda_agent.approval import Answer
from andromeda_tools import ToolResult, ToolSpec

from . import bootstrap
from . import checkpoints as checkpoint_module
from . import config as config_module
from . import output
from . import render
from . import sessions as sessions_store
from .state import live as live_module
from .session import build_conversation, set_asker, set_lane_announcer

PROMPT_STYLE = Style.from_dict({"prompt": "bold cyan", "meter": "#666666"})

# How long after a session starts the injected-input guard stays armed, and
# what it treats as impossible to have typed.
#
# `bootstrap.drain_pending_input` handles input already in the buffer when the
# session starts. It cannot handle input that arrives a second *later*, which
# is what an editor's venv auto-activation actually does — it writes into the
# terminal after the shell has settled, by which time the prompt is drawn and
# `prompt_toolkit` is reading. So there is a second layer, and it is a timing
# argument rather than a content one: **a long line that arrives faster than a
# person could type it, in the first seconds of a session, was not typed.**
#
# Deliberately narrow, because the cost of a false positive is eating something
# somebody meant:
#   - armed only for the first few seconds, so a paste later in a session is
#     never touched;
#   - only for lines long enough to be a real command — `y`, `/help` and `n`
#     are plausible at any speed and trivial to retype;
#   - it says what it ignored, and the line is in the history, so nothing is
#     lost silently.
GUARD_SECONDS = 3.0
MIN_TYPING_SECONDS = 0.25
MIN_GUARDED_LENGTH = 12


def looks_injected(text: str, typing_seconds: float, session_seconds: float) -> bool:
    """Whether this line arrived too fast, too early, to have been typed.

    `typing_seconds` is measured **from the first character to Enter**, not
    from when the prompt appeared. That distinction is the whole fix: an
    injected line can land at any moment after the prompt is drawn, so
    prompt-to-Enter says nothing — it is just how long the terminal sat idle
    first. First-character-to-Enter is the thing a human cannot fake: sixty
    characters take seconds to type and arrive in one write when something
    else sends them.
    """
    if session_seconds > GUARD_SECONDS:
        return False
    if len(text.strip()) < MIN_GUARDED_LENGTH:
        return False
    return typing_seconds < MIN_TYPING_SECONDS


class _TypingClock:
    """When the first character of the current line arrived.

    Attached to the buffer rather than inferred, because there is no other way
    to know: `PromptSession.prompt()` returns at Enter and tells you nothing
    about what happened in between.
    """

    def __init__(self, session: "PromptSession[str]") -> None:
        self.first_change_at: float | None = None
        session.default_buffer.on_text_changed += self._changed

    def _changed(self, buffer) -> None:
        # Only a change that leaves actual text counts. `prompt_toolkit` fires
        # this once when it clears the buffer to open a new prompt, and taking
        # *that* as the first keystroke starts the clock before anything has
        # been typed — which makes every line look slow and the guard useless.
        if self.first_change_at is None and buffer.text:
            self.first_change_at = time.monotonic()

    def elapsed(self) -> float:
        """Seconds from the first character to now. Large when nothing typed."""
        if self.first_change_at is None:
            # No text change at all — a bare Enter, or a line delivered whole
            # by something that bypassed the buffer. Neither is typing, and the
            # length floor decides whether it matters.
            return 0.0
        return time.monotonic() - self.first_change_at

    def reset(self) -> None:
        self.first_change_at = None


def _prompt_fragments(conversation):
    """The prompt, with a context gauge when it starts to matter.

    Hidden below a third of the window: a meter that is always full-width and
    always empty teaches nobody anything, and the number only becomes
    actionable as it climbs.
    """
    used = conversation.context_used
    if used < 0.33:
        return [("class:prompt", "› ")]
    return [
        ("class:meter", f"{render.context_meter(used, width=8)} {int(used * 100)}%  "),
        ("class:prompt", "› "),
    ]


def _mention_state_health() -> None:
    """Say something only when the session index needs attention.

    A stale index makes `session_search` answer "nothing found", which reads as
    the truth — so this is checked automatically where the rest of `sessions
    doctor` is not. Once a day, and silent when there is nothing to say.
    """
    try:
        from . import __version__
        from . import state

        findings = state.startup_check(__version__)
    except Exception:  # noqa: BLE001 - never fail a session over a health check
        return
    for line in findings.lines:
        output.info(f"  {line}")


def _mention_suggestions() -> None:
    """One line, only when there is something waiting.

    Suggestions that live behind a command nobody runs are suggestions that do
    not exist. Counted rather than listed: the banner is not the place to make
    a decision, and reading the file is cheap.
    """
    try:
        from andromeda_agent.suggestions import Suggestions

        from .session import schedule_path

        pending = Suggestions(schedule_path().parent / "suggestions.json").pending()
    except Exception:  # noqa: BLE001 - never fail a session over a hint
        return
    if pending:
        output.info(
            f"  {len(pending)} automation(s) suggested · andromeda cron suggest"
        )


def _cleanup(conversation) -> None:
    """Do not leave a dev server holding a port after the session ends."""
    binding = getattr(conversation, "binding", None)
    if binding is not None:
        live_module.release(binding.record.id)
    registry = getattr(conversation, "process_registry", None)
    killed = registry.shutdown_all() if registry else 0
    if killed:
        render.note(f"stopped {killed} background process(es)")
    for server in getattr(conversation, "mcp_servers", None) or []:
        server.close()


def _report_running_lanes(conversation) -> None:
    """Say what is still working when the model stops talking.

    Without this a background lane the model forgot to wait for simply vanishes,
    and its work is billed and discarded.
    """
    registry = getattr(conversation, "lane_registry", None)
    if registry is None:
        return
    running = registry.running
    processes = getattr(conversation, "process_registry", None)
    still = processes.running if processes else []
    if still:
        render.note(
            f"{len(still)} background process(es) running: "
            + ", ".join(process.id for process in still)
        )
    if running:
        render.note(
            f"{len(running)} lane(s) still running: "
            + ", ".join(lane.id for lane in running)
            + " — ask me to wait for them"
        )

SLASH_HELP = """
  /help      show this
  /new       start a fresh conversation
  /rewind    undo the last exchange (/rewind N for a numbered checkpoint)
  /history   list the checkpoints you can rewind to
  /ps        background processes started this session
  /recap     what has happened so far, without asking the model
  /sessions  search past sessions (/sessions <text>)
  /resume    switch to another session (/resume lists them)
  /tools     list the tools this session can use
  /skills    list the skills on this machine
  /lanes     list the delegation specialists
  /credits   show the credit balance as of the last call
  /model     show the model in use
  /think     show or set the thinking level (off, low, medium, high)
  /cwd       show the workspace root
  /exit      leave (Ctrl-D also works)
"""


def _history_file() -> FileHistory:
    root = config_module.home()
    root.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(Path(root) / "history"))


def run(
    config: dict[str, Any],
    workspace_root: str | None = None,
    resume: "sessions_store.Session | None" = None,
) -> int:
    # First, before anything slow. Anything already in the terminal's input
    # buffer was typed before this process asked for input, so it was not typed
    # at this process — see `bootstrap.drain_pending_input`. Building a
    # provider can take a moment, and that moment is exactly the window an
    # editor's venv auto-activation lands in.
    drained = bootstrap.drain_pending_input()

    try:
        provider = build_provider(config)
    except AgentError as exc:
        output.agent_error(exc)
        return 1

    conversation, record = build_conversation(
        config,
        provider,
        interactive=True,
        workspace_root=workspace_root,
        session=resume,
    )

    # The credit balance is unknown until a call has been made, so it is absent
    # on the very first line and present on every session after. Showing an
    # unknown balance as "$0.00" would be a lie at exactly the wrong moment.
    from andromeda_agent import credits as _credits

    output.banner(
        model=provider.model,
        lane=provider.label,
        extra=_credits.summary(getattr(provider, "balance", _credits.Balance())),
    )
    output.info(f"  {conversation.workspace.root}")
    output.info(
        f"  {len(conversation.available)} tools · approval: {conversation.policy.mode}"
        f" · thinking: {provider.thinking}"
    )
    if resume is not None:
        output.info(f"  resumed {record.id} · {record.turns} turns")
    from .commands import sessions as sessions_cmd

    sessions_cmd.announce_holder(record.id)
    live_module.claim(
        record.id, surface="repl", workspace=str(conversation.workspace.root)
    )
    if drained == 1:
        output.info("  (ignored input that arrived before the prompt)")
    _mention_state_health()
    _mention_suggestions()
    output.console.print()

    set_asker(_make_asker())
    set_lane_announcer(
        lambda specialist, label: output.console.print(
            f"\n  [magenta]⇢ {specialist} lane[/magenta] [dim]{label}[/dim]"
        )
    )

    checkpoints = checkpoint_module.CheckpointStack.from_json(
        resume.checkpoints if resume is not None else None
    )

    session: PromptSession[str] = PromptSession(history=_history_file())

    session_started = time.monotonic()
    clock = _TypingClock(session)

    while True:
        try:
            clock.reset()
            line = session.prompt(_prompt_fragments(conversation), style=PROMPT_STYLE)
        except KeyboardInterrupt:
            # Ctrl-C clears the current line rather than exiting, matching every
            # other REPL. Ctrl-D is the way out.
            continue
        except EOFError:
            render.console.print()
            _cleanup(conversation)
            return 0

        prompt = line.strip()
        if not prompt:
            continue

        if looks_injected(
            prompt, clock.elapsed(), time.monotonic() - session_started
        ):
            # Not typed at us. Said out loud rather than swallowed, and the
            # line is in the history, so nothing is lost if this is wrong.
            output.info("  (ignored a line that arrived too fast to have been typed)")
            output.info(f"  [{prompt[:70]}]")
            continue

        if prompt.startswith("/"):
            outcome = _slash(prompt, conversation, checkpoints)
            if outcome == "exit":
                _cleanup(conversation)
                return 0
            if outcome == "rewound":
                # The transcript on disk must match the one in memory, or
                # resuming would restore the turns that were just undone.
                record = conversation.binding.record
                record.messages = conversation.messages
                record.checkpoints = checkpoints.to_json()
                record.save()
            if outcome == "switched":
                record = conversation.binding.record
                # The checkpoint stack belongs to the transcript, not to the
                # terminal: rewinding after a switch must undo turns from the
                # session now on screen, never from the one just left.
                checkpoints = checkpoint_module.CheckpointStack.from_json(
                    record.checkpoints
                )
                live_module.claim(
                    record.id,
                    surface="repl",
                    workspace=str(conversation.workspace.root),
                )
                render.note(
                    f"now in {record.id} · {record.turns} turns · {record.title}"
                )
            continue

        # Taken before the turn, so rewinding lands where you were when you
        # asked — not after the answer you want to discard.
        checkpoints.take(conversation.messages, prompt)
        # Persisted alongside the transcript on the same schedule, so a crash
        # cannot leave a session whose checkpoints describe a different run.
        # Read through the binding rather than the local, which /resume moves.
        conversation.binding.record.checkpoints = checkpoints.to_json()
        live_module.beat(conversation.binding.record.id)

        try:
            render.console.print()
            # A fresh stream per turn: the Live region owns the screen while it
            # is open, so it has to close before the next prompt is drawn.
            with render.AnswerStream() as stream:
                globals()["_active_stream"] = stream
                try:
                    conversation.send(prompt, _callbacks(stream))
                finally:
                    globals()["_active_stream"] = None
            render.console.print()
            _report_running_lanes(conversation)
        except AgentError as exc:
            output.console.print()
            output.agent_error(exc)
        except KeyboardInterrupt:
            # Abandoning a stream is expected. The relay settles the credit
            # reservation from the usage frame it already saw, so an interrupted
            # turn is billed for what it produced, not held open.
            output.console.print("\n[dim]interrupted[/dim]")


_active_stream: "render.AnswerStream | None" = None


def _make_asker():
    """Put questions to the person, one at a time.

    Batched questions are asked in sequence rather than on one form: a terminal
    has no form, and inventing a multi-field editor here would be a worse
    version of what the desktop app already does well.
    """

    def ask(questions):
        answers = []
        with _suspend():
            render.console.print()
            for index, question in enumerate(questions, start=1):
                prefix = f"[muted]{index}/{len(questions)}[/muted] " if len(questions) > 1 else ""
                render.console.print(f"  {prefix}[accent]{question.text}[/accent]")
                answers.append(_answer(question))
            render.console.print()
        return answers

    return ask


def _answer(question) -> str:
    if not question.choices:
        try:
            return ask_line("    › ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    for index, choice in enumerate(question.choices, start=1):
        label = " [muted](recommended)[/muted]" if index == 1 else ""
        render.console.print(f"    [accent]{index}[/accent]  {choice}{label}")
    render.console.print("    [muted]pick a number, or type your own answer[/muted]")

    while True:
        try:
            raw = ask_line("    › ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if not raw:
            # Empty takes the recommendation, which is why order matters.
            return question.choices[0]
        if raw.isdigit() and 1 <= int(raw) <= len(question.choices):
            return question.choices[int(raw) - 1]
        # Anything else is the "Other" case: a typed answer is an answer.
        return raw


@contextmanager
def _suspend():
    """Hand the screen back to whatever needs to read from the user."""
    if _active_stream is None:
        yield
        return
    with _active_stream.paused():
        yield


def _callbacks(stream: "render.AnswerStream") -> Callbacks:
    return Callbacks(
        on_text=stream.feed,
        on_tool_start=_tool_start,
        on_tool_result=_tool_result,
        on_tool_denied=lambda spec, reason: render.console.print(
            f"    [bad]declined[/bad] [muted]{spec.name} — {reason}[/muted]"
        ),
        on_compaction=_compacted,
        ask_approval=_approve,
    )


def _tool_start(spec: ToolSpec, arguments: dict[str, Any]) -> None:
    render.tool_call(spec.summary(arguments), spec.risk_tier)


def _tool_result(spec: ToolSpec, result: ToolResult) -> None:
    first = result.display.splitlines()[0] if result.display else ""
    render.tool_result(first, result.ok)


def _compacted(result) -> None:
    if result.stage == "prune":
        render.note(f"compacted — cleared {result.pruned_results} old tool results")
    else:
        render.note(
            f"compacted — summarised {result.summarised_messages} earlier messages"
        )


def _approve(request: ApprovalRequest) -> Answer:
    """Ask the person.

    The summary is shown verbatim — for `terminal` that is the command itself.
    A prompt that paraphrases what it is asking about is not consent.
    """
    with _suspend():
        render.console.print()
        render.console.print(
            f"  [warn]⚠ {request.spec.name}[/warn] [muted]{request.spec.risk_tier}[/muted]"
        )
        for line in request.summary.splitlines():
            render.console.print(f"    {line}", markup=False, highlight=False)
        hint = "  [muted]y = once · a = this session · ! = always · n = no[/muted]"
        allowlist = getattr(request, "allowlist", None)
        if allowlist is not None and allowlist.should_suggest(request.spec.name):
            approvals = allowlist.approvals_of(request.spec.name)
            render.console.print(
                f"  [muted]you have approved this {approvals} times — "
                f"! stops the asking[/muted]"
            )
        render.console.print(hint)

        while True:
            try:
                answer = ask_line("  › ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # An interrupted prompt is a refusal. Anything else would treat
                # walking away from the keyboard as consent.
                render.console.print("  [muted]declined[/muted]")
                return "no"
            if answer in {"y", "yes"}:
                return "once"
            if answer in {"a", "session"}:
                return "session"
            if answer in {"!", "always"}:
                return "always"
            if answer in {"never"}:
                return "never"
            if answer in {"n", "no", ""}:
                return "no"
            render.console.print("  [muted]y, a, !, or n[/muted]")


def _resume(conversation, arguments: list[str]) -> str:
    """Switch this terminal to another transcript, or list the candidates.

    Only the transcript moves. The registry, the policy, the provider, the
    browser and any running lanes belong to this terminal and stay exactly as
    they are — which is what makes this safe to do mid-run, and what stops it
    from being a second, subtly different way of starting a session.

    The list is numbered as well as addressable by id, because a twelve-hex-
    digit id is not something anyone retypes correctly the first time.
    """
    from . import sessions as store

    binding = getattr(conversation, "binding", None)
    if binding is None:
        output.fail("This surface cannot switch sessions.")
        return "continue"

    recent = [
        session
        for session in store.recent(limit=10)
        if session.id != binding.record.id
    ]

    if not arguments:
        if not recent:
            output.info("No other sessions to switch to.")
            return "continue"
        for number, session in enumerate(recent, start=1):
            render.console.print(
                f"  [accent]{str(number).rjust(2)}[/accent]  "
                f"[muted]{session.id}  {str(session.turns).rjust(3)} turns[/muted]  "
                f"{session.title}"
            )
        render.console.print("  [muted]/resume <number or id>[/muted]")
        return "continue"

    choice = arguments[0].strip()
    target = None
    if choice.isdigit() and 1 <= int(choice) <= len(recent):
        target = recent[int(choice) - 1]
    else:
        target = store.resolve(choice)
    if target is None:
        output.fail(f"No session matching {choice!r}.", "/resume lists them.")
        return "continue"
    if target.id == binding.record.id:
        output.info("Already in that session.")
        return "continue"

    binding.switch(target, conversation.messages)
    # The transcript replaces the whole message list, its original system
    # message included. Rewriting that would silently change the rules the
    # earlier turns were produced under — the same reason `--resume` does not.
    conversation.messages = list(target.messages)
    return "switched"


def _slash(command: str, conversation, checkpoints=None) -> str:
    parts = command.split()
    verb = parts[0].lower()
    if verb in {"/exit", "/quit"}:
        return "exit"
    if verb == "/help":
        output.console.print(SLASH_HELP)
    elif verb == "/new":
        conversation.reset()
        output.ok("New conversation.")
    elif verb == "/tools":
        for spec in sorted(conversation.available, key=lambda item: item.name):
            decision = conversation.policy.decide(spec)
            note = "asks first" if decision == "needs_approval" else "auto"
            output.console.print(
                f"  [cyan]{spec.name.ljust(14)}[/cyan] [dim]{spec.risk_tier.ljust(12)} {note}[/dim]"
            )
    elif verb == "/credits":
        from andromeda_agent import credits as credits_module

        balance = getattr(getattr(conversation, "provider", None), "balance", None)
        line = credits_module.summary(balance) if balance else ""
        if not line:
            # Three different reasons land here and they are not the same, so
            # the message says which rather than showing a confident zero: the
            # BYOK lane has no account to have a balance, a session that has
            # not called yet has nothing to report, and a deployment that does
            # not stamp the headers is unknown rather than empty.
            provider = getattr(conversation, "provider", None)
            if provider is not None and provider.name != "relay":
                output.info("No balance on this lane — you are using your own key.")
            else:
                output.info("No balance yet. It is read from the next reply.")
        else:
            render.console.print(f"  [accent]{line}[/accent]")
            if balance.used_micros is not None:
                render.console.print(
                    f"  [muted]{credits_module.format_micros(balance.used_micros)}"
                    " used this period[/muted]"
                )
    elif verb == "/rewind":
        if checkpoints is None or not len(checkpoints):
            output.info("Nothing to rewind to yet.")
            return "continue"
        index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target = checkpoints.resolve(index)
        if target is None:
            output.fail(f"No checkpoint {parts[1]!r}.", "/history lists them.")
            return "continue"
        conversation.messages = checkpoints.rewind_to(target)
        render.note(f"rewound to before: {target.label}")
        return "rewound"
    elif verb == "/history":
        if checkpoints is None or not len(checkpoints):
            output.info("No checkpoints yet.")
            return "continue"
        for checkpoint in checkpoints.all():
            render.console.print(
                f"  [accent]{str(checkpoint.index).rjust(3)}[/accent]  "
                f"[muted]{str(checkpoint.turns).rjust(2)} turns[/muted]  {checkpoint.label}"
            )
        render.console.print("  [muted]/rewind N[/muted]")
    elif verb == "/ps":
        registry = getattr(conversation, "process_registry", None)
        processes = registry.all() if registry else []
        if not processes:
            output.info("No background processes.")
        for process in processes:
            render.console.print(f"  [muted]{process.summary()}[/muted]")
    elif verb == "/recap":
        from . import state

        summary = state.build_recap(
            conversation.messages, getattr(conversation, "todos", None)
        )
        for line in summary.lines():
            render.console.print(f"  [muted]{line}[/muted]")
    elif verb == "/sessions":
        from . import state

        query = " ".join(parts[1:]).strip()
        if not query:
            output.fail("/sessions <text>", "Searches every past session.")
            return "continue"
        hits = state.search(query, limit=8)
        if not hits:
            output.info(f"Nothing found for {query!r}.")
            return "continue"
        for hit in hits:
            marked = (
                " ".join(hit.snippet.split())
                .replace("»", "[bold yellow]")
                .replace("«", "[/bold yellow]")
            )
            render.console.print(
                f"  [accent]{hit.session_id}[/accent][muted]@{hit.position}"
                f"  {hit.role.ljust(9)}[/muted] {marked}"
            )
        render.console.print("  [muted]/resume <id> to switch to one[/muted]")
    elif verb == "/resume":
        return _resume(conversation, parts[1:])
    elif verb == "/skills":
        from andromeda_tools import skills as skills_module

        found = skills_module.discover(conversation.workspace.root)
        if not found:
            output.info("No skills found.")
        for skill in sorted(found.values(), key=lambda item: item.name):
            state = "" if skill.available else f" [yellow]needs {', '.join(skill.missing_bins)}[/yellow]"
            output.console.print(
                f"  [cyan]{skill.name.ljust(18)}[/cyan] [dim]{skill.description[:80]}[/dim]{state}"
            )
    elif verb == "/lanes":
        from andromeda_agent.specialists import SPECIALISTS

        for belt in SPECIALISTS.values():
            output.console.print(
                f"  [magenta]{belt.id.ljust(10)}[/magenta] [dim]{belt.max_turns} steps · "
                f"{belt.purpose}[/dim]"
            )
    elif verb == "/think":
        from andromeda_agent.models import THINKING_LEVELS, supports_reasoning

        if not supports_reasoning(conversation.provider.model):
            output.info(f"{conversation.provider.model} does not support thinking levels.")
            return "continue"
        if len(parts) > 1:
            level = parts[1].lower()
            if level not in THINKING_LEVELS:
                output.fail(
                    f"Unknown level {level!r}.", f"One of: {', '.join(THINKING_LEVELS)}"
                )
                return "continue"
            # Changed on the provider, so it takes effect on the next turn
            # without rebuilding the session and losing the transcript.
            conversation.provider.thinking = level
            output.ok(f"thinking: {level}")
        else:
            output.info(f"thinking: {conversation.provider.thinking}")
    elif verb == "/model":
        output.info(f"{conversation.provider.label} · {conversation.provider.model}")
    elif verb == "/cwd":
        output.console.print(str(conversation.workspace.root), soft_wrap=True)
    else:
        output.fail(f"Unknown command {verb}", "/help lists them.")
    return "continue"
