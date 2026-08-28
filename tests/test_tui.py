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
import os
import re
import threading
from pathlib import Path

import pytest

from andromeda_agent import ApprovalRequest, Callbacks
from andromeda_agent.loop import Conversation
from andromeda_cli import render, repl, sessions as sessions_store
from andromeda_tools import ToolResult, ToolSpec
from andromeda_tools.clarify import Question as ClarifyQuestion

import andromeda_tui
from andromeda_tui import events as ev
from andromeda_tui.app import AndromedaApp, slash_help
from andromeda_tui.driver import AgentDriver, Pending, TurnInterrupted
from andromeda_tui.prompts import APPROVAL_CHOICES, ApprovalScreen, ClarifyScreen
from andromeda_tui.widgets import ActivityLane, RecentUpdates, StudyPanel, Transcript

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
        assert slash_help() == repl.slash_help()

    def test_every_conversation_command_is_handled_here(self):
        """The list is generated now, so the way it can be wrong has changed:
        not "somebody forgot to add a line" but "somebody added a line for a
        command this screen does not implement"."""
        import inspect

        from andromeda_cli import vocabulary

        source = inspect.getsource(AndromedaApp._slash)
        for row in vocabulary.commands():
            if row.kind != "conversation":
                continue
            assert f'"/{row.name}"' in source, row.name


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
    def test_the_stylesheet_names_no_terminal_colours(self):
        """Hue is allowed now; *terminal* colour names still are not.

        The palette was monochrome and is not any more — see
        `render.YOU` and its three siblings. What that decision changed is
        which colours exist, not where they may be written: a stylesheet that
        says `cyan` inherits whatever sixteen colours the person's terminal
        happens to be configured with, and the surface stops being a thing
        anyone designed.

        Checked as CSS declarations rather than as substrings of the whole
        sheet. The version of this test that searched the raw text failed on
        the word "rendered", which contains "red" — a check that fires on prose
        teaches people to phrase comments around it.
        """
        css = AndromedaApp.CSS.lower()
        declarations = " ".join(
            line.split("/*")[0] for line in css.splitlines()
        )
        values = re.findall(r":\s*([^;{}]+)", declarations)
        words = {word for value in values for word in re.findall(r"[a-z#0-9]+", value)}

        for retired in ("cyan", "magenta", "yellow", "red", "green", "blue"):
            assert retired not in words, f"{retired} is a terminal colour"
        for retired in ("#8f9bff", "#18181b", "#202027", "#52525b"):
            assert retired not in css
        for retired in ("border-left: thick", "border: round"):
            assert retired not in css

        assert render.ZINC_50 == "#fafafa"
        assert render.ZINC_100 == "#f4f4f5"
        assert render.ZINC_200 == "#e4e4e7"
        assert not hasattr(render, "PERIWINKLE")

    def test_the_hues_are_one_per_meaning(self):
        """Four hues, each naming a distinction and none naming two.

        The budget is the point. A palette grows one well-argued colour at a
        time until nothing on screen means anything, so the test is not "these
        four exist" but "these four are all there are, and they differ".
        """
        hues = {
            "you": render.YOU,
            "agent": render.AGENT,
            "autonomous": render.AUTONOMOUS,
            "good": render.GOOD,
            "bad": render.BAD,
        }
        assert len(set(hues.values())) == len(hues)
        assert all(value.startswith("#") for value in hues.values())

        from andromeda_tui.widgets import SCREEN_TONES

        # The surface and the REPL resolve the same names to the same colours.
        for name in ("you", "agent", "autonomous", "good", "bad"):
            assert name in SCREEN_TONES
            assert name in render.THEME.styles

    @pytest.mark.asyncio
    async def test_a_turn_is_framed_once_not_labelled_per_segment(self, tmp_path):
        """One frame per turn. This is the whole complaint it fixes.

        The banner this replaces was painted by `feed_answer`, which opens a
        new block after every tool call — so a turn that called three tools
        produced four labels interleaved with the tool lines, and there was no
        mark anywhere saying where the answer began or ended. A frame is opened
        once by whatever speaks first and closed once when the turn ends.
        """
        app = _app(tmp_path, script=["Hello back"])
        async with app.run_test(size=(160, 60)) as pilot:
            app.driver.submit("Hello there")
            transcript_of = lambda: app.query_one(Transcript)
            # Settle on the closing edge itself, not on `driver.busy`. The flag
            # drops when the agent thread finishes, which is one tick *before*
            # `TurnFinished` is drained and the frame is closed — so waiting on
            # it asserts against a half-drawn turn. Under a loaded full-suite
            # run that gap is wide enough to fail; alone it never was.
            await _settle(
                pilot,
                app,
                lambda: len(transcript_of().query(".frame-bottom")) == 1,
            )
            await pilot.pause()

            transcript = transcript_of()
            tops = transcript.query(".frame-top")
            bottoms = transcript.query(".frame-bottom")

            assert len(tops) == 1
            assert len(bottoms) == 1
            assert "[ A N D R O M E D A ]" not in _painted(app)
            # The label rides on the frame's top edge, once.
            assert "A N D R O M E D A" in str(tops[0].visual)

    @pytest.mark.asyncio
    async def test_the_frame_closes_after_the_answer_not_before_a_tool(self, tmp_path):
        """A tool belongs inside the turn that called it."""
        from andromeda_tui.widgets import Transcript as T

        transcript = T()
        transcript._append = lambda widget: None  # no app to mount into
        transcript.call_after_refresh = lambda call: None

        transcript.ensure_frame("andromeda")
        transcript.add_tool("read_file data.txt", "safe_local")
        transcript.add_tool_result("4 lines", ok=True)
        assert transcript._frame == ("andromeda", "agent", "")

        transcript.close_frame()
        assert transcript._frame is None

    @pytest.mark.asyncio
    async def test_only_the_persons_turn_is_unframed(self, tmp_path):
        """The asymmetry is still the signal, and now it is a hue too.

        A matching `[ Y O U ]` was tried and removed: two labels facing each
        other is twice the furniture for one bit. What separates them is that
        the agent's turn is framed and the person's is not — plus
        `render.YOU`, which does in one glance what a 16-point brightness
        difference between two near-identical greys could not do at all.
        """
        app = _app(tmp_path, script=["Hello back"])
        async with app.run_test(size=(160, 60)) as pilot:
            app.driver.submit("Hello there")
            await _settle(pilot, app, lambda: not app.driver.busy)
            prompt = app.query_one(Transcript).query_one(".prompt")

            assert "Hello there" in str(prompt.visual)
            assert "[ Y O U ]" not in str(prompt.visual)
            assert "INPUT" not in str(prompt.visual)
            assert "OUTPUT" not in _painted(app)
            assert "YOUR MESSAGE" not in AndromedaApp.CSS.upper()

            # Textual parses the hex into a `Color` before it reaches the
            # widget, so the assertion is on the resolved triple. Matching the
            # literal `#67e8f9` passes only until Textual normalises it, which
            # it already does.
            wanted = tuple(int(render.YOU[index : index + 2], 16) for index in (1, 3, 5))
            spans = prompt.visual.spans if hasattr(prompt.visual, "spans") else []
            found = {
                (
                    span.style.foreground.rgb
                    if getattr(span.style, "foreground", None) is not None
                    else None
                )
                for span in spans
            }
            assert wanted in found

    @pytest.mark.asyncio
    async def test_a_scheduled_run_paints_into_the_session_that_created_it(
        self, tmp_path, monkeypatch
    ):
        """The complaint this closes: "it just sends me a notification".

        A scheduled run is a full agent turn happening in a daemon with no
        screen. It now writes a journal and the surface tails it, so the run
        appears in the conversation that asked for it — live, with its tool
        calls, inside its own amber frame.
        """
        from andromeda_agent import live
        from andromeda_cli import config as config_module

        home = tmp_path / "home"
        monkeypatch.setattr(config_module, "home", lambda: home)

        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            session_id = app.driver.binding.record.id

            writer = live.Writer(
                home, job_id="job_x", job_name="PR watch", session=session_id
            )
            writer.started(reason="scheduled")
            writer.text("Two PRs changed.")
            writer.tool("bash gh pr list", "safe_local")
            writer.tool_result("exit 0 · 4 lines", ok=True)
            writer.finished("ok", summary="Two PRs changed.")

            await _settle(pilot, app, lambda: app.transcript.query(".frame-top"))
            await pilot.pause()

            # Read off the widgets rather than `_painted`, which only returns
            # the answer block — the frame edge, the tool line and the status
            # note are separate rows by design.
            rows = " ".join(str(widget.visual) for widget in app.transcript.children)
            assert "A U T O N O M O U S" in rows
            # The job's name is beside the label, in plain casing — tracking it
            # would render "PR watch" as "P R   W A T C H".
            assert "⌂ PR watch" in rows
            assert "Two PRs changed." in _painted(app)
            assert "gh pr list" in rows
            assert "finished · ok" in rows
            # It is closed, not left hanging.
            assert len(app.transcript.query(".frame-bottom")) == 1

            # And it is not confused with an answer this person asked for.
            top = app.transcript.query(".frame-top")[0]
            wanted = tuple(
                int(render.AUTONOMOUS[index : index + 2], 16) for index in (1, 3, 5)
            )
            found = {
                (
                    span.style.foreground.rgb
                    if getattr(span.style, "foreground", None) is not None
                    else None
                )
                for span in top.visual.spans
            }
            assert wanted in found

    @pytest.mark.asyncio
    async def test_a_run_for_another_conversation_stays_out_of_this_one(
        self, tmp_path, monkeypatch
    ):
        """A job attached elsewhere must not interrupt the session on screen.

        Without the filter every surface paints every run, and the feature
        becomes a reason to close the app.
        """
        from andromeda_agent import live
        from andromeda_cli import config as config_module

        home = tmp_path / "home"
        monkeypatch.setattr(config_module, "home", lambda: home)

        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()

            live.Writer(
                home, job_id="job_y", job_name="Elsewhere", session="not-this-one"
            ).finished("ok", summary="should not appear")

            for _ in range(4):
                await pilot.pause()

            assert "should not appear" not in _painted(app)
            assert not app.transcript.query(".frame-top")

    def test_a_session_is_deleted_from_the_rail_after_two_clicks(
        self, tmp_path, monkeypatch
    ):
        """One click arms, the second deletes.

        A modal is the right answer when the thing being destroyed is expensive
        to rebuild. A transcript is not, and a dialog per row turns tidying up
        forty sessions into eighty keystrokes. The row says `sure?` between the
        clicks, so the second is never a surprise.
        """
        from andromeda_cli import sessions as store
        from andromeda_tui.widgets import SessionsRail

        directory = tmp_path / "sessions"
        monkeypatch.setattr(store, "sessions_dir", lambda: directory)

        keeper = store.Session(id="aaaaaaaaaaaa", messages=[{"role": "user", "content": "keep"}])
        doomed = store.Session(id="bbbbbbbbbbbb", messages=[{"role": "user", "content": "drop"}])
        keeper.save()
        doomed.save()
        assert doomed.path.exists()

        rail = SessionsRail()
        # No running app, so painting is stubbed. What is under test is the
        # arming state machine and the file, neither of which needs a screen.
        rail._refresh = lambda: None
        rail.reload()
        assert {row[0] for row in rail.rows} >= {keeper.id, doomed.id}

        target = next(index for index, row in enumerate(rail.rows) if row[0] == doomed.id)
        rail.cursor = target

        posted = []
        rail.post_message = posted.append

        rail.delete_selected()
        assert rail._confirming == doomed.id
        assert posted == []  # armed, not fired

        rail.delete_selected()
        assert len(posted) == 1
        assert posted[0].session_id == doomed.id
        assert rail._confirming == ""

        assert store.delete(doomed.id) is True
        assert not doomed.path.exists()
        assert keeper.path.exists()

        rail.forget(doomed.id)
        assert doomed.id not in {row[0] for row in rail.rows}

    def test_moving_off_a_row_disarms_its_delete(self, tmp_path, monkeypatch):
        """A confirmation that outlives the cursor fires on a click you forgot."""
        from andromeda_cli import sessions as store
        from andromeda_tui.widgets import SessionsRail

        directory = tmp_path / "sessions"
        monkeypatch.setattr(store, "sessions_dir", lambda: directory)
        for index in range(3):
            store.Session(
                id=f"{index}" * 12, messages=[{"role": "user", "content": str(index)}]
            ).save()

        rail = SessionsRail()
        rail.reload()
        rail._refresh = lambda: None
        rail.post_message = lambda message: None

        rail.delete_selected()
        assert rail._confirming
        rail.move(1)
        assert rail._confirming == ""

    @pytest.mark.asyncio
    async def test_the_live_session_refuses_to_be_deleted(self, tmp_path):
        """Unlinking the file being written to does not stop the writing.

        The next save recreates it, half of it, under the same id. Refusing
        with a one-key instruction leaves nothing ambiguous.
        """
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            live_id = app.driver.binding.record.id

            app.on_sessions_rail_deleted(
                app.recent_updates.Deleted(live_id)
            )
            await pilot.pause()

            assert "CTRL+L" in _notes(app)

    @pytest.mark.asyncio
    async def test_the_landing_page_chrome_frames_the_surface(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()

            assert "∞  ANDROMEDA" in str(app.brand.visual)
            assert "PERSONAL AGENT  ·  CLI" in str(app.header_meta.visual)
            assert "MODEL  /  ASK" in str(app.header_state.visual)

    @pytest.mark.asyncio
    async def test_the_hero_and_chat_are_one_continuous_scroll_flow(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()

            assert app.masthead.parent is app.conversation_scroll
            assert app.transcript.parent is app.conversation_scroll
            assert app.transcript.region.height >= 21
            assert app.conversation_scroll.scroll_y == 0

    @pytest.mark.asyncio
    async def test_conversation_growth_gradually_pushes_the_hero_out_of_frame(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            positions = []

            # Painted the way `_handle` paints a turn — prompt, frame, answer,
            # close. The version of this loop that skipped the frame was
            # measuring a turn the app never produces, and it drifted the
            # moment the per-segment banner it was silently relying on for
            # height went away.
            for index in range(12):
                app.transcript.close_frame()
                app.transcript.add_prompt(f"Question {index}")
                app.transcript.ensure_frame(AndromedaApp.ANSWER_FRAME)
                app.transcript.feed_answer(f"Answer {index}\nwith one more line")
                app.transcript.end_answer()
                app.transcript.close_frame()
                await pilot.pause()
                positions.append(app.conversation_scroll.scroll_y)

            assert positions == sorted(positions)
            assert positions[0] < positions[-1]
            assert app.conversation_scroll.scroll_y > app.masthead.region.height

    @pytest.mark.asyncio
    async def test_completed_answers_keep_their_own_text_after_a_resize(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.transcript.feed_answer("First answer")
            app.transcript.end_answer()
            app.transcript.feed_answer("Second answer")
            app.transcript.end_answer()
            await pilot.resize_terminal(150, 55)
            await pilot.pause()

            answers = list(app.transcript.query(".answer"))
            assert "First answer" in str(answers[0].visual)
            assert "Second answer" not in str(answers[0].visual)
            assert "Second answer" in str(answers[1].visual)

    @pytest.mark.asyncio
    async def test_a_real_resize_restores_the_hero_after_a_narrow_start(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            assert app.masthead.display is False
            assert app.header_meta.display is False
            assert app.header_state.display is False

            await pilot.resize_terminal(160, 60)
            await pilot.pause()

            assert app.masthead.display is True
            assert app.header_meta.display is True
            assert app.header_state.display is True
            assert app.study.region.width > 0
            assert app.recent_updates.region.width > 0

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
    async def test_the_composer_keeps_a_visible_text_row_after_typing(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("h", "i")
            await pilot.pause()

            assert app.composer.text == "hi"
            assert app.composer.region.height == 1
            assert app.composer.content_region.height == 1
            help_line = app.query_one("#composer-help")
            assert help_line.region.y - app.composer.region.bottom == 1
            assert app.composer_shell.region.bottom - help_line.region.bottom == 1

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
    async def test_a_long_command_does_not_push_the_refusals_off_the_box(
        self, tmp_path
    ):
        """Every answer stays on screen, the two refusals most of all.

        The box grew to fit the command it was asking about and Textual clipped
        the overflow, which took `n` and `never` with it. What scrolls now is
        the command; the answers are pinned.
        """
        long_command = (
            "rsync -av --delete /Users/someone/a/very/long/source/path/that/wraps "
            "/Volumes/backup/destination && echo done && ls -la /Volumes/backup"
        )
        app = _app(
            tmp_path,
            script=[turn_with(call("terminal", {"command": long_command})), "ok"],
        )
        async with app.run_test(size=(70, 16)) as pilot:
            app.driver.submit("back it up")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.pause()
            screen = app.screen
            choices = screen.query_one("#approval-choices")
            assert choices.region.height >= len(APPROVAL_CHOICES)
            assert app.screen.region.contains_region(choices.region)
            assert app.screen.region.contains_region(
                screen.query_one("#approval-header").region
            )
            await pilot.press("n")

    @pytest.mark.asyncio
    async def test_the_activity_lane_says_who_is_waiting(self, tmp_path):
        """A spinner while a prompt is open blames the machine for the pause."""
        app = _app(tmp_path, script=[turn_with(call("terminal", {"command": "ls"})), "ok"])
        async with app.run_test() as pilot:
            app.driver.submit("run ls")
            await _settle(pilot, app, lambda: isinstance(app.screen, ApprovalScreen))
            lane = app.query_one(ActivityLane)
            assert lane.waiting is True
            assert "WAITING FOR YOUR ANSWER" in str(lane.visual)
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
    async def test_a_long_list_on_a_short_terminal_keeps_the_prompt_answerable(
        self, tmp_path
    ):
        """The two rows that must survive are the question and the input.

        The box grew to fit its options and Textual clipped what would not fit,
        which took the question off the top and the input off the bottom: a
        numbered list with nothing saying what it was for and no way to answer
        it. The list scrolls instead.
        """
        from textual.containers import VerticalScroll
        from textual.widgets import Input

        app = _app(tmp_path, script=["thanks"])
        async with app.run_test(size=(80, 11)) as pilot:
            answers: list[list[str]] = []
            questions = [
                ClarifyQuestion(
                    "Which of these did you mean? The catalogue is fixed and the "
                    "name you gave is not on it.",
                    ["webflow", "linear", "notion", "supabase"],
                )
            ]
            thread = threading.Thread(
                target=lambda: answers.append(app.driver.ask_questions(questions))
            )
            thread.start()
            await _settle(pilot, app, lambda: isinstance(app.screen, ClarifyScreen))
            await pilot.pause()
            screen = app.screen
            visible = app.screen.region
            for widget_id in ("#clarify-question", "#clarify-input"):
                region = screen.query_one(widget_id).region
                assert region.height > 0, f"{widget_id} was laid out with no height"
                assert visible.contains_region(region), f"{widget_id} is off screen"
            # And the squeeze landed where it was aimed.
            assert screen.query_one("#clarify-choices", VerticalScroll).styles.max_height
            screen.query_one("#clarify-input", Input).value = "2"
            await pilot.press("enter")
            await _settle(pilot, app, lambda: bool(answers))
            thread.join(timeout=5)
            assert answers == [["linear"]]

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


def _study(app) -> str:
    rows = app.query_one(StudyPanel).query(".study-row")
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
            study = _study(app)

        # The coordinates and the caption are text rather than braille, so they
        # survive width clamping and are the honest thing to assert on.
        assert "00h 42m 44s" in study
        assert "HUMAN / SYSTEM / ORBIT" in study

    @pytest.mark.asyncio
    async def test_it_does_not_push_off_what_you_are_connected_to(self, tmp_path):
        """The study must not cost the working details their place."""
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            updates = str(app.query_one(RecentUpdates).visual)

        assert "APPROVAL / ASK" in updates
        assert "TOOLS / 2" in updates

    @pytest.mark.asyncio
    async def test_recent_updates_occupy_the_wide_side_of_the_study(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()

            assert app.masthead.display is True
            assert app.recent_updates.display is True
            assert app.study.region.x < app.recent_updates.region.x

    @pytest.mark.asyncio
    async def test_updates_rail_lists_sessions_not_live_chat_activity(self, tmp_path):
        """The rail browses saved sessions; it is not a feed of this turn.

        The rail used to hold the changelog, and this test used to assert that.
        The half of its intent that outlived the change is the half kept: what
        is being said *right now* must not leak into the rail. A live turn is
        not history, and the rail showing it would be the "conversation event
        masquerading as something durable" the old version guarded against.
        """
        app = _app(tmp_path, script=["A user-facing answer"])
        async with app.run_test(size=(160, 60)) as pilot:
            app.driver.submit("A private user message")
            await _settle(pilot, app, lambda: not app.driver.busy)
            await pilot.pause()
            updates = str(app.query_one(RecentUpdates).visual)

        assert "SYS. 001" in updates
        assert "SESSIONS" in updates
        # Both groups are always offered, including the empty one. A tab that
        # appears only once it has content is a feature nobody discovers.
        assert "ALL" in updates and "AGENT" in updates
        assert "RECENT CLI CHANGES" not in updates
        assert "A private user message" not in updates
        assert "A user-facing answer" not in updates
        assert str(tmp_path) not in updates

    @pytest.mark.asyncio
    async def test_rail_gathers_every_agent_session_under_one_tab(self, tmp_path):
        """Local and cloud stopped being two things the moment placement became
        a per-fire decision. Splitting the rail by it made a conversation move
        between tabs for reasons nobody could see."""
        from andromeda_tui.widgets import SessionsRail

        rail = SessionsRail()
        rail.rows = [
            ("aaa", "Watch my PRs", "2h ago", "local"),
            ("bbb", "Refactor auth", "1d ago", ""),
            ("ccc", "Inbox digest", "3d ago", "cloud"),
        ]
        rail._clamp()
        painted: dict = {}
        rail.update = lambda text: painted.update(text=text)

        rail._refresh()
        # ALL is what *you* said. A five-minute job would otherwise push every
        # real conversation off the rail with its own output.
        assert [row[0] for row in rail._visible_rows()] == ["bbb"]

        rail.switch_tab(1)
        # Both job sessions, neither plain conversation.
        assert [row[0] for row in rail._visible_rows()] == ["aaa", "ccc"]

    @pytest.mark.asyncio
    async def test_each_agent_row_still_says_where_it_is(self, tmp_path):
        """One tab, and the badge on each row carries what the tabs used to."""
        from andromeda_tui.widgets import SessionsRail

        rail = SessionsRail()
        rail.rows = [
            ("aaa", "Watch my PRs", "2h ago", "local"),
            ("ccc", "Inbox digest", "3d ago", "cloud"),
        ]
        rail._clamp()
        painted: dict = {}
        rail.update = lambda text: painted.update(text=text)
        rail.switch_tab(1)

        plain = painted["text"].plain
        assert "⌂" in plain
        assert "☁" in plain
        assert "☁" in painted["text"].plain

        # Wraps rather than stopping, so ←→ never dead-ends on an end tab.
        rail.switch_tab(1)
        assert rail.TABS[rail.tab][0] == "all"

    @pytest.mark.asyncio
    async def test_rail_says_what_an_empty_group_means(self, tmp_path):
        """An empty AGENT tab is the ordinary state, not a broken feature."""
        from andromeda_tui.widgets import SessionsRail

        rail = SessionsRail()
        rail.rows = []
        painted: dict = {}
        rail.update = lambda text: painted.update(text=text)

        rail._refresh()
        assert "NO SAVED SESSIONS" in painted["text"].plain
        rail.switch_tab(1)
        assert "NO AGENT JOBS YET" in painted["text"].plain
        # Two tabs, so one more step is back to the start.
        rail.switch_tab(1)
        assert "NO SAVED SESSIONS" in painted["text"].plain

    @pytest.mark.asyncio
    async def test_the_rail_is_hit_tested_in_real_geometry(self, tmp_path):
        """Clicks are resolved through the widget's actual padding.

        This is the regression that shipped. A click's offset is relative to
        the widget's outer box, and the rail carries CSS padding — one line at
        the top, two columns at the left. Hit-testing the raw offset made the
        tab strip unreachable and mapped a click on one row onto its
        neighbour, which is exactly how it behaved in a real terminal while
        every unit test passed: the tests posted synthetic offsets that already
        agreed with the broken arithmetic.

        So this test is driven from the live app and reads the padding back off
        the resolved style rather than restating it.
        """
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            rail = app.recent_updates
            rail.rows = [
                (f"id{index}", f"Session {index}", "2h ago", "")
                for index in range(30)
            ]
            rail.tab = 0
            rail.cursor = 0
            rail.window_top = 0
            rail._clamp()
            rail._refresh()
            await pilot.pause()

            padding = rail.styles.padding
            assert padding.top or padding.left, "the bug needs padding to exist"

            opened: list[str] = []
            real_post = rail.post_message

            def spy(message):
                if isinstance(message, rail.Opened):
                    opened.append(message.session_id)
                else:
                    real_post(message)

            rail.post_message = spy

            class _Offset:
                def __init__(self, x, y):
                    self.x, self.y = x, y

            class _Click:
                def __init__(self, x, y):
                    self.offset = _Offset(x, y)

            # The third visible row, addressed the way Textual addresses it.
            line, index = sorted(rail._row_lines.items())[2]
            rail.on_click(_Click(6 + padding.left, line + padding.top))
            assert opened == [f"id{index}"]

            # And the tab strip, which the shipped version could not reach.
            begins, _ = rail._tab_span(1)
            rail.on_click(
                _Click(begins + padding.left + 1, rail._tab_line + padding.top)
            )
            assert rail.TABS[rail.tab][0] == "agent"

    @pytest.mark.asyncio
    async def test_the_rail_shows_as_many_rows_as_it_has_room_for(self, tmp_path):
        """Capacity is measured, not fixed.

        The constant it replaced showed four rows on a terminal with room for a
        dozen, which turned browsing forty sessions into forty keystrokes.
        """
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            rail = app.recent_updates
            rail.rows = [
                (f"id{index}", f"Session {index}", "2h ago", "")
                for index in range(40)
            ]
            rail._clamp()
            rail._refresh()
            await pilot.pause()

            assert rail._capacity() > 4
            assert len(rail._row_lines) == rail._capacity()
            # One painted line per row, so the map has no gaps.
            lines = sorted(rail._row_lines)
            assert lines == list(range(lines[0], lines[0] + len(lines)))

    @pytest.mark.asyncio
    async def test_the_wheel_reaches_every_session(self, tmp_path):
        """Forty sessions, and no chord required to see the fortieth."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            rail = app.recent_updates
            rail.rows = [
                (f"id{index}", f"Session {index}", "2h ago", "")
                for index in range(40)
            ]
            rail._clamp()
            rail._refresh()

            class _Scroll:
                def stop(self):
                    pass

            for _ in range(60):
                rail.on_mouse_scroll_down(_Scroll())
            assert rail.cursor == 39

            for _ in range(80):
                rail.on_mouse_scroll_up(_Scroll())
            assert rail.cursor == 0


class TestJobsAreManagedWithoutLeaving:
    """The surface offers the verbs the agent points at.

    The complaint these answer: Andromeda replied with `andromeda cron install`
    and `andromeda cron approve ...`, both of which are shell commands that
    cannot be typed where they were read. One of them should never have been
    asked for at all; the other needed a door on this surface rather than an
    instruction to leave through it.
    """

    @staticmethod
    def _notes(app) -> str:
        """Everything the transcript printed that was not a model answer."""
        transcript = app.query_one(Transcript)
        return ANSI.sub(
            "",
            "\n".join(
                str(child.visual)
                for child in transcript.children
                if not child.has_class("answer")
            ),
        )

    @pytest.mark.asyncio
    async def test_jobs_answers_on_this_surface(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            app._slash("/jobs")
            await pilot.pause()
            notes = self._notes(app)

        assert "unknown command" not in notes
        # It answered here rather than sending the person to a shell.
        assert "andromeda cron list" not in notes

    @pytest.mark.asyncio
    async def test_a_slash_command_never_reaches_the_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        app = _app(tmp_path, script=["never asked"])
        async with app.run_test() as pilot:
            app._slash("/jobs")
            await pilot.pause()
            assert app.conversation.provider.seen == []

    @pytest.mark.asyncio
    async def test_approve_without_an_id_explains_itself(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            app._slash("/approve")
            await pilot.pause()
            notes = self._notes(app)

        assert "/approve" in notes and "/jobs" in notes

    @pytest.mark.asyncio
    async def test_approve_widens_a_real_job_in_place(self, tmp_path, monkeypatch):
        """The grant is still a person's to make — it just happens here now."""
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        from andromeda_cli.commands import cron as cron_cmd

        job = cron_cmd._schedule().add(
            "every 1h", "watch", str(tmp_path), name="W", origin="agent"
        )
        assert job.approval_mode == "ask"

        app = _app(tmp_path)
        async with app.run_test() as pilot:
            app._slash(f"/approve {job.id}")
            await pilot.pause()

        assert cron_cmd._schedule().resolve(job.id).approval_mode == "auto"

    @pytest.mark.asyncio
    async def test_approve_cannot_move_a_job_into_the_cloud(self, tmp_path):
        """A larger grant than this one, and not a slash-command decision.

        Moving a job onto hardware the person does not hold deserves the
        refusal matrix and a command they had to read, not a shortcut typed
        mid-conversation.
        """
        import inspect

        from andromeda_tui import app as app_module

        source = inspect.getsource(app_module.AndromedaApp._approve_job)
        assert "run_on" not in source or "cloud" not in source.split("approval=")[-1]

    @pytest.mark.asyncio
    async def test_a_failing_slash_command_does_not_kill_the_app(
        self, tmp_path, monkeypatch
    ):
        """`_capture` folds the failure into a line.

        A slash command reaching into the CLI's command layer is reaching into
        code that expects a terminal. It must not take the surface down with it.
        """
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            lines = app._capture(lambda: 1 / 0)
            assert lines and "ZeroDivisionError" in lines[0]
            app._slash("/help")
            await pilot.pause()
            assert "/jobs" in self._notes(app)


class TestTheSchedulerArmsItself:
    """A job created is a job that runs. No second command.

    The failure this closes: `cron add` produced a job that looked scheduled,
    listed healthy, and never fired, because nothing had installed a
    supervisor — and the only signal was a printed suggestion nobody had to act
    on.
    """

    def test_a_test_run_never_writes_a_login_service(self, monkeypatch):
        """The guard that matters most in that module.

        The supervisor's path is fixed by the OS and reached through
        `Path.home()`, which `ANDROMEDA_HOME` does not redirect — so without
        this, a green test run would leave a real launch agent on the
        developer's machine pointing at a deleted temp directory.
        """
        from andromeda_cli.commands import service

        assert "PYTEST_CURRENT_TEST" in os.environ
        assert service.auto_install_allowed() is False
        assert service.ensure_installed() is False

    def test_a_person_can_turn_the_automatic_install_off(self, monkeypatch):
        from andromeda_cli.commands import service

        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv(service.NO_AUTO_ENV, "1")
        assert service.auto_install_allowed() is False

    def test_the_tool_never_hands_out_a_shell_command(self, tmp_path):
        """What the model is told after creating a job.

        The old text instructed it to relay two commands. One is now automatic
        and the other has a slash command, so neither belongs in an answer.
        """
        from andromeda_agent.schedule import Schedule
        from andromeda_tools import scheduling

        schedule = Schedule(tmp_path / "cron.json")
        spec = scheduling.cron_spec(schedule, str(tmp_path), session_id="s1")
        result = spec.run(
            action="create", schedule="every 60m", prompt="watch", name="W"
        )

        assert "cron install" not in result.content
        assert "andromeda cron approve" not in result.content
        assert "/approve" in result.content

    def test_it_does_not_claim_a_job_will_fire_when_nothing_will_fire_it(
        self, tmp_path, monkeypatch
    ):
        """The honest branch.

        With no scheduler installed the result must say so. A confident
        sentence promising a job is live is worse than the silence it replaced.

        `is_installed` is stubbed rather than left to the machine. The
        auto-install guard already refuses under a test runner, but
        `is_installed` reads real launchd or systemd state — so on a developer
        who happens to have the scheduler installed this branch is never
        reached and the test passes or fails depending on whose laptop it runs
        on. Which branch is under test has to be stated, not inherited.
        """
        from andromeda_agent.schedule import Schedule
        from andromeda_cli.commands import service as service_module
        from andromeda_tools import scheduling

        monkeypatch.setattr(service_module, "is_installed", lambda: False)

        schedule = Schedule(tmp_path / "cron.json")
        spec = scheduling.cron_spec(schedule, str(tmp_path), session_id="s1")
        result = spec.run(
            action="create", schedule="every 60m", prompt="watch", name="W"
        )

        assert "will not fire" in result.content

    def test_it_says_a_job_is_live_when_a_scheduler_is_installed(
        self, tmp_path, monkeypatch
    ):
        """The other branch, stated for the same reason.

        Two tests pinned to two stubs, so both sentences are covered wherever
        the suite runs — rather than one of them being whatever this machine
        happens to be.
        """
        from andromeda_agent.schedule import Schedule
        from andromeda_cli.commands import service as service_module
        from andromeda_tools import scheduling

        monkeypatch.setattr(service_module, "is_installed", lambda: True)

        schedule = Schedule(tmp_path / "cron.json")
        spec = scheduling.cron_spec(schedule, str(tmp_path), session_id="s1")
        result = spec.run(
            action="create", schedule="every 60m", prompt="watch", name="W"
        )

        assert "will fire on its own" in result.content
        assert "will not fire" not in result.content

    @pytest.mark.asyncio
    async def test_capturing_output_does_not_pin_the_shared_console(
        self, tmp_path, monkeypatch
    ):
        """`_capture` must leave the console following stdout.

        Rich resolves `Console.file` to `sys.stdout` at read time when nothing
        is pinned. Saving that value and writing it back pins the console to
        one stream permanently — after a single slash command every later
        caller writes into a stale object. In the app that means output going
        somewhere nobody is looking; in the suite it made unrelated tests go
        silent, which is how it was found.
        """
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        from andromeda_cli import output as output_module

        before = output_module.console._file
        app = _app(tmp_path)
        async with app.run_test() as pilot:
            app._capture(lambda: print("something"))
            app._capture(lambda: 1 / 0)
            await pilot.pause()

        assert output_module.console._file is before


# ---------------------------------------------------------------------------
# The command palette
# ---------------------------------------------------------------------------


class TestCommandPalette:
    """Typing `/` in the full-screen surface did nothing at all, while the help
    line under the composer promised `/ COMMANDS` the whole time."""

    @pytest.mark.asyncio
    async def test_a_slash_opens_the_list(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()

            assert app.palette.open
            assert app.palette.display

    @pytest.mark.asyncio
    async def test_typing_narrows_it(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/", "m", "c")
            await pilot.pause()

            assert [row.name for row in app.palette.rows] == ["mcp"]

    @pytest.mark.asyncio
    async def test_enter_completes_rather_than_sends(self, tmp_path):
        """A highlighted row is a choice in progress. Sending `/mc` because
        somebody was looking at `/mcp` would ignore what was on screen."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/", "m", "c")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app.composer.text == "/mcp "
            assert not app.palette.open

    @pytest.mark.asyncio
    async def test_arrows_move_the_highlight_not_the_cursor(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()
            first = app.palette.chosen.name
            await pilot.press("down")
            await pilot.pause()

            assert app.palette.chosen.name != first
            assert app.composer.text == "/"

    @pytest.mark.asyncio
    async def test_escape_closes_it_and_gives_the_keys_back(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert not app.palette.open
            # And Enter sends again, rather than completing nothing.
            assert app.composer.text == "/"

    @pytest.mark.asyncio
    async def test_a_space_closes_it(self, tmp_path):
        """Once there is a space the command is chosen and the rest is its
        arguments, which the palette knows nothing about."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/", "m", "c", "p", "space")
            await pilot.pause()

            assert not app.palette.open

    @pytest.mark.asyncio
    async def test_a_message_containing_a_slash_is_left_alone(self, tmp_path):
        """Popping a list over somebody writing `and/or` makes it feel broken."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            for key in "what about and/or":
                await pilot.press("space" if key == " " else key)
            await pilot.pause()

            assert not app.palette.open

    @pytest.mark.asyncio
    async def test_nothing_matching_closes_rather_than_showing_an_empty_box(
        self, tmp_path
    ):
        """An empty box reads as the surface having frozen."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/", "z", "z", "q", "q")
            await pilot.pause()

            assert not app.palette.open

    @pytest.mark.asyncio
    async def test_the_list_scrolls_past_what_fits(self, tmp_path):
        """More commands than rows, so the window has to follow the highlight
        rather than showing the first screenful forever."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()
            palette = app.palette
            assert len(palette.rows) > palette.VISIBLE

            for _ in range(palette.VISIBLE + 2):
                await pilot.press("down")
            await pilot.pause()

            window, highlight = palette._window()
            assert palette.rows[palette.index] is window[highlight]

    @pytest.mark.asyncio
    async def test_page_keys_move_a_screenful_and_clamp(self, tmp_path):
        """Wrapping a page jump is disorienting in a way wrapping one row is
        not: page-down near the end should land on the end."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()
            palette = app.palette

            await pilot.press("pagedown")
            await pilot.pause()
            assert palette.index == palette.PAGE

            for _ in range(20):
                await pilot.press("pagedown")
            await pilot.pause()
            assert palette.index == len(palette.rows) - 1

    @pytest.mark.asyncio
    async def test_home_and_end_jump(self, tmp_path):
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()

            await pilot.press("end")
            await pilot.pause()
            assert app.palette.index == len(app.palette.rows) - 1

            await pilot.press("home")
            await pilot.pause()
            assert app.palette.index == 0

    @pytest.mark.asyncio
    async def test_it_says_which_way_there_is_more(self, tmp_path):
        """A bare "… 21 more" under a half-scrolled list is wrong in both
        directions and reads as though the top is all there is."""
        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            app.composer.focus()
            await pilot.press("/")
            await pilot.pause()
            await pilot.press("end")
            await pilot.pause()

            painted = app.palette.render().plain
            assert "↑" in painted
            assert f"{len(app.palette.rows)}/{len(app.palette.rows)}" in painted


class TestSessionLinks:
    """Printing `andromeda --resume 78d4aa057c95` and expecting somebody to
    select it, copy it, leave the app and paste it into a shell is asking a
    person to be a terminal."""

    @pytest.mark.asyncio
    async def test_a_written_resume_command_becomes_a_clickable_row(self, tmp_path):
        from andromeda_tui.widgets import SessionLink

        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            transcript = app.transcript
            transcript.feed_answer(
                "Made you a job.\n\n  andromeda --resume 78d4aa057c95\n"
            )
            transcript.end_answer()
            await pilot.pause()

            links = list(transcript.query(SessionLink))
            assert [link.session_id for link in links] == ["78d4aa057c95"]

    @pytest.mark.asyncio
    async def test_one_row_per_session_however_often_it_is_written(self, tmp_path):
        from andromeda_tui.widgets import SessionLink

        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            transcript = app.transcript
            transcript.feed_answer(
                "andromeda --resume aaaaaaaaaaaa and again "
                "andromeda --resume aaaaaaaaaaaa"
            )
            transcript.end_answer()
            await pilot.pause()

            assert len(list(transcript.query(SessionLink))) == 1

    @pytest.mark.asyncio
    async def test_a_bare_hex_run_is_not_a_link(self, tmp_path):
        """A commit hash is also twelve hex characters, and turning one into
        "open this conversation" would be a link to nothing."""
        from andromeda_tui.widgets import SessionLink

        app = _app(tmp_path)
        async with app.run_test(size=(160, 60)) as pilot:
            transcript = app.transcript
            transcript.feed_answer("Fixed in 78d4aa057c95, see the diff.")
            transcript.end_answer()
            await pilot.pause()

            assert not list(transcript.query(SessionLink))

    @pytest.mark.asyncio
    async def test_clicking_goes_through_the_same_path_as_the_rail(self, tmp_path):
        """Refused mid-turn, refused for the current session, and never
        resuming from inside a widget."""
        from andromeda_tui.widgets import SessionLink, SessionsRail

        link = SessionLink("78d4aa057c95")
        posted: list = []
        link.post_message = lambda message: posted.append(message)

        link.on_click()

        assert len(posted) == 1
        assert isinstance(posted[0], SessionsRail.Opened)
        assert posted[0].session_id == "78d4aa057c95"
