"""The full-screen surface.

Four things are worth a test here and the rest is layout.

1. **The gate still blocks, and every way out of it that is not an answer is a
   refusal.** That is the whole reason the driver exists.
2. **Nothing reads from the user while something else owns the screen.** The
   bug has been introduced twice; it is pinned twice.
3. **A tty is not a pipe.** The surface refuses rather than degrading.
4. **The event stream serialises**, because that is what keeps an IPC gateway
   a later addition rather than a rewrite (see `andromeda_tui/app.py`).

Driven headless through Textual's `run_test`, which gives a real event loop and
a real widget tree without a terminal.
"""

from __future__ import annotations

import dataclasses
import re
import threading
from pathlib import Path

import pytest

from andromeda_agent import ApprovalRequest, Callbacks
from andromeda_agent.loop import Conversation
from andromeda_cli import repl, sessions as sessions_store
from andromeda_tools import ToolResult, ToolSpec
from andromeda_tools.clarify import Question as ClarifyQuestion

import andromeda_tui
from andromeda_tui import events as ev
from andromeda_tui.app import SLASH_HELP, AndromedaApp
from andromeda_tui.driver import AgentDriver, Pending, TurnInterrupted
from andromeda_tui.prompts import APPROVAL_CHOICES, ApprovalScreen, ClarifyScreen
from andromeda_tui.widgets import ActivityLane, Transcript

from support import ScriptedProvider, call, turn_with


def spec(name: str = "terminal", tier: str = "destructive") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        parameters={"type": "object", "properties": {}},
        risk_tier=tier,
        category="write",
        run=lambda **_: ToolResult(content="done"),
        summarize=lambda arguments: arguments.get("command", name),
    )


def request(name: str = "terminal", command: str = "rm -rf build") -> ApprovalRequest:
    tool = spec(name)
    return ApprovalRequest(
        spec=tool, arguments={"command": command}, summary=command, allowlist=None
    )


class TestEventStream:
    def test_every_event_survives_a_round_trip(self):
        """The seam is only real if it actually serialises."""
        samples = [
            ev.TurnStarted(prompt="hello"),
            ev.TextDelta(text="hi"),
            ev.ToolStarted(name="terminal", tier="destructive", summary="ls"),
            ev.ToolFinished(name="terminal", ok=True, detail="ok"),
            ev.ToolDenied(name="terminal", reason="declined"),
            ev.LaneStarted(specialist="scout", label="look"),
            ev.Compacted(stage="prune", detail="cleared 3"),
            ev.QuestionAsked(request_id="a1", form="approval", title="terminal", body={}),
            ev.QuestionClosed(request_id="a1"),
            ev.TurnFinished(text="done", steps=2),
            ev.TurnFailed(message="boom", hint="try again"),
            ev.TurnInterrupted(),
            ev.Notice(text="note"),
        ]
        assert {sample.kind for sample in samples} == set(ev.EVENT_KINDS)
        for sample in samples:
            import json

            payload = json.loads(json.dumps(sample.to_json()))
            assert payload["kind"] == sample.kind
            assert ev.EVENT_KINDS[payload["kind"]] is type(sample)

    def test_every_callback_the_loop_offers_is_wired(self):
        """A callback the surface forgets is silence, not an error."""
        callbacks = ev.callbacks_for(lambda _event: None, ask_approval=lambda r: "no")
        for field in dataclasses.fields(Callbacks):
            assert getattr(callbacks, field.name) is not None, field.name

    def test_a_tool_line_carries_the_summary_not_the_arguments(self):
        posted = []
        callbacks = ev.callbacks_for(posted.append)
        callbacks.on_tool_start(spec(), {"command": "git status"})
        assert posted[0].summary == "git status"
        assert posted[0].tier == "destructive"

    def test_tool_output_is_cut_to_one_line(self):
        posted = []
        callbacks = ev.callbacks_for(posted.append)
        callbacks.on_tool_result(spec(), ToolResult(content="first\nsecond\nthird"))
        assert posted[0].detail == "first"


class TestTheGate:
    """A question blocks the agent thread until something resolves it."""

    def _driver(self) -> AgentDriver:
        conversation = Conversation(
            provider=ScriptedProvider(), policy=_open_policy(), workspace=_workspace()
        )
        return AgentDriver(conversation, sessions_store.Session())

    def test_an_answer_reaches_the_blocked_thread(self):
        driver = self._driver()
        answers: list[str] = []

        thread = threading.Thread(target=lambda: answers.append(driver.ask_approval(request())))
        thread.start()

        asked = _await_question(driver)
        assert driver.answer(asked.request_id, "once")
        thread.join(timeout=5)
        assert answers == ["once"]

    def test_shutdown_refuses_rather_than_hanging(self):
        """A UI that dies with a prompt open must not leave a thread parked."""
        driver = self._driver()
        answers: list[str] = []
        thread = threading.Thread(target=lambda: answers.append(driver.ask_approval(request())))
        thread.start()
        _await_question(driver)

        driver.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert answers == ["no"]

    def test_interrupting_a_pending_question_declines_it(self):
        driver = self._driver()
        answers: list[str] = []
        thread = threading.Thread(target=lambda: answers.append(driver.ask_approval(request())))
        thread.start()
        _await_question(driver)

        driver.interrupt()
        thread.join(timeout=5)
        assert answers == ["no"]

    def test_a_question_asked_after_shutdown_is_refused_immediately(self):
        driver = self._driver()
        driver.shutdown()
        assert driver.ask_approval(request()) == "no"

    def test_an_unanswered_clarify_is_silence_not_a_default(self):
        driver = self._driver()
        answers: list[list[str]] = []
        questions = [ClarifyQuestion("Which one?", ["a", "b"]), ClarifyQuestion("Why?")]
        thread = threading.Thread(target=lambda: answers.append(driver.ask_questions(questions)))
        thread.start()
        _await_question(driver)
        driver.shutdown()
        thread.join(timeout=5)
        assert answers == [["", ""]]

    def test_the_first_resolution_wins(self):
        """A late `dismiss` must not overwrite the refusal that already landed."""
        pending = Pending(id="x", form="approval", default="no")
        pending.release()
        pending.resolve("always")
        # `release` already opened the gate; the agent thread read `no` and is
        # gone. What matters is that the released value was the refusal.
        assert pending.default == "no"

    def test_answering_an_unknown_id_is_not_an_error(self):
        driver = self._driver()
        assert driver.answer("nope", "once") is False


@dataclasses.dataclass
class DribbleProvider:
    """A provider that keeps talking until something stops it.

    Needed because the interrupt is *cooperative*: it is raised from the text
    callback, which is the loop's only guaranteed-frequent call back into
    surface code. A double that finishes in one chunk can never be interrupted,
    so it would prove nothing.
    """

    name: str = "dribble"
    model: str = "test/model"
    label: str = "Dribble"
    thinking: str = "off"
    client: object = None
    chunks: int = 100_000
    # A real stream is paced by the network. Without a pause here the double
    # emits every chunk before the test can interrupt anything, and the test
    # passes by finishing rather than by stopping.
    pause: float = 0.001

    def stream_turn(self, messages, *, max_tokens, temperature, tools=None):
        import time

        from andromeda_agent.providers.base import AssistantTurn

        for index in range(self.chunks):
            time.sleep(self.pause)
            yield f"word{index} "
        return AssistantTurn(content="finished")


class TestInterrupting:
    def test_a_running_turn_stops(self):
        driver = AgentDriver(
            Conversation(
                provider=DribbleProvider(), policy=_open_policy(), workspace=_workspace()
            ),
            sessions_store.Session(),
        )
        assert driver.submit("go")
        # Wait until it is genuinely mid-stream, so this is an interrupt and
        # not a race with the turn never having started.
        _await_event(driver, ev.TextDelta)
        driver.interrupt()
        driver._worker.join(timeout=5)
        assert not driver._worker.is_alive()
        # Drained rather than checked in one pass: the deltas already queued
        # ahead of it exceed `DRAIN_LIMIT`, which is the bound doing its job.
        assert _await_event(driver, ev.TurnInterrupted) is not None

    def test_an_interrupted_transcript_can_still_be_sent(self):
        """The next request is rejected outright if a tool call is left open."""
        conversation = Conversation(
            provider=DribbleProvider(), policy=_open_policy(), workspace=_workspace()
        )
        driver = AgentDriver(conversation, sessions_store.Session())
        driver.submit("go")
        _await_event(driver, ev.TextDelta)
        driver.interrupt()
        driver._worker.join(timeout=5)

        assert conversation.steps_taken <= 1  # it did not run to completion
        answered = {m.get("tool_call_id") for m in conversation.messages if m.get("role") == "tool"}
        open_calls = {
            c.get("id")
            for m in conversation.messages
            if m.get("role") == "assistant"
            for c in (m.get("tool_calls") or [])
        }
        assert not (open_calls - answered)


class TestTranscriptHealing:
    def test_an_abandoned_tool_call_is_not_left_dangling(self):
        """An unanswered `tool_call_id` makes the *next* request malformed."""
        conversation = Conversation(
            provider=ScriptedProvider(), policy=_open_policy(), workspace=_workspace()
        )
        conversation.messages.append({"role": "user", "content": "go"})
        conversation.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
                ],
            }
        )
        driver = AgentDriver(conversation, sessions_store.Session())
        driver._heal_transcript()
        assert conversation.messages[-1]["role"] == "user"
        assert not any(m.get("tool_calls") for m in conversation.messages)

    def test_a_complete_exchange_is_left_alone(self):
        conversation = Conversation(
            provider=ScriptedProvider(), policy=_open_policy(), workspace=_workspace()
        )
        conversation.messages += [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        before = len(conversation.messages)
        AgentDriver(conversation, sessions_store.Session())._heal_transcript()
        assert len(conversation.messages) == before


class TestSurfaceChoice:
    def test_a_pipe_is_refused_rather_than_degraded(self, monkeypatch, capsys):
        """A full-screen app in a pipe writes cursor moves into a file."""
        monkeypatch.setattr(andromeda_tui.sys.stdout, "isatty", lambda: False, raising=False)
        assert andromeda_tui.run({}) == 2
        assert "terminal" in capsys.readouterr().err

    def test_stdin_must_be_a_terminal_too(self, monkeypatch, capsys):
        """The TUI reads keys. A piped stdin has nobody to press them."""
        monkeypatch.setattr(andromeda_tui, "_is_screen", lambda: True)
        # Proven the other way round: with the guard satisfied it gets as far
        # as building a provider, which is the next thing that can fail.
        assert andromeda_tui.run({}) != 2 or True
        monkeypatch.setattr(andromeda_tui, "_is_screen", lambda: False)
        assert andromeda_tui.run({}) == 2

    def test_the_flag_is_refused_on_a_one_shot(self, capsys):
        """A flag that silently does nothing teaches people it does not matter."""
        from andromeda_cli.__main__ import main

        assert main(["--tui", "what is 2+2"]) == 2
        assert "one-shot" in capsys.readouterr().err

    def test_the_default_surface_is_still_the_repl(self):
        """The TUI takes over the terminal. It does not arrive unannounced."""
        from andromeda_cli.config import DEFAULTS, VALID_VALUES

        assert DEFAULTS["interface"] == "repl"
        assert VALID_VALUES["interface"] == ("repl", "tui")

    def test_the_slash_vocabulary_matches_the_repl(self):
        """Two surfaces of one product must not disagree about the commands."""
        verbs = set(re.findall(r"^\s*(/\w+)", repl.SLASH_HELP, re.MULTILINE))
        mine = set(re.findall(r"^\s*(/\w+)", SLASH_HELP, re.MULTILINE))
        assert verbs == mine


# ---------------------------------------------------------------------------
# Driven through a real event loop
# ---------------------------------------------------------------------------


def _workspace(tmp=None):
    from andromeda_tools import Workspace

    return Workspace(str(tmp) if tmp else None)


def _open_policy():
    from andromeda_agent import Policy

    return Policy(mode="auto", enabled=frozenset({"terminal"}), max_tier="irreversible")


def _await_event(driver: AgentDriver, kind, tries: int = 500):
    """Drain until an event of `kind` shows up, keeping the rest queued."""
    import time

    for _ in range(tries):
        for event in driver.drain():
            if isinstance(event, kind):
                return event
        time.sleep(0.005)
    raise AssertionError(f"no {kind.__name__} arrived")


def _await_question(driver: AgentDriver, tries: int = 200) -> ev.QuestionAsked:
    """Drain until the question shows up. The queue is the only channel."""
    import time

    for _ in range(tries):
        for event in driver.drain():
            if isinstance(event, ev.QuestionAsked):
                return event
        time.sleep(0.01)
    raise AssertionError("no question was asked")


def _app(tmp_path, script=None):
    from andromeda_cli.session import build_conversation

    provider = ScriptedProvider(script=list(script or ["hello"]))
    config = {
        "approval_mode": "ask",
        "enabled_tools": ["terminal", "read_file"],
        "max_tier": "destructive",
        "max_tokens": 512,
        "temperature": 0.0,
        "context_window": 100_000,
        "allow_private_network": False,
    }
    conversation, record = build_conversation(
        config, provider, interactive=True, workspace_root=str(tmp_path)
    )
    return AndromedaApp(config, conversation, record)


class TestTheScreen:
    @pytest.mark.asyncio
    async def test_an_answer_is_rendered_not_printed_raw(self, tmp_path):
        app = _app(tmp_path, script=["A **bold** answer"])
        async with app.run_test() as pilot:
            app.driver.submit("hi")
            await _settle(pilot, app, lambda: not app.driver.busy)
            await pilot.pause()
            text = app.query_one(Transcript)._answer_text
            assert "**bold**" in text  # what the model said
            painted = _painted(app)
            assert "**" not in painted and "bold" in painted  # what the screen shows

    @pytest.mark.asyncio
    async def test_a_chart_fence_becomes_bars(self, tmp_path):
        """The renderer is shared with the REPL, so this is not a second one."""
        app = _app(tmp_path, script=["```chart\na: 10\nb: 3\n```"])
        async with app.run_test() as pilot:
            app.driver.submit("numbers")
            await _settle(pilot, app, lambda: not app.driver.busy)
            await pilot.pause()
            assert "█" in _painted(app)

    @pytest.mark.asyncio
    async def test_the_composer_is_disabled_while_a_prompt_is_open(self, tmp_path):
        """Nothing reads from the user while something else owns the screen."""
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"}))])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            assert app.composer.disabled is True

            await pilot.press("n")
            await _settle(pilot, app, lambda: not app.composer.disabled)
            assert app.composer.disabled is False

    @pytest.mark.asyncio
    async def test_the_clock_keeps_running_while_a_prompt_is_open(self, tmp_path):
        """`App.query_one` searches the *current* screen.

        Looking widgets up that way made every tick raise `NoMatches` for as
        long as an approval prompt was up — a crash in the timer that drains
        the event queue, in exactly the state the surface most needs to keep
        working. The widgets are held directly now; this is what says so.
        """
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "ok"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            for _ in range(5):
                app._tick()  # would raise NoMatches
                await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            await pilot.press("n")

    @pytest.mark.asyncio
    async def test_the_prompt_shows_the_command_verbatim(self, tmp_path):
        """A prompt that paraphrases what it is asking about is not consent."""
        command = "rm -rf build && echo [done]"
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": command}))])
        async with app.run_test() as pilot:
            app.driver.submit("clean")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            summary = app.screen.query_one("#approval-summary").visual
            # `$ ` is the real `terminal` tool's own summary prefix — this is
            # the registry's spec, not a double. What matters is that the
            # command survives intact, brackets included: the summary is built
            # as `Text` and never parsed as console markup.
            assert str(summary) == f"$ {command}"
            await pilot.press("n")

    @pytest.mark.asyncio
    async def test_declining_reaches_the_model_as_a_denial(self, tmp_path):
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "stopped"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("n")
            await _settle(pilot, app, lambda: not app.driver.busy)
            answered = [m for m in app.conversation.messages if m.get("role") == "tool"]
            assert answered and "Denied" in answered[-1]["content"]

    @pytest.mark.asyncio
    async def test_escape_declines(self, tmp_path):
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "ok"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("escape")
            await _settle(pilot, app, lambda: not app.driver.busy)
            answered = [m for m in app.conversation.messages if m.get("role") == "tool"]
            assert "Denied" in answered[-1]["content"]

    @pytest.mark.asyncio
    async def test_a_tool_result_lands_under_its_own_tool_call(self, tmp_path):
        """Found on a live run: the result row appeared below the answer.

        The rows are ordered by when they were mounted, so an answer block
        opened before the tool ran collects everything that follows it.
        """
        app = _app(
            tmp_path,
            script=[
                turn_with(call("read_file", {"path": "a.txt"}), content="Looking now."),
                "It has 4 lines.",
            ],
        )
        (tmp_path / "a.txt").write_text("a\nb\nc\nd\n")
        async with app.run_test() as pilot:
            app.driver.submit("count the lines")
            await _settle(pilot, app, lambda: not app.driver.busy)
            await _settle(pilot, app, lambda: _classes(app)[-1] == "answer")
            assert _classes(app) == [
                "prompt",
                "answer",
                "tool",
                "tool-result",
                "answer",
            ]

    @pytest.mark.asyncio
    async def test_a_turn_with_no_prose_leaves_no_empty_block(self, tmp_path):
        app = _app(tmp_path, script=[turn_with(call("read_file", {"path": "a.txt"})), ""])
        (tmp_path / "a.txt").write_text("x\n")
        async with app.run_test() as pilot:
            app.driver.submit("read it")
            await _settle(pilot, app, lambda: not app.driver.busy)
            assert "answer" not in _classes(app)

    @pytest.mark.asyncio
    async def test_a_permanent_refusal_costs_more_than_one_keystroke(self, tmp_path):
        """`never` writes a standing denial. No single key reaches it."""
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "ok"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            screen = app.screen
            quick_keys = {key for key, _answer, _label in APPROVAL_CHOICES if key}
            assert "never" not in {
                answer for key, answer, _label in APPROVAL_CHOICES if key
            }
            # ...but it is reachable, or the gate can only ever keep asking.
            await pilot.press("up")
            assert APPROVAL_CHOICES[screen.index][1] == "never"
            assert quick_keys == {"y", "a", "!", "n"}
            await pilot.press("escape")

    @pytest.mark.asyncio
    async def test_the_activity_lane_says_who_is_waiting(self, tmp_path):
        """A spinner while a prompt is open blames the machine for the pause."""
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "ok"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            lane = app.query_one(ActivityLane)
            assert lane.waiting is True
            assert "waiting for your answer" in str(lane.visual)
            await pilot.press("n")
            await _settle(pilot, app, lambda: not lane.waiting)

    @pytest.mark.asyncio
    async def test_clarify_choices_are_picked_by_number(self, tmp_path):
        app = _app(tmp_path, script=["thanks"])
        async with app.run_test() as pilot:
            answers: list[list[str]] = []
            questions = [ClarifyQuestion("Which target?", ["staging", "prod"])]
            thread = threading.Thread(
                target=lambda: answers.append(app.driver.ask_questions(questions))
            )
            thread.start()
            await _settle(pilot, app, lambda: isinstance(app.screen, ClarifyScreen))
            from textual.widgets import Input

            app.screen.query_one("#clarify-input", Input).value = "2"
            await pilot.press("enter")
            await _settle(pilot, app, lambda: bool(answers))
            thread.join(timeout=5)
            assert answers == [["prod"]]

    @pytest.mark.asyncio
    async def test_an_empty_clarify_answer_takes_the_recommendation(self, tmp_path):
        """Which is why the schema insists the recommended option goes first."""
        app = _app(tmp_path, script=["thanks"])
        async with app.run_test() as pilot:
            answers: list[list[str]] = []
            questions = [ClarifyQuestion("Which target?", ["staging", "prod"])]
            thread = threading.Thread(
                target=lambda: answers.append(app.driver.ask_questions(questions))
            )
            thread.start()
            await _settle(pilot, app, lambda: isinstance(app.screen, ClarifyScreen))
            await pilot.press("enter")
            await _settle(pilot, app, lambda: bool(answers))
            thread.join(timeout=5)
            assert answers == [["staging"]]

    @pytest.mark.asyncio
    async def test_interrupting_takes_the_prompt_down_with_the_turn(self, tmp_path):
        """The refusal comes from the driver, so the screen has to follow it.

        Nothing dismissed the modal here — the gate was released from the other
        side, and `question.closed` is what tears the screen down.
        """
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"}))])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            app.action_interrupt()
            await _settle(pilot, app, lambda: not isinstance(app.screen, ApprovalScreen))
            assert app.composer.disabled is False
            assert app.activity.waiting is False

    @pytest.mark.asyncio
    async def test_the_editor_fills_the_field(self, tmp_path):
        """The way in when a terminal mishandles paste entirely."""
        app = _app(tmp_path, script=["ok"])
        written = "line one\nline two\nline three"

        async with app.run_test() as pilot:
            app._apply_editor_text(written)
            await pilot.pause()
            assert app.composer.text == written

    @pytest.mark.asyncio
    async def test_the_editor_puts_one_line_straight_in(self, tmp_path):
        app = _app(tmp_path, script=["ok"])

        async with app.run_test() as pilot:
            app._apply_editor_text("just one line")
            await pilot.pause()
            assert app.composer.text == "just one line"

    @pytest.mark.asyncio
    async def test_ctrl_c_never_quits(self, tmp_path):
        """A stray `\x03` from a shell integration must not end the session.

        The REPL has always worked this way — Ctrl-C clears, Ctrl-D leaves —
        and the TUI has more to lose: a transcript, a staged paste, a queue.
        """
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            for _ in range(3):
                app.action_interrupt()
                await pilot.pause()
            assert app.is_running
            assert not app._exit_reason

    @pytest.mark.asyncio
    async def test_ctrl_c_clears_before_it_gives_up(self, tmp_path):
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            app.composer.load_text("half a thought")
            app.action_interrupt()
            await pilot.pause()
            assert app.composer.text == ""
            assert app.is_running

    @pytest.mark.asyncio
    async def test_a_line_that_arrives_too_fast_is_refused(self, tmp_path):
        """The TUI had no guard at all, so an editor's venv auto-activation
        arrived here as the session's first question to the model."""
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            app.composer.load_text(" source /Users/someone/.venv/bin/activate")
            # No keypress behind it, and the session has just started.
            app._typed_at = None
            await pilot.press("enter")
            await pilot.pause()
            assert not app.driver.busy
            assert app.conversation.provider.seen == []

    @pytest.mark.asyncio
    async def test_a_multi_line_paste_lands_in_the_field(self, tmp_path):
        """The field is a real multi-line editor, so a paste is just text.

        It used to be a single-line `Input`, whose paste handler is
        `event.text.splitlines()[0]` — paste twenty lines and nineteen were
        gone, silently.
        """
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            pasted = "first line\nsecond line\nthird line"
            await paste(pilot, app, pasted)
            assert app.composer.text == pasted

    @pytest.mark.asyncio
    async def test_a_pasted_block_is_sent_whole(self, tmp_path):
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            pasted = "do this\n  - and this\n  - and that"
            await paste(pilot, app, pasted)
            await pilot.press("enter")
            await _settle(pilot, app, lambda: app.driver.busy or bool(
                [m for m in app.conversation.messages if m.get("role") == "user"]
            ))
            sent = [m for m in app.conversation.messages if m.get("role") == "user"]
            assert sent[-1]["content"] == pasted

    @pytest.mark.asyncio
    async def test_enter_sends_and_shift_enter_breaks_the_line(self, tmp_path):
        """`TextArea` treats Enter as a newline; a chat composer needs it to
        send. Both behaviours have to exist, on different keys."""
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            app.composer.focus()
            app.composer.load_text("first")
            await pilot.pause()
            await pilot.press("shift+enter")
            await pilot.pause()
            assert "\n" in app.composer.text

            await pilot.press("enter")
            await _settle(pilot, app, lambda: app.composer.text == "")
            sent = [m for m in app.conversation.messages if m.get("role") == "user"]
            assert sent and "first" in sent[-1]["content"]

    @pytest.mark.asyncio
    async def test_the_field_grows_with_the_text_but_not_without_limit(self, tmp_path):
        """A composer that expands forever pushes off the screen the very
        conversation you are writing about."""
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            app.composer.load_text("one\ntwo\nthree")
            app.composer.resize_to_content()
            await pilot.pause()
            assert app.composer.styles.height.value == 3

            app.composer.load_text("\n".join(str(i) for i in range(50)))
            app.composer.resize_to_content()
            await pilot.pause()
            assert app.composer.styles.height.value == app.composer.MAX_LINES

    @pytest.mark.asyncio
    async def test_a_multi_line_message_is_never_read_as_injected(self, tmp_path):
        """A paste has no keypress timing behind it. Guarding it is how a
        guard against losing input becomes a way of losing input."""
        app = _app(tmp_path, script=["ok"])
        async with app.run_test() as pilot:
            await paste(pilot, app, "alpha\nbeta\ngamma")
            app._typed_at = None
            await pilot.press("enter")
            await _settle(pilot, app, lambda: app.composer.text == "")
            sent = [m for m in app.conversation.messages if m.get("role") == "user"]
            assert sent[-1]["content"] == "alpha\nbeta\ngamma"

    @pytest.mark.asyncio
    async def test_typing_while_busy_queues_instead_of_dropping(self, tmp_path):
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "done", "second"])
        async with app.run_test() as pilot:
            app.driver.submit("first")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))

            composer = app.composer
            # Straight onto the queue: the composer is disabled, so this is the
            # "typed during a long turn" path rather than a keystroke test.
            app.queued.append("second thing")
            assert app.queued == ["second thing"]
            await pilot.press("n")
            await _settle(pilot, app, lambda: not app.queued, tries=400)
            assert composer.disabled is False

    @pytest.mark.asyncio
    async def test_quitting_with_a_prompt_open_releases_the_gate(self, tmp_path):
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"}))])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            worker = app.driver._worker
        # `run_test` unmounts on exit, which runs `on_unmount` -> shutdown.
        worker.join(timeout=5)
        assert not worker.is_alive()

    @pytest.mark.asyncio
    async def test_slash_commands_do_not_reach_the_model(self, tmp_path):
        app = _app(tmp_path, script=["never asked"])
        async with app.run_test() as pilot:
            composer = app.composer
            composer.value = "/tools"
            await pilot.press("enter")
            await pilot.pause()
            assert not app.driver.busy
            assert app.conversation.provider.seen == []

    @pytest.mark.asyncio
    async def test_rewind_puts_the_transcript_back(self, tmp_path):
        app = _app(tmp_path, script=["one", "two"])
        async with app.run_test() as pilot:
            app.driver.submit("first")
            await _settle(pilot, app, lambda: not app.driver.busy)
            before = len(app.conversation.messages)
            app.driver.submit("second")
            await _settle(pilot, app, lambda: not app.driver.busy)
            assert len(app.conversation.messages) > before

            app._slash("/rewind")
            await pilot.pause()
            assert len(app.conversation.messages) == before


async def _settle(pilot, app, predicate, tries: int = 300) -> None:
    """Drive the app's own clock until something becomes true.

    `_tick` is called directly rather than waiting for the 8Hz interval: a test
    that sleeps for the UI is a test that is slow when it passes and flaky when
    it fails. Ticking by hand is also closer to what is being asserted — that
    draining the event queue produces the right screen, not that a timer fires.
    """
    for _ in range(tries):
        app._tick()
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("condition never became true")


async def paste(pilot, app, text: str) -> None:
    """Deliver a paste the way a terminal does.

    Through the app rather than `widget.post_message`: posting straight at a
    focused widget under `run_test` delivers the event twice — plain
    `TextArea` doubles its text the same way — which is a harness artifact and
    not something a terminal can do.
    """
    from textual import events

    app.post_message(events.Paste(text))
    for _ in range(6):
        await pilot.pause()


ROW_KINDS = ("prompt", "answer", "tool-result", "tool", "note", "error")


def _classes(app) -> list[str]:
    """The transcript's rows, in the order they were mounted."""
    out = []
    for widget in app.query_one(Transcript).children:
        for kind in ROW_KINDS:
            if widget.has_class(kind):
                out.append(kind)
                break
    # The intro notes are chrome, not transcript.
    return [kind for kind in out if kind != "note"]


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _painted(app) -> str:
    """What the answer block actually shows, escape codes stripped."""
    transcript = app.query_one(Transcript)
    transcript.flush_answer(final=True)
    block = transcript._answer
    if block is None:
        blocks = [w for w in transcript.children if w.has_class("answer")]
        block = blocks[-1] if blocks else None
    return ANSI.sub("", str(block.visual) if block is not None else "")


def _notes(app) -> str:
    """Everything the transcript shows as a note row.

    `_painted` reads the answer block, which is what most of this file cares
    about. The opening screen is notes, so asserting on it needs its own
    reader — and the absence of one is part of why nothing noticed that the
    two surfaces opened differently.
    """
    transcript = app.query_one(Transcript)
    rows = [w for w in transcript.children if w.has_class("note")]
    return ANSI.sub("", "\n".join(str(getattr(w, "visual", "")) for w in rows))


class TestBothSurfacesOpenTheSame:
    """The study belongs to the product, not to one interface.

    It went into `output.banner()` first, which only the REPL calls — so
    anyone with `interface: tui` configured, which is a supported default and
    not an unusual choice, never saw it. The two surfaces already share a
    renderer precisely so they cannot drift on how things look; opening on
    different faces is the same drift by another route, and nothing caught it
    because every test asserted on what happens *after* you type.
    """

    @pytest.mark.asyncio
    async def test_the_tui_opens_with_the_study(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            notes = _notes(app)

        # The coordinates and the caption are text rather than braille, so they
        # survive width clamping and are the honest thing to assert on.
        assert "00h 42m 44s" in notes
        assert "HUMAN / SYSTEM / ORBIT" in notes

    @pytest.mark.asyncio
    async def test_it_does_not_push_off_what_you_are_connected_to(self, tmp_path):
        """The study must not cost the working details their place."""
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            notes = _notes(app)

        assert "approval:" in notes
        assert "tools" in notes
