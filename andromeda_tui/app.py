"""The full-screen surface.

## Why this is Python in-process and not a JS TUI over an IPC gateway

The common shape for a rich terminal UI is a React/Ink tree in TypeScript
talking newline-delimited JSON-RPC to the agent process. That split earns its
keep when the same tree also renders a desktop app and an embedded web chat:
three clients justify a protocol.

This build has one client. Reproducing the split would mean:

- porting `andromeda_cli/render.py` — the markdown streaming, the eighth-block
  chart renderer, the held-back unclosed fence, the restrained palette — into
  TypeScript, which is how a product ends up with two visual languages that
  drift, each rendering its own subset of markdown;
- putting a socket in the middle of the approval gate. The gate is blocking on
  purpose, so over IPC it becomes a request with a deadline, and a deadline on
  a consent prompt is a policy decision about consent. In-process there is
  nothing to decide: the thread waits;
- adding Node to an install path that is `curl | bash` → a uv venv → a symlink,
  which would add a second toolchain, a build step and a bundled `entry.js` to
  the one part of the product where conservatism pays most.

So: Textual, in-process, sharing the session code directly. It also removes a
whole bug class rather than managing it — `render.paint()` means the two
surfaces cannot drift, because they are the same code.

**The seam is kept anyway, and it is cheap.** `events.py` is a serialisable
event vocabulary and `driver.py` answers blocking questions by request id —
which is the shape a socket forces, adopted now while it costs nothing. If a
second client ever appears, a gateway writes `event.to_json()` down a pipe and
routes answers back by id; nothing in this file changes. What is *not* being
paid for today is a process boundary, a protocol version, a reconnect policy
and a second language.

## Threading

One rule. The agent runs on a worker thread and never touches a widget; it
appends to `AgentDriver`'s queue. This app drains that queue on a timer and
touches widgets only from the event loop. Everything crossing the boundary is
an event going down or an answer coming up by id.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from andromeda_agent import live
from andromeda_agent.models import THINKING_LEVELS, supports_reasoning
from andromeda_cli import config as config_module
from andromeda_cli import checkpoints as checkpoint_module
from andromeda_cli import art, render, vocabulary
from andromeda_cli.session import set_asker, set_lane_announcer

from . import events as ev
from .driver import AgentDriver
from .prompts import ApprovalScreen, ClarifyScreen
from .widgets import (
    ActivityLane,
    Composer,
    RecentUpdates,
    StatusBar,
    StudyPanel,
    Transcript,
    CommandPalette,
    screen_style,
)

# Both surfaces read one registry now. They could not be kept in step by hand:
# the list said twenty commands, the product had fifty, and a test that asserts
# two hardcoded strings match proves only that they are equally out of date.
def slash_help() -> str:
    return vocabulary.help_text()


# `/` leads, because it is the one that makes the other fifty commands
# findable; the key bindings are things you learn once and keep.
KEY_HINT = "/ COMMANDS  ·  CTRL+C INTERRUPT  ·  CTRL+G EDITOR  ·  CTRL+L NEW  ·  CTRL+D QUIT"


class AndromedaApp(App):
    """One conversation on one screen."""

    #: The label on the frame around an answer this person asked for. A
    #: constant because it is compared as well as painted — `ensure_frame` is
    #: idempotent on `(label, tone)`, so a caller that spells it differently
    #: opens a second frame instead of continuing the first.
    ANSWER_FRAME = "andromeda"

    CSS = """
    Screen { background: #000000; color: #fafafa; }

    #chrome {
        height: 3; margin: 0 2; padding: 1 1 0 1;
        border-bottom: solid #e4e4e7 24%; background: #000000;
    }
    #brand { width: 1fr; color: #fafafa; text-style: bold; }
    #header-meta {
        width: auto; color: #e4e4e7; text-style: dim;
        text-align: center;
    }
    #header-state {
        width: 1fr; color: #e4e4e7; text-style: dim;
        text-align: right;
    }

    #conversation-scroll {
        height: 1fr; background: #000000;
        scrollbar-size-vertical: 1; overflow-x: hidden; overflow-y: auto;
        scrollbar-color: #e4e4e7 30%;
        scrollbar-color-hover: #e4e4e7 45%;
        scrollbar-color-active: #e4e4e7 60%;
        scrollbar-background: #000000;
        scrollbar-background-hover: #000000;
        scrollbar-background-active: #000000;
        scrollbar-corner-color: #000000;
    }
    #masthead { height: 27; margin: 1 3 0 3; }
    #study {
        width: 2fr; height: 1fr; padding: 0;
        scrollbar-size-vertical: 0; overflow-y: hidden;
    }
    #study .study-row { height: auto; }
    #recent-updates {
        width: 1fr; height: 1fr; margin: 2 0 1 3; padding: 1 0 0 2;
        background: #000000; color: #f4f4f5;
    }

    #transcript {
        height: auto; min-height: 21; margin: 0 3; padding: 1 1 0 1;
        border-top: solid #e4e4e7 24%;
        background: #000000;
    }
    #transcript .row { margin-bottom: 1; }
    #transcript .prompt {
        height: auto; margin: 0 0 1 0; padding: 0;
        border: none; background: #000000;
    }
    /* The answer is indented by padding rather than by prefixing its lines:
       it is rendered markdown, and a string indent would push the fences and
       table rules out of alignment with the width they were laid out for. */
    #transcript .answer {
        height: auto; margin: 0 0 1 0; padding: 0 1 0 2;
        border: none; background: #000000;
    }
    #transcript .error { height: auto; margin: 0 0 1 0; padding: 0; }
    #transcript .tool, #transcript .tool-result, #transcript .note {
        margin-bottom: 0;
    }
    /* The frame edges hug what they contain: no bottom margin under the top
       rule, no top margin above the bottom one. A blank line between an edge
       and the first row makes the frame read as two unrelated rules. */
    #transcript .frame-top { height: 1; margin: 0 0 1 0; padding: 0; }
    #transcript .frame-bottom { height: 1; margin: 0 0 1 0; padding: 0; }

    #activity {
        height: auto; margin: 0 3; padding: 0 1; display: none;
        background: #000000;
    }
    #composer-shell {
        height: auto; max-height: 30; margin: 0 3; padding: 1;
        border-top: solid #e4e4e7 24%; background: #000000;
    }
    #composer-help {
        height: 1; margin-top: 1;
        color: #e4e4e7; text-style: dim;
    }
    #command-palette {
        height: auto; max-height: 14; margin-bottom: 1; padding: 0 1;
        background: #000000; border-left: solid #a78bfa;
        display: none;
    }
    #composer {
        height: auto; max-height: 10; border: none; padding: 0;
        background: #000000; color: #fafafa; scrollbar-size-vertical: 1;
    }
    #composer:focus { border: none; background: #000000; }
    #status {
        height: 2; padding: 0 2; background: #000000; color: #e4e4e7;
        border-top: solid #e4e4e7 24%;
    }

    /* The prompts. A dimmed backdrop is not decoration here: it is the visible
       statement that the thing underneath is stopped and waiting. */
    ApprovalScreen, ClarifyScreen { align: center middle; background: #000000 70%; }
    /* `max-height` is what keeps the prompt answerable. Without it a box tall
       enough to outgrow the terminal is clipped rather than fitted, and what
       gets clipped is the top — the question — and then the bottom, which is
       the input. Bounded, the scrolling region inside takes the squeeze. */
    #approval-box, #clarify-box {
        width: 80%; max-width: 100; height: auto; max-height: 100%; padding: 1 2;
        background: #000000; border: solid #e4e4e7 36%;
    }
    #approval-summary { padding: 1 0; }
    #approval-choices { padding-top: 1; }
    #approval-detail, #clarify-choices {
        height: auto; background: #000000;
        scrollbar-size-vertical: 1; scrollbar-color: #e4e4e7 30%;
        scrollbar-background: #000000; scrollbar-corner-color: #000000;
    }
    #clarify-choices { padding-bottom: 1; }
    #clarify-input {
        border: none; border-top: solid #e4e4e7 24%;
        background: #000000; color: #fafafa;
    }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit_app", "quit", show=False),
        Binding("ctrl+c", "interrupt", "interrupt", show=False),
        Binding("ctrl+l", "new_conversation", "new", show=False),
        Binding("ctrl+g", "edit_in_editor", "editor", show=False),
        Binding("pageup", "scroll_transcript(-1)", "scroll up", show=False),
        Binding("pagedown", "scroll_transcript(1)", "scroll down", show=False),
        # The sessions rail. Chorded, because the composer owns the bare arrow
        # keys and taking them would break typing to save a keystroke here.
        # The click path in `SessionsRail.on_click` is the one that needs no
        # teaching; these exist for people who do not reach for the mouse.
        Binding("ctrl+left", "rail_tab(-1)", "prev group", show=False),
        Binding("ctrl+right", "rail_tab(1)", "next group", show=False),
        Binding("ctrl+up", "rail_move(-1)", "rail up", show=False),
        Binding("ctrl+down", "rail_move(1)", "rail down", show=False),
        # Delete the highlighted session. Chorded and confirmed, like every
        # other rail key — and the click on the row's ✕ is the path that needs
        # no teaching.
        Binding("ctrl+backspace", "rail_delete", "delete session", show=False),
    ]

    def __init__(self, config: dict[str, Any], conversation, record, resumed=None) -> None:
        super().__init__()
        self.config = config
        self.conversation = conversation
        self.resumed = resumed
        self.checkpoints = checkpoint_module.CheckpointStack.from_json(
            resumed.checkpoints if resumed is not None else None
        )
        self.driver = AgentDriver(
            conversation, record, checkpoints=self.checkpoints
        )
        self.history: list[str] = []
        self._history_at = 0
        # Typed while the agent was working. Drained one at a time when a turn
        # ends — the alternative is dropping what someone typed, and a terminal
        # that discards keystrokes is a terminal people stop trusting.
        self.queued: list[str] = []
        # The dimmed placeholder rows standing in for `queued`, one per entry
        # and in the same order, so each is removed as its message is sent.
        self._queued_rows: list = []
        self._open_question: str = ""
        self._answering = False
        # A multi-line paste, held whole until it is sent. The composer is a
        # single-line `Input`, which truncates pasted text at the first
        # newline — so a pasted instruction arrives as its first line and the
        # rest is simply gone. Staging it keeps every line and keeps the
        # composer single-line, which is what makes Enter mean "send".
        # When this session began, and when the current line's first character
        # arrived. Both feed the injected-input guard; see `_looks_injected`.
        self._started_at = time.monotonic()
        self._typed_at: float | None = None
        self._last_typed_at: float | None = None
        self._exit_reason = ""

    # ---- layout -----------------------------------------------------------

    # Set by `_paint_live`, cleared by the tick that paints it. A class
    # attribute so a tick that lands before `on_mount` finishes reads False
    # rather than raising.
    _live_text_pending = False

    def compose(self) -> ComposeResult:
        # Held as attributes rather than looked up with `query_one`. `App.query_one`
        # searches the *current* screen, so every lookup here would raise
        # `NoMatches` for the whole time an approval prompt is open — and the
        # thing doing the looking up is the timer that drains the event queue.
        # That is a crash in exactly the state the surface most needs to keep
        # working. Caught by a test, not by reading.
        self.brand = Static("∞  ANDROMEDA", id="brand")
        self.header_meta = Static("PERSONAL AGENT  ·  CLI", id="header-meta")
        self.header_state = Static("", id="header-state")
        self.chrome = Horizontal(
            self.brand, self.header_meta, self.header_state, id="chrome"
        )
        self.study = StudyPanel(id="study")
        self.recent_updates = RecentUpdates(id="recent-updates")
        self.masthead = Horizontal(self.study, self.recent_updates, id="masthead")
        self.transcript = Transcript(id="transcript")
        self.conversation_scroll = VerticalScroll(
            self.masthead, self.transcript, id="conversation-scroll"
        )
        self.activity = ActivityLane(id="activity")
        self.composer = Composer(placeholder="Ask Andromeda anything…", id="composer")
        self.palette = CommandPalette(id="command-palette")
        # Above the field, not below it: the composer sits at the bottom of the
        # screen and a list under it would open off-screen or shove the whole
        # conversation up every time somebody typed a slash.
        self.composer_shell = Vertical(
            self.palette,
            self.composer,
            Static("ENTER TO SEND  ·  SHIFT+ENTER NEW LINE  ·  / COMMANDS", id="composer-help"),
            id="composer-shell",
        )
        self.status = StatusBar(id="status")
        yield self.chrome
        yield self.conversation_scroll
        yield self.activity
        yield self.composer_shell
        yield self.status

    async def on_mount(self) -> None:
        # The composer takes the navigation keys only while the list is open,
        # so it needs to be able to ask. Wired here rather than in `compose`,
        # because both have to exist first.
        self.composer.palette = self.palette
        transcript = self.transcript
        provider = self.conversation.provider
        self._sync_masthead_layout()
        # A real terminal can mount before its first measured layout. In that
        # state `self.size` is 0×0; treating it as a genuinely narrow terminal
        # hides the whole hero and the header metadata permanently. Re-run
        # after Textual has painted once and knows the actual cell dimensions.
        self.call_after_refresh(self._sync_masthead_layout)
        self.header_state.update(
            f"{provider.model.rsplit('/', 1)[-1].upper()}  /  {self.conversation.policy.mode.upper()}"
        )

        self.recent_updates.configure(
            provider=provider.label,
            model=provider.model,
            workspace=str(self.conversation.workspace.root),
            tools=len(self.conversation.available),
            approval=self.conversation.policy.mode,
            thinking=provider.thinking,
        )
        # The study opens this surface as well as the REPL's.
        #
        # It was added to `output.banner()` first, which only the REPL calls —
        # so anyone whose `interface` is `tui`, which is a configured default
        # and not an unusual choice, never saw it at all. One surface getting
        # the product's face and the other not is exactly the drift the shared
        # renderer exists to prevent.
        #
        # Textual owns the screen, so its widgets perform the same one-pass
        # reveal as the REPL instead of starting a competing Rich `Live`
        # region. Every row is mounted up front: the layout stays still while
        # the scan reveals the drawing, then remains fully drawn.
        await self._reveal_study(self.study)

        if self.resumed is not None:
            transcript.add_note(
                f"SESSION / RESUMED {self.record.id} · {self.record.turns} TURNS"
            )
        for line in self._state_health():
            transcript.add_note(line, tone="yellow")
        pending = self._pending_suggestions()
        if pending:
            transcript.add_note(
                f"{pending} automation(s) suggested · andromeda cron suggest",
                tone="yellow",
            )
        status = self.status
        status.model = provider.model
        status.mode = f"approval:{self.conversation.policy.mode}"
        status.thinking = provider.thinking
        status.hint = KEY_HINT
        status.refresh_status()

        # The two hooks the tool layer reaches the surface through. Set here
        # rather than in `build_conversation` because they are properties of
        # the surface, and the one-shot path and the REPL set their own.
        set_asker(self.driver.ask_questions)
        set_lane_announcer(self._lane_started)

        # The live view of scheduled runs. Bound to the session on screen, so a
        # job attached to a different conversation does not interrupt this one
        # — it lands in its own transcript, where somebody looking for it will
        # be. `since=now` is the horizon: runs that finished before this screen
        # opened are already in the durable session copy and replaying them
        # here would show them twice.
        self.live_tail = live.Tail(
            config_module.home(), session=self.driver.binding.record.id
        )
        live.reap(config_module.home())

        self.composer.focus()
        # One clock for everything that moves: draining events, repainting the
        # streaming answer, advancing the spinner. `REFRESH_HZ` is the REPL's,
        # so both surfaces feel the same and a long markdown answer is
        # re-rendered at the same rate in each.
        self.set_interval(1 / render.REFRESH_HZ, self._tick)

    async def _reveal_study(self, study: StudyPanel) -> None:
        """Reveal the study once in place, without a second screen renderer."""
        await asyncio.sleep(0)
        width = study.size.width or self.size.width
        # The art has two discrete source plates: 24 and 34 rows. The wide
        # plate used to be selected from width alone and then clipped by a
        # shorter masthead. Choose the compact plate whenever all 34 rows
        # cannot actually be shown, so Leonardo keeps his feet and caption.
        art_width = max(width - 2, 40)
        if study.size.height < 34:
            art_width = min(art_width, 85)
        rows = art.study(art_width)
        if not rows:
            return

        widgets: list[Static] = []
        figure_rows: list[int] = []
        for index, (line, style) in enumerate(rows):
            is_figure = style == "figure"
            widgets.append(
                study.add_row(
                    "" if is_figure else line.rstrip(),
                    tone=style or "muted",
                )
            )
            if is_figure:
                figure_rows.append(index)

        if not figure_rows:
            return

        # Yield once so Textual paints the empty, already-sized frame before
        # the sweep begins. This is the only run; there is no recurring timer.
        if not self.masthead.display:
            for row_index in figure_rows:
                widgets[row_index].update(
                    Text(rows[row_index][0].rstrip(), style=screen_style("figure"))
                )
            return

        steps = len(figure_rows) + art.SCAN_BAND
        delay = art.SCAN_SECONDS / max(steps, 1)
        for head in range(steps + 1):
            for position, row_index in enumerate(figure_rows):
                distance = head - position
                if distance < 0:
                    text = ""
                    tone = "figure"
                else:
                    text = rows[row_index][0].rstrip()
                    tone = "bold white" if distance < art.SCAN_BAND else "figure"
                widgets[row_index].update(Text(text, style=screen_style(tone)))
            await asyncio.sleep(delay)

        for row_index in figure_rows:
            widgets[row_index].update(
                Text(rows[row_index][0].rstrip(), style=screen_style("figure"))
            )

    def _sync_masthead_layout(self, measured_size=None) -> None:
        """Keep the portrait generous on large screens and out of the way on small ones."""
        size = measured_size or self.size
        if size.width <= 0 or size.height <= 0:
            return
        self.masthead.display = size.height >= 38
        self.recent_updates.display = size.width >= 116
        self.header_meta.display = size.width >= 90
        self.header_state.display = size.width >= 116
        if self.masthead.display:
            self.masthead.styles.height = min(29, max(27, int(size.height * 0.45)))

    def on_resize(self, event) -> None:
        # Textual dispatches this user handler before App._on_resize updates
        # `self.size`. Reading `self.size` here therefore sees the *previous*
        # terminal dimensions—the exact reason a session opened at 80×24 and
        # stayed collapsed after the real 160×60 resize. The event is the
        # source of truth for this transition.
        if hasattr(self, "masthead"):
            self._sync_masthead_layout(event.size)

    def _state_health(self) -> list[str]:
        """Anything the session index needs said, or nothing.

        Same check the REPL runs and the same reason: a stale index makes
        search answer "nothing found", which reads as the truth. Once a day,
        silent when healthy.
        """
        try:
            from andromeda_cli import __version__, state

            return list(state.startup_check(__version__).lines)
        except Exception:  # noqa: BLE001 - never fail a session over a check
            return []

    @staticmethod
    def _pending_suggestions() -> int:
        """How many automations are waiting on a decision.

        Suggestions behind a command nobody runs are suggestions that do not
        exist. Counted, not listed — the intro is not the place to decide.
        """
        try:
            from andromeda_agent.suggestions import Suggestions
            from andromeda_cli.session import schedule_path

            return len(
                Suggestions(schedule_path().parent / "suggestions.json").pending()
            )
        except Exception:  # noqa: BLE001 - never fail a session over a hint
            return 0

    # ---- the clock --------------------------------------------------------

    def _tick(self) -> None:
        transcript = self.transcript
        for event in self.driver.drain():
            self._handle(event, transcript)
        self._drain_live(transcript)
        # `busy` alone was not enough: an autonomous run arrives precisely when
        # the driver is idle, so its text accumulated with nothing ever
        # repainting it — which is why the flush used to be per record.
        if self.driver.busy or self._live_text_pending:
            transcript.flush_answer()
            self._live_text_pending = False
        self.activity.tick(self.driver.busy)
        status = self.status
        status.context = self.conversation.context_used
        status.refresh_status()

    def _handle(self, event: ev.UiEvent, transcript: Transcript) -> None:
        if isinstance(event, ev.TurnStarted):
            transcript.add_prompt(event.prompt)

        elif isinstance(event, ev.TextDelta):
            transcript.ensure_frame(self.ANSWER_FRAME)
            transcript.feed_answer(event.text)

        elif isinstance(event, ev.ToolStarted):
            # The answer block is closed here and the next one opens on the
            # next delta, so prose the model wrote before a tool stays above
            # the tool line and prose it writes after stays below. The *frame*
            # is not closed — the tool is part of this turn, and closing it
            # here is what produced a bracket per segment.
            transcript.end_answer()
            transcript.ensure_frame(self.ANSWER_FRAME)
            transcript.add_tool(event.summary, event.tier)
            self.activity.start_tool(event.summary)

        elif isinstance(event, ev.ToolFinished):
            self.activity.stop_tool()
            transcript.add_tool_result(event.detail, event.ok)

        elif isinstance(event, ev.ToolDenied):
            self.activity.stop_tool()
            transcript.add_tool_result(f"{event.name} — {event.reason}", ok=False)

        elif isinstance(event, ev.LaneStarted):
            transcript.add_note(f"⇢ {event.specialist} lane  {event.label}", tone="magenta")

        elif isinstance(event, ev.Compacted):
            transcript.add_note(f"compacted — {event.detail}")

        elif isinstance(event, ev.QuestionAsked):
            self._ask(event)

        elif isinstance(event, ev.QuestionClosed):
            self._question_closed(event.request_id)

        elif isinstance(event, ev.TurnFinished):
            self._report_still_running(event, transcript)
            transcript.close_frame()
            self._finish_turn()

        elif isinstance(event, ev.TurnFailed):
            transcript.ensure_frame(self.ANSWER_FRAME)
            transcript.add_error(event.message, event.hint)
            transcript.close_frame()
            self._finish_turn()

        elif isinstance(event, ev.TurnInterrupted):
            transcript.ensure_frame(self.ANSWER_FRAME)
            transcript.add_note("interrupted", tone="warn")
            transcript.close_frame()
            self._finish_turn()

        elif isinstance(event, ev.Notice):
            transcript.add_note(event.text, tone=event.tone)

    # ---- scheduled runs, live ---------------------------------------------

    def _drain_live(self, transcript: Transcript) -> None:
        """Paint whatever a scheduled run has done since the last tick.

        The daemon is a different process with no screen. It writes a journal;
        this reads it. That is the whole mechanism, and it is deliberately one
        directional — a surface that could talk back would be a surface a job
        waits for, and a job that waits for a window to be open is not
        autonomous.

        Never raises. A journal that cannot be read costs the live rows, not
        the conversation.
        """
        tail = getattr(self, "live_tail", None)
        if tail is None:
            return
        try:
            records = tail.poll()
        except Exception:  # noqa: BLE001 - the transcript is worth more than the tail
            return
        if not records:
            return

        # Whether a turn of this person's own was interrupted by the run. The
        # frame is put back afterwards so the answer they are reading does not
        # end up with a job's rows inside its box.
        resume_frame = transcript.frame_tone == "agent" and self.driver.busy

        for record in records:
            self._paint_live(record, transcript)

        if resume_frame:
            transcript.ensure_frame(self.ANSWER_FRAME)

    def _paint_live(self, record: dict, transcript: Transcript) -> None:
        kind = record.get("kind") or ""
        name = str(record.get("name") or record.get("job") or "job")
        where = str(record.get("where") or "local")
        # `⌂` and `☁` are the same two glyphs the sessions rail uses for the
        # same two facts. One vocabulary, so a person learns it once.
        badge = "☁" if where == "cloud" else "⌂"
        # The label is fixed and tracked; the job's name rides beside it as
        # plain text. `AUTONOMOUS` is what the reader is scanning for, and it
        # stays the same width whatever the job is called.
        frame = "autonomous"
        detail = f"{badge} {name}"

        if kind == "run.started":
            transcript.ensure_frame(frame, "autonomous", detail)
            transcript.add_note(
                f"started · {where} · {record.get('reason') or 'scheduled'}",
                tone="autonomous",
            )
            return

        if kind == "text":
            transcript.ensure_frame(frame, "autonomous", detail)
            # Accumulate only. Painting is the tick's job, not the record's —
            # flushing here put every coalesced chunk on screen as its own
            # block, so a run that said one paragraph arrived as five pieces.
            # A live turn has never done that; this is now the same path.
            transcript.feed_answer(str(record.get("text") or ""))
            self._live_text_pending = True
            return

        if kind == "tool":
            transcript.ensure_frame(frame, "autonomous", detail)
            transcript.end_answer()
            transcript.add_tool(
                str(record.get("summary") or ""), str(record.get("tier") or "")
            )
            return

        if kind == "tool.result":
            transcript.add_tool_result(
                str(record.get("detail") or ""), bool(record.get("ok"))
            )
            return

        if kind == "note":
            transcript.ensure_frame(frame, "autonomous", detail)
            transcript.add_note(str(record.get("text") or ""), tone="autonomous")
            return

        if kind == "run.finished":
            transcript.ensure_frame(frame, "autonomous", detail)
            status = str(record.get("status") or "")
            error = str(record.get("error") or "")
            if error:
                transcript.add_error(error)
            transcript.add_note(f"finished · {status}", tone="autonomous")
            transcript.close_frame()

    def _report_still_running(self, event: ev.TurnFinished, transcript: Transcript) -> None:
        if event.processes_running:
            transcript.add_note(
                f"{len(event.processes_running)} background process(es) running: "
                + ", ".join(event.processes_running)
            )
        if event.lanes_running:
            transcript.add_note(
                f"{len(event.lanes_running)} lane(s) still running: "
                + ", ".join(event.lanes_running)
                + " — ask me to wait for them"
            )

    def _finish_turn(self) -> None:
        self.activity.stop_tool()
        # A turn can have created a job, and a job now creates a session of its
        # own. Without this the rail keeps showing what was on disk when the
        # app launched, so the conversation the agent just told you about is
        # not there when you look for it. Cheap: one directory listing.
        try:
            self.recent_updates.reload()
            self.recent_updates._refresh()
        except Exception:  # noqa: BLE001 - the rail is furniture, never a crash
            pass
        # One at a time, and only when the agent is actually free. Draining the
        # whole queue would submit the second item into a turn that has not
        # started yet, and `submit` would refuse it silently.
        if self.queued and not self.driver.busy:
            # The placeholder row goes as the real turn begins — `submit`
            # paints the prompt properly, and leaving both would show the
            # message twice.
            if self._queued_rows:
                row = self._queued_rows.pop(0)
                try:
                    row.remove()
                except Exception:  # noqa: BLE001 - an unmounted row is fine
                    pass
            self.driver.submit(self.queued.pop(0))
        self._refresh_queue_hint()

    def _lane_started(self, specialist: str, label: str) -> None:
        """Called from a lane's own thread. Goes through the queue like everything else."""
        self.driver.post(ev.LaneStarted(specialist=specialist, label=label))

    # ---- questions --------------------------------------------------------

    def _ask(self, event: ev.QuestionAsked) -> None:
        """Take the screen, and take it away from the composer.

        `push_screen` alone would be enough for Textual — a modal screen
        receives every key. Disabling the composer as well is belt and braces:
        the first is a property of a library, the second is a property of this
        code and can be asserted in a test. The bug this guards has been
        introduced twice.
        """
        self._open_question = event.request_id
        composer = self.composer
        composer.disabled = True
        self.activity.waiting = True

        request_id = event.request_id

        def answered(value) -> None:
            # `dismiss` can fire after the driver has released the question —
            # an interrupt, a shutdown. `answer` returns False then and the
            # value is dropped, which is correct: the first resolution wins,
            # and the first one was the refusal.
            self.driver.answer(request_id, value)

        if event.form == "approval":
            self.push_screen(ApprovalScreen(event.body), answered)
        else:
            self.push_screen(ClarifyScreen(event.body), answered)

    def _question_closed(self, request_id: str) -> None:
        if request_id != self._open_question:
            return
        self._open_question = ""
        composer = self.composer
        composer.disabled = False
        self.activity.waiting = False
        # The screen may already be gone (the user answered it, which is what
        # closed the question). Popping only what is still there.
        if isinstance(self.screen, (ApprovalScreen, ClarifyScreen)):
            self.pop_screen()
        composer.focus()

    # ---- input ------------------------------------------------------------

    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        raw = event.text.strip()
        self.composer.clear_text()
        typed_at, self._typed_at = self._typed_at, None
        self._last_typed_at = typed_at

        if not raw:
            return

        self.history.append(raw)
        self._history_at = len(self.history)

        # A multi-line message is a paste or an editor's worth of writing, and
        # neither has keypress timing behind it. The injected-input guard only
        # judges single lines, which is all it was ever built to catch.
        if "\n" not in raw and self._looks_injected(raw):
            self.transcript.add_note(
                "ignored a line that arrived too fast to have been typed", tone="yellow"
            )
            self.transcript.add_note(f"  {raw[:90]}", tone="dim")
            return

        if raw.startswith("/") and "\n" not in raw:
            # Slash commands never queue. They are about the surface, not about
            # the conversation, and making `/tools` wait for a running turn
            # would be surprising.
            self._slash(raw)
            return

        if self.driver.busy:
            # Shown where it was typed, not just counted in the status bar. A
            # message that clears the composer and leaves no trace reads as
            # swallowed, and people retype it.
            self.queued.append(raw)
            self._queued_rows.append(self.transcript.add_queued_prompt(raw))
            self._refresh_queue_hint()
            self.palette.close()
            return

        self.palette.close()
        self.driver.submit(raw)

    def on_text_area_changed(self, event) -> None:
        """Grow the composer with the text, and time the first keystroke."""
        self.composer.resize_to_content()
        # Every keystroke, so the list opens the instant `/` is typed and
        # narrows as the word is finished. Waiting for a Tab is what a shell
        # does, and it works there because you already know the command exists.
        # Here the list is the only way anybody finds out that it does.
        self.palette.sync(self.composer.text)
        if self.composer.text and self._typed_at is None:
            self._typed_at = time.monotonic()
        elif not self.composer.text:
            self._typed_at = None

    def _looks_injected(self, text: str) -> bool:
        """Delegates to the REPL's rule so the two surfaces cannot disagree.

        `_typed_at` is stamped on the first character of each message; a line
        with no keypress behind it reads as instantaneous, which is what it is.
        Only ever asked about single-line messages — a paste has no typing
        behind it by definition, and judging one would turn a guard against
        losing input into a way of losing it.
        """
        from andromeda_cli.repl import looks_injected

        typed_for = time.monotonic() - (self._last_typed_at or time.monotonic())
        return looks_injected(text, typed_for, time.monotonic() - self._started_at)

    def _refresh_queue_hint(self) -> None:
        status = self.status
        parts = []
        if self.queued:
            parts.append(f"{len(self.queued)} queued")
        parts.append(KEY_HINT)
        status.hint = " · ".join(parts)
        status.refresh_status()

    def on_composer_changed(self, event) -> None:
        """The first character of a line, for the injected-input guard."""
        if event.value and self._typed_at is None:
            self._typed_at = time.monotonic()
        elif not event.value:
            self._typed_at = None

    def on_key(self, event) -> None:
        """History on up/down, when the composer has focus and nothing is open."""
        if self._open_question or self.composer.disabled:
            return
        if event.key not in {"up", "down"}:
            return
        if not self.history:
            return
        composer = self.composer
        if composer.text and self._history_at >= len(self.history):
            # A draft in progress is not history navigation. Leave the arrow to
            # the input's own cursor handling rather than eating what was typed.
            return
        event.stop()
        step = -1 if event.key == "up" else 1
        self._history_at = max(0, min(len(self.history), self._history_at + step))
        composer.load_text(
            self.history[self._history_at] if self._history_at < len(self.history) else ""
        )
        composer.resize_to_content()

    # ---- actions ----------------------------------------------------------

    def action_interrupt(self) -> None:
        """Ctrl-C: stop the turn, then clear the draft, then say how to leave.

        **It never quits.** That matches the REPL, where Ctrl-C clears the line
        and Ctrl-D is the way out — and it closes an accidental-exit path that
        matters more here than in a line editor. A terminal is not a private
        channel: shell integrations clear the current line before they type
        into it, multiplexers replay buffers, and a stray `\x03` arriving from
        any of those would otherwise end the session and everything staged in
        it. Losing a conversation to a keystroke nobody pressed is not a
        trade worth one saved keypress.
        """
        composer = self.composer
        if self.driver.busy or self._open_question:
            self.driver.interrupt()
            return
        if composer.text:
            composer.clear_text()
            return
        if self.queued:
            self.queued.clear()
            for row in self._queued_rows:
                try:
                    row.remove()
                except Exception:  # noqa: BLE001 - an unmounted row is fine
                    pass
            self._queued_rows.clear()
            self._refresh_queue_hint()
            self.transcript.add_note("queue cleared")
            return
        self.transcript.add_note("nothing to interrupt — ctrl+d quits")

    def action_new_conversation(self) -> None:
        if self.driver.busy:
            self.transcript.add_note("finish or interrupt the turn first")
            return
        self.conversation.reset()
        self.transcript.add_note("new conversation", tone="green")

    def action_scroll_transcript(self, direction: int) -> None:
        flow = self.conversation_scroll
        page = max(1, flow.size.height - 2)
        flow.scroll_relative(y=direction * page, animate=False)

    def action_edit_in_editor(self) -> None:
        """Hand the whole screen to `$EDITOR`, then take it back.

        This is the one place the literal form of the rule applies inside the
        TUI: the editor is a separate program that owns the terminal, so the
        app is suspended — its alternate screen released and its input handling
        stopped — before it runs. Resuming without that leaves two programs
        drawing to one terminal, which is the same failure the approval prompt
        had in the REPL.
        """
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if not editor:
            # A default rather than a refusal: this is the reliable way to get a
            # long prompt in when a terminal mishandles paste, so "no $EDITOR"
            # should not be a dead end. `vi` is on every machine this runs on.
            editor = "vi"
            self.transcript.add_note("no $EDITOR set — using vi", tone="dim")
        composer = self.composer
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".md", prefix="andromeda-", delete=False
        ) as handle:
            handle.write(composer.text)
            path = Path(handle.name)
        try:
            with self.suspend():
                completed = subprocess.run([*editor.split(), str(path)], check=False)
            if completed.returncode == 0:
                self._apply_editor_text(path.read_text(encoding="utf-8").strip())
        except OSError as exc:
            self.transcript.add_note(f"could not run {editor}: {exc}", tone="red")
        except SuspendNotSupported:
            # Headless, or a driver that cannot hand the terminal over. Saying
            # so beats a traceback that ends the session.
            self.transcript.add_note("this terminal cannot suspend for an editor", tone="red")
        finally:
            path.unlink(missing_ok=True)
        composer.focus()

    def _apply_editor_text(self, written: str) -> None:
        """What came back from `$EDITOR`, straight into the field.

        Split out from the suspend-and-launch dance so it can be tested:
        `App.suspend` is unavailable headless, and the interesting part is what
        happens to the text.
        """
        self.composer.load_text(written)
        self.composer.resize_to_content()

    @property
    def record(self) -> Any:
        """The transcript this screen is writing to.

        Read through the driver's binding rather than stored, so `/resume`
        has one place to change and cannot leave the screen and the worker
        thread disagreeing about which session they are in.
        """
        return self.driver.binding.record

    # ---- slash commands ---------------------------------------------------

    def _slash(self, command: str) -> None:
        parts = command.split()
        verb = parts[0].lower()
        transcript = self.transcript

        if verb in {"/exit", "/quit"}:
            self.exit(_reason="slash /exit")
        elif verb == "/help":
            transcript.add_note(slash_help() + "\n  " + KEY_HINT)
        elif verb == "/new":
            self.action_new_conversation()
        elif verb == "/cwd":
            transcript.add_note(str(self.conversation.workspace.root))
        elif verb == "/model":
            provider = self.conversation.provider
            transcript.add_note(f"{provider.label} · {provider.model}")
        elif verb == "/credits":
            # Same three distinct cases the REPL states, for the same reason: a
            # confident "$0.00" would be wrong in all of them.
            from andromeda_agent import credits as credits_module

            provider = self.conversation.provider
            line = credits_module.summary(provider.balance)
            if line:
                # The lag is stated, because the number looks stuck otherwise:
                # the relay stamps these headers from the balance it reserved
                # *before* answering, and settles the charge when the reply
                # ends. `/usage` is the figure for the turn you just watched.
                transcript.add_note(
                    f"{line}\nas of your previous turn — /usage for this session"
                )
            elif provider.name != "relay":
                transcript.add_note("No balance on this lane — you are using your own key.")
            else:
                transcript.add_note("No balance yet. It is read from the next reply.")
        elif verb == "/upgrade":
            self._upgrade(transcript)
        elif verb == "/usage":
            transcript.add_note(self._usage_lines())
        elif verb == "/tools":
            self._list_tools(transcript)
        elif verb == "/skills":
            self._list_skills(transcript)
        elif verb == "/lanes":
            self._list_specialists(transcript)
        elif verb == "/ps":
            self._list_processes(transcript)
        elif verb == "/think":
            self._think(parts, transcript)
        elif verb == "/history":
            self._list_checkpoints(transcript)
        elif verb == "/rewind":
            self._rewind(parts, transcript)
        elif verb == "/recap":
            self._recap(transcript)
        elif verb == "/sessions":
            self._search_sessions(parts, transcript)
        elif verb == "/resume":
            self._resume(parts, transcript)
        elif verb == "/jobs":
            self._list_jobs(transcript)
        elif verb == "/approve":
            self._approve_job(parts, transcript)
        elif vocabulary.is_verb(verb):
            self._verb(verb, command[len(parts[0]):].strip(), transcript)
        else:
            near = vocabulary.matching(verb)[:4]
            hint = "  ".join(row.display for row in near)
            transcript.add_note(
                f"unknown command {verb} — type / to see them all"
                + (f"\n  did you mean: {hint}" if hint else ""),
                tone="yellow",
            )

    def _verb(self, verb: str, arguments: str, transcript) -> None:
        """Run an `andromeda` command and put its output in the transcript.

        The command writes to a console, and this screen owns the terminal — so
        it is captured and added as a note rather than printed underneath the
        interface, which is where it would otherwise land and stay invisible.
        """
        import io
        from contextlib import redirect_stdout

        from andromeda_cli import output as output_module

        buffer = io.StringIO()
        previous = output_module.console.file
        try:
            output_module.console.file = buffer
            with redirect_stdout(buffer):
                vocabulary.run_verb(verb, arguments)
        except Exception as exc:  # noqa: BLE001 - a verb must not kill the screen
            transcript.add_note(f"{verb} failed: {exc}", tone="yellow")
            return
        finally:
            output_module.console.file = previous

        text = buffer.getvalue().rstrip()
        transcript.add_note(text or f"{verb} had nothing to say.")

    def _recap(self, transcript: Transcript) -> None:
        from andromeda_cli import state

        summary = state.build_recap(
            self.conversation.messages, getattr(self.conversation, "todos", None)
        )
        transcript.add_note("\n".join(f"  {line}" for line in summary.lines()))

    def _search_sessions(self, parts: list[str], transcript: Transcript) -> None:
        from andromeda_cli import state

        query = " ".join(parts[1:]).strip()
        if not query:
            transcript.add_note("/sessions <text> — searches every past session")
            return
        hits = state.search(query, limit=8)
        if not hits:
            transcript.add_note(f"nothing found for {query!r}")
            return
        rows = []
        for hit in hits:
            row = Text("  ")
            row.append(f"{hit.session_id}@{hit.position}  ", style="cyan")
            row.append(hit.role.ljust(9) + " ", style="dim")
            row.append(" ".join(hit.snippet.replace("»", "").replace("«", "").split())[:90])
            rows.append(row)
        rows.append(Text("  /resume <id> to switch to one", style="dim"))
        self._rows(transcript, rows, "nothing found")

    def _resume(self, parts: list[str], transcript: Transcript) -> None:
        """Switch this screen to another transcript.

        Refused mid-turn for the same reason `/rewind` is: the worker thread is
        appending to the message list this would replace, and the two orders
        the race can finish in produce different transcripts.
        """
        from andromeda_cli import sessions as store

        if self.driver.busy:
            transcript.add_note("finish or interrupt the turn first")
            return

        binding = self.driver.binding
        recent = [
            session
            for session in store.recent(limit=10)
            if session.id != binding.record.id
        ]

        if len(parts) < 2:
            if not recent:
                transcript.add_note("no other sessions to switch to")
                return
            rows = []
            for number, session in enumerate(recent, start=1):
                row = Text("  ")
                row.append(f"{str(number).rjust(2)}  ", style="cyan")
                row.append(f"{session.id}  {str(session.turns).rjust(3)} turns  ", style="dim")
                row.append(session.title)
                rows.append(row)
            rows.append(Text("  /resume <number or id>", style="dim"))
            self._rows(transcript, rows, "no other sessions")
            return

        choice = parts[1].strip()
        if choice.isdigit() and 1 <= int(choice) <= len(recent):
            target = recent[int(choice) - 1]
        else:
            target = store.resolve(choice)
        if target is None:
            transcript.add_note(f"no session matching {choice!r}", tone="yellow")
            return
        if target.id == binding.record.id:
            transcript.add_note("already in that session")
            return

        self._switch_to(target, transcript)

    def _switch_to(self, target, transcript: Transcript) -> None:
        """Make `target` the live conversation.

        Extracted so `/resume` and the sessions rail cannot drift apart. They
        are the same act reached two ways, and the checkpoint-stack handover in
        the middle is the part that would be forgotten by whichever of the two
        was written second.
        """
        binding = self.driver.binding
        binding.switch(target, self.conversation.messages)
        self.conversation.messages = list(target.messages)
        # The checkpoint stack belongs to the transcript, not to the screen.
        self.checkpoints = checkpoint_module.CheckpointStack.from_json(
            target.checkpoints
        )
        self.driver.checkpoints = self.checkpoints
        transcript.remove_children()
        self._replay(target)
        transcript.add_note(
            f"now in {target.id} · {target.turns} turns · {target.title}",
            tone="green",
        )
        # The live tail follows the conversation on screen. Without this, a
        # switch leaves it pointed at the session that was open when the app
        # launched, and scheduled runs paint into the wrong transcript — which
        # is worse than not painting at all.
        tail = getattr(self, "live_tail", None)
        if tail is not None:
            tail.session = target.id
        self.recent_updates.reload()
        self.recent_updates._refresh()

    # -- the sessions rail --------------------------------------------------

    def action_rail_tab(self, delta: int) -> None:
        self.recent_updates.switch_tab(delta)

    def action_rail_move(self, delta: int) -> None:
        self.recent_updates.move(delta)

    def action_rail_delete(self) -> None:
        self.recent_updates.delete_selected()

    def on_sessions_rail_deleted(self, message) -> None:
        """Remove a transcript, having been asked twice.

        The live session is refused rather than handled. Deleting the file the
        conversation on screen is still writing to would not stop the writing:
        the next save recreates it, half of it, under the same id. "Start a new
        one first" is a one-key answer and leaves nothing ambiguous.
        """
        transcript = self.transcript
        session_id = message.session_id
        if session_id == self.driver.binding.record.id:
            transcript.add_note(
                "that is the session you are in — press CTRL+L for a new one first",
                tone="warn",
            )
            return

        from andromeda_cli import sessions as store

        if store.delete(session_id):
            self.recent_updates.forget(session_id)
            transcript.add_note(f"deleted session {session_id}", tone="muted")
        else:
            transcript.add_note(
                f"could not delete {session_id}", tone="bad"
            )

    def on_sessions_rail_opened(self, message) -> None:
        """Someone picked a session in the rail.

        Refused mid-turn for the same reason `/resume` is: the worker thread is
        appending to the message list this would replace, and the two orders
        the race can finish in produce different transcripts. Refused with a
        note rather than silently, or a click that does nothing reads as a
        broken rail.
        """
        transcript = self.transcript
        if self.driver.busy:
            transcript.add_note("finish or interrupt the turn first")
            return
        if message.session_id == self.driver.binding.record.id:
            transcript.add_note("already in that session")
            return

        from andromeda_cli import sessions as store

        target = store.load(message.session_id)
        if target is None:
            transcript.add_note("that session could not be read", tone="yellow")
            return
        self._switch_to(target, transcript)

    def _capture(self, call) -> list[str]:
        """Run a CLI command function and collect what it printed.

        The commands under `andromeda_cli.commands` write through the shared
        rich console, which targets the real stdout. Textual owns that, so
        calling one directly paints over the interface. Capturing the output
        and re-emitting it as transcript notes is what lets the full-screen
        surface offer the same verbs as the terminal instead of telling people
        to leave in order to use them.
        """
        import contextlib
        import io

        from andromeda_cli import output as output_module

        sink = io.StringIO()
        console = output_module.console
        # Save and restore the PRIVATE `_file`, never the `file` property.
        # Reading `console.file` when nothing is pinned returns `sys.stdout`
        # *as it is right now*, so writing that value back pins the shared
        # console to one stream forever — it stops following stdout, and every
        # later caller writes into whatever object happened to be current when
        # the first slash command ran. Caught by unrelated suites going quiet
        # when they ran after this one.
        original = console._file
        console._file = sink
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                call()
        except Exception as exc:  # noqa: BLE001 - a slash command never kills the app
            return [f"{type(exc).__name__}: {exc}"]
        finally:
            console._file = original
        return [line.rstrip() for line in sink.getvalue().splitlines()]

    def _list_jobs(self, transcript: Transcript) -> None:
        """What is scheduled, without leaving to find out."""
        from andromeda_cli.commands import cron as cron_cmd

        lines = self._capture(lambda: cron_cmd.show_list())
        self._rows(
            transcript,
            [Text(line) for line in lines if line.strip()],
            "no scheduled jobs",
        )

    def _approve_job(self, parts: list[str], transcript: Transcript) -> None:
        """Widen a job from looking to acting.

        This stays a decision a person makes — an agent may propose autonomy
        and only a person grants the unattended kind — but the deciding now
        happens where the proposal was read. Sending somebody to a shell to
        type the grant was never a safety property; it was a missing surface,
        and it made the safe path the inconvenient one.
        """
        from andromeda_cli.commands import cron as cron_cmd

        if len(parts) < 2:
            transcript.add_note(
                "/approve <job id> — lets that job change files and run "
                "commands. /jobs lists them."
            )
            return

        identifier = parts[1].strip()
        # `--run-on cloud` is deliberately not reachable here. Moving a job
        # onto hardware the person does not hold is a larger grant than this
        # one and deserves reading a refusal matrix, not a slash command typed
        # in the middle of a conversation.
        lines = self._capture(
            lambda: cron_cmd.approve(identifier, approval="auto")
        )
        self._rows(
            transcript,
            [Text(line) for line in lines if line.strip()],
            f"could not approve {identifier}",
        )

    def _replay(self, session) -> None:
        """Redraw a switched-to session's conversation on the screen.

        Only what was said. Tool calls and their results are not replayed —
        they are a record of work, and re-rendering hundreds of them to
        reconstruct a scrollback nobody scrolled is how a switch takes four
        seconds instead of none.

        A run the clock asked for keeps its amber frame across the replay. The
        marker `cron._append_to_session` writes is the only thing on disk that
        distinguishes one, so reading it back here is what stops a resumed
        transcript flattening a distinction the live one drew.
        """
        frame = (self.ANSWER_FRAME, "agent")
        for message in session.messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            body = content.strip()
            if role == "user":
                marker = self._job_marker(body)
                if marker:
                    # The marker is furniture, not something anyone said. It
                    # becomes the frame's label and is not printed as a turn.
                    frame = (f"autonomous · {marker}", "autonomous")
                    continue
                frame = (self.ANSWER_FRAME, "agent")
                self.transcript.close_frame()
                self.transcript.add_prompt(body)
            elif role == "assistant":
                self.transcript.ensure_frame(*frame)
                self.transcript.feed_answer(body)
                self.transcript.end_answer()
                frame = (self.ANSWER_FRAME, "agent")
        self.transcript.close_frame()

    @staticmethod
    def _job_marker(body: str) -> str:
        """The job name out of a `[scheduled job NAME ran …]` marker, or ""."""
        if not body.startswith("[scheduled job ") or not body.endswith("]"):
            return ""
        inner = body[len("[scheduled job ") : -1]
        name = inner.split(" ran", 1)[0].strip()
        return name or "job"

    def _rows(self, transcript: Transcript, rows: list[Text], empty: str) -> None:
        if not rows:
            transcript.add_note(empty)
            return
        block = Text("\n").join(rows)
        transcript.mount(Static(block, classes="row note"))
        transcript.scroll_end(animate=False)

    def _upgrade(self, transcript: Transcript) -> None:
        """Hand them to the browser. The one thing the terminal cannot do.

        The url is printed whether or not the browser opened: over SSH, on a
        headless box, or with no handler registered, `webbrowser.open` returns
        False and a message that only said "opened your browser" would be a
        dead end.
        """
        from andromeda_cli.commands import auth as auth_module

        url, opened = auth_module.open_upgrade()
        transcript.add_note(
            ("Opened your browser to change your plan." if opened
             else "Open this to change your plan:"),
            tone="autonomous",
        )
        transcript.add_note(url)
        # Said because the number on the status bar will look unchanged until
        # then: the balance is read off the next reply's headers, not polled.
        transcript.add_note(
            "your new balance appears on the next reply", tone="muted"
        )

    def _usage_lines(self) -> str:
        """`/usage` — tokens this session, and tokens this week.

        The question `/credits` cannot answer: a balance is an account figure
        that lags a turn and does not exist at all on the BYOK lane. This is
        counted from the provider's own reply, and reads the same transcripts
        `andromeda status` does, so the two cannot disagree.
        """
        import time

        from andromeda_agent import usage as usage_module

        from andromeda_cli.commands import status as status_cmd

        lines: list[str] = []
        session = getattr(self.conversation, "usage", None)
        if session is None or session.empty:
            lines.append(
                "Nothing counted yet — usage is read from the provider's own "
                "reply, so it starts at your next turn."
            )
        else:
            lines.append(
                f"this session  {usage_module.compact(session.total)} tokens "
                f"({usage_module.compact(session.input)} in, "
                f"{usage_module.compact(session.output)} out, "
                f"{session.requests} request(s))"
            )

        week = usage_module.Usage()
        sessions = 0
        for record in status_cmd._recent(
            time.time() - status_cmd.RECENT_DAYS * 86_400
        ):
            entry = usage_module.Usage.from_dict(record.usage)
            if entry.empty:
                continue
            week.merge(entry)
            sessions += 1
        if not week.empty:
            plural = "" if sessions == 1 else "s"
            lines.append(
                f"last {status_cmd.RECENT_DAYS} days  "
                f"{usage_module.compact(week.total)} tokens "
                f"({week.requests} request(s) across {sessions} session{plural})"
            )
        return "\n".join(lines)

    def _list_tools(self, transcript: Transcript) -> None:
        rows = []
        for spec in sorted(self.conversation.available, key=lambda item: item.name):
            decision = self.conversation.policy.decide(spec)
            row = Text("  ")
            row.append(spec.name.ljust(18), style="cyan")
            row.append(
                f"{spec.risk_tier.ljust(12)} "
                f"{'asks first' if decision == 'needs_approval' else 'auto'}",
                style="dim",
            )
            rows.append(row)
        self._rows(transcript, rows, "no tools available")

    def _list_skills(self, transcript: Transcript) -> None:
        from andromeda_tools import skills as skills_module

        found = skills_module.discover(self.conversation.workspace.root)
        rows = []
        for skill in sorted(found.values(), key=lambda item: item.name):
            row = Text("  ")
            row.append(skill.name.ljust(20), style="cyan")
            row.append(skill.description[:70], style="dim")
            if not skill.available:
                row.append(f"  needs {', '.join(skill.missing_bins)}", style="yellow")
            rows.append(row)
        self._rows(transcript, rows, "no skills found")

    def _list_specialists(self, transcript: Transcript) -> None:
        from andromeda_agent.specialists import SPECIALISTS

        rows = []
        for belt in SPECIALISTS.values():
            row = Text("  ")
            row.append(belt.id.ljust(12), style="magenta")
            row.append(f"{belt.max_turns} steps · {belt.purpose}", style="dim")
            rows.append(row)
        self._rows(transcript, rows, "no specialists")

    def _list_processes(self, transcript: Transcript) -> None:
        registry = getattr(self.conversation, "process_registry", None)
        processes = registry.all() if registry else []
        self._rows(
            transcript,
            [Text(f"  {process.summary()}", style="dim") for process in processes],
            "no background processes",
        )

    def _think(self, parts: list[str], transcript: Transcript) -> None:
        provider = self.conversation.provider
        if not supports_reasoning(provider.model):
            transcript.add_note(f"{provider.model} does not support thinking levels")
            return
        if len(parts) > 1:
            level = parts[1].lower()
            if level not in THINKING_LEVELS:
                transcript.add_note(
                    f"unknown level {level!r} — one of: {', '.join(THINKING_LEVELS)}",
                    tone="yellow",
                )
                return
            # Set on the provider so it takes effect next turn without
            # rebuilding the session and losing the transcript.
            provider.thinking = level
            status = self.status
            status.thinking = level
            status.refresh_status()
        transcript.add_note(f"thinking: {provider.thinking}")

    def _list_checkpoints(self, transcript: Transcript) -> None:
        rows = []
        for checkpoint in self.checkpoints.all():
            row = Text("  ")
            row.append(str(checkpoint.index).rjust(3), style="cyan")
            row.append(f"  {str(checkpoint.turns).rjust(2)} turns  ", style="dim")
            row.append(checkpoint.label)
            rows.append(row)
        self._rows(transcript, rows, "no checkpoints yet")

    def _rewind(self, parts: list[str], transcript: Transcript) -> None:
        if self.driver.busy:
            transcript.add_note("finish or interrupt the turn first")
            return
        if not len(self.checkpoints):
            transcript.add_note("nothing to rewind to yet")
            return
        index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target = self.checkpoints.resolve(index)
        if target is None:
            transcript.add_note("no such checkpoint — /history lists them", tone="yellow")
            return
        self.conversation.messages = self.checkpoints.rewind_to(target)
        # The transcript on disk must match the one in memory, or resuming
        # would restore the turns that were just undone.
        self.record.messages = self.conversation.messages
        self.record.checkpoints = self.checkpoints.to_json()
        self.record.save()
        transcript.add_note(f"rewound to before: {target.label}", tone="green")

    # ---- shutdown ---------------------------------------------------------

    def exit(self, *args, **kwargs):
        """Record *why* the app is exiting before it takes the screen away.

        Every quit path goes through here, so one line covers the deliberate
        ones and anything that calls `exit` unexpectedly. Read back from
        `~/.andromeda-cli/tui.log`.
        """
        self._exit_reason = kwargs.pop("_reason", self._exit_reason or "exit()")
        return super().exit(*args, **kwargs)

    def action_quit_app(self) -> None:
        self.exit(_reason="ctrl+d")

    def on_unmount(self) -> None:
        """Release every gate before the screen goes.

        An agent thread parked in the approval gate is waiting on an event only
        this can set. Without it, quitting with a prompt open leaves a
        non-daemon-looking thread blocked forever — and, worse, leaves a tool
        that was never consented to in an undecided state rather than refused.
        """
        self._log_exit()
        killed = self.driver.shutdown()
        if killed:
            # Printed after the screen is gone, so it lands in the scrollback
            # the user is looking at rather than on a screen being torn down.
            render.note(f"stopped {killed} background process(es)")

    def _log_exit(self) -> None:
        try:
            from andromeda_cli import config as config_module

            path = config_module.home() / "tui.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  unmount  "
                    f"reason={self._exit_reason or 'unknown'}  "
                    f"alive={time.monotonic() - self._started_at:.1f}s\n"
                )
        except (OSError, Exception):  # noqa: BLE001 - logging must not fail a teardown
            pass


def build(config: dict[str, Any], conversation, record, resumed=None) -> AndromedaApp:
    return AndromedaApp(config, conversation, record, resumed=resumed)


__all__ = ["AndromedaApp", "build", "slash_help"]
