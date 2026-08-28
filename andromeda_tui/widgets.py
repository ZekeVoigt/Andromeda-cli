"""The parts of the screen.

Everything here paints through `andromeda_cli.render`, never around it. That is
the point of the module: the full-screen surface and the REPL are two views of
one product, and a chart or a heading has to look the same in both. The bar
glyphs, the restrained palette and the held-back unclosed fence are not
reimplemented — `render.paint()` runs the same code and hands back styled text.

Two consequences worth knowing before editing:

- A painted block is re-rendered on resize, because it was laid out at a fixed
  width. A widget that keeps its old text after a resize shows a table whose
  rules stop halfway across the terminal.
- Markdown is re-rendered from the whole answer on every update, not appended
  to. Markdown is not incremental — a `**` only becomes bold when its partner
  arrives — so there is nothing to append to. Updates are throttled instead;
  see `render.REFRESH_HZ` and the tick in `app.py`.
"""

from __future__ import annotations

import time
import re
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual.containers import VerticalGroup, VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from andromeda_cli import render

# The spinner. Braille rather than a bar, because it occupies one cell at every
# frame — a spinner that changes width makes the whole activity line jitter.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


SCREEN_TONES = {
    "accent": render.ZINC_50,
    "lane": render.ZINC_100,
    "muted": f"dim {render.ZINC_200}",
    "dim": f"dim {render.ZINC_200}",
    "eyebrow": f"bold {render.ZINC_200}",
    "figure": f"dim {render.ZINC_200}",
    "rule": f"dim {render.ZINC_200}",
    "ok": render.GOOD,
    "warn": f"bold {render.AUTONOMOUS}",
    # The structural hues, matching `render.THEME` name for name. The surface
    # and the REPL are two views of one product; a tone that means one thing in
    # one of them and another thing in the other is the drift this module's
    # docstring exists to prevent.
    "you": f"bold {render.YOU}",
    "agent": render.AGENT,
    "agent.rule": f"dim {render.AGENT}",
    "autonomous": f"bold {render.AUTONOMOUS}",
    "autonomous.rule": f"dim {render.AUTONOMOUS}",
    "good": render.GOOD,
    "bad": render.BAD,
    # Legacy tone names still arrive from slash commands and tool events. They
    # now resolve to the structural hues rather than to greys — the reason they
    # were flattened was a monochrome palette, and that reason is gone.
    "red": f"bold {render.BAD}",
    "yellow": render.AUTONOMOUS,
    "green": render.GOOD,
    "magenta": render.AGENT,
    "cyan": render.YOU,
}


def screen_style(tone: str) -> str:
    """Resolve product theme aliases before Textual sees Rich text."""
    return SCREEN_TONES.get(tone, tone)


def latest_cli_changes(limit: int = 3) -> list[tuple[str, str]]:
    """Read the newest product changes from the CLI's own changelog.

    The installed macOS command is an editable checkout, so the changelog sits
    beside the packages. Keeping the rail sourced from that file prevents a
    conversation event from masquerading as a product update.
    """
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    try:
        lines = changelog.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    release = ""
    category = ""
    raw_changes: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            raw_changes.append(pending)
            pending = None

    for line in lines:
        if line.startswith("## ["):
            flush()
            release = line.removeprefix("## [").split("]", 1)[0].upper()
            continue
        if line.startswith("### "):
            flush()
            category = line.removeprefix("### ").strip().upper()
            continue
        if release and line.startswith("- "):
            flush()
            pending = (f"{release} / {category}", line[2:].strip())
            continue
        if pending is not None and (line.startswith("  ") or line.startswith("\t")):
            label, detail = pending
            pending = (label, f"{detail} {line.strip()}")
            continue
        flush()
    flush()

    changes: list[tuple[str, str]] = []
    for label, raw_detail in raw_changes[:limit]:
        detail = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw_detail)
        detail = re.sub(r"`([^`]+)`", r"\1", detail)
        detail = " ".join(detail.split())
        if len(detail) > 112:
            detail = detail[:109].rstrip() + "…"
        changes.append((label, detail))
    return changes


def _relative_age(when: float) -> str:
    """How long ago, in the shortest form that is still true.

    Rounded down, never up: a session touched 59 minutes ago reads "59m", not
    "1h". Rounding up puts a conversation in the wrong hour bucket, and the
    rail is a thing people scan for "the one from this morning".
    """
    seconds = max(0.0, time.time() - (when or 0.0))
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%dm ago" % minutes
    hours = minutes // 60
    if hours < 24:
        return "%dh ago" % hours
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return "%dd ago" % days
    weeks = days // 7
    return "%dw ago" % weeks


class Painted(Static):
    """A block whose content comes from a rich renderable.

    `source` is stored rather than the rendered text so a resize can re-render
    it. It is a callable rather than the renderable itself because the
    streaming answer's renderable changes on every tick, and holding a stale
    one would repaint the wrong thing on resize.
    """

    def __init__(self, source, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.source = source

    def repaint(self) -> None:
        width = self.content_size.width or self.size.width
        if width <= 0:
            # Not laid out yet. The resize that gives it a width will call back.
            return
        self.update(render.paint(self.source(), width))

    def on_resize(self) -> None:
        self.repaint()


class FrameRule(Static):
    """One horizontal edge of a turn's frame.

    Its own widget rather than a line inside the answer block, because the two
    edges have to survive things the answer does not: the top edge is painted
    before the first token arrives and must still be the right width after a
    resize that happens ten minutes later, and the bottom edge is painted after
    the answer is already bound to a finished string.

    Width is read at paint time from the widget's own box, which is the only
    number that is correct — a rule sized from the terminal ignores the
    transcript's margins and overshoots by exactly their sum, which is how a
    framed block ends up with edges wider than the text inside it.
    """

    def __init__(
        self,
        label: str = "",
        tone: str = "agent",
        top: bool = True,
        detail: str = "",
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self.label = label
        # Printed as written, beside the tracked label. A job's name is prose
        # somebody typed — "PR watch", "Andromeda repo daily summary" — and
        # letter-spacing prose turns it into `P R   W A T C H`, which is
        # unreadable at exactly the width where it matters. Tracking is for
        # short fixed labels; this is not one.
        self.detail = detail
        self.tone = tone
        self.top = top

    def _paint(self) -> Text:
        width = self.content_size.width or self.size.width or 0
        if width <= 0:
            return Text("")
        rule_style = screen_style(f"{self.tone}.rule")
        line = Text()
        if not self.top:
            line.append("└" + "─" * max(0, width - 2) + "┘", style=rule_style)
            return line
        if not self.label:
            line.append("┌" + "─" * max(0, width - 2) + "┐", style=rule_style)
            return line
        # `┌─ LABEL ─…─┐`. The label is tracked out like every other eyebrow on
        # the surface, and truncated rather than wrapped: a frame edge that
        # takes two lines stops reading as an edge.
        label = render.eyebrow(self.label)
        budget = max(0, width - 8)
        if len(label) > budget:
            label = label[:budget]
        line.append("┌─ ", style=rule_style)
        line.append(label, style=screen_style(self.tone))
        drawn = 3 + len(label)
        if self.detail:
            detail = self.detail
            room = max(0, width - drawn - 6)
            if len(detail) > room:
                detail = detail[: max(0, room - 1)] + "…"
            if detail:
                line.append("  " + detail, style=screen_style("muted"))
                drawn += 2 + len(detail)
        line.append(" ", style=rule_style)
        drawn += 1
        line.append("─" * max(0, width - drawn - 1) + "┐", style=rule_style)
        return line

    def repaint(self) -> None:
        self.update(self._paint())

    def on_resize(self) -> None:
        self.repaint()


class Transcript(VerticalGroup):
    """What has already happened, oldest first.

    Widgets rather than one growing text blob: a tool line and an answer have
    different styling, and a screen reader — or a future "copy this answer" —
    needs them to be separate things.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._answer: Painted | None = None
        self._answer_text = ""
        #: The frame currently open, as `(label, tone, detail)`, or `None`.
        #:
        #: One frame per turn — not one per text segment. The banner this
        #: replaces was printed on every segment, so a turn that called three
        #: tools produced four of them interleaved with the tool lines and the
        #: reader had no way to tell where one answer stopped. A frame is
        #: opened once by whatever speaks first and closed once when the turn
        #: ends; everything between is inside it.
        self._frame: tuple[str, str] | None = None

    # ---- rows -------------------------------------------------------------

    #: How far from the bottom still counts as "watching the live edge". Two
    #: rows, so a stray trackpad nudge does not detach the view, but a
    #: deliberate scroll up does.
    STICK_ROWS = 2

    @staticmethod
    def _at_live_edge(flow) -> bool:
        """Whether the reader is still following the newest row.

        The whole scroll behaviour turns on this. Pinning unconditionally means
        a person who scrolls up to read something gets yanked back down on the
        next token — which makes a streaming answer impossible to read while it
        arrives, and there is no way to ask it to stop.
        """
        try:
            furthest = flow.max_scroll_y
        except Exception:  # noqa: BLE001 - geometry is not worth a crash
            return True
        # Nothing to scroll: trivially at the edge.
        if furthest <= 0:
            return True
        return (furthest - flow.scroll_offset.y) <= Transcript.STICK_ROWS

    def scroll_end(self, animate: bool = False, force: bool = False) -> None:
        """Follow the newest row — unless the reader has scrolled away.

        `force` is for the cases where the person's own action is the reason
        the view moved: sending a message, opening a session. There, jumping to
        the bottom is the expected answer rather than an interruption.
        """

        def scroll_flow() -> None:
            flow = getattr(self.app, "conversation_scroll", None)
            if flow is None:
                return
            if not force and not self._at_live_edge(flow):
                return
            flow.scroll_end(animate=animate)

        # A mounted row does not affect the parent's virtual height until the
        # next layout. Scrolling synchronously lands at yesterday's bottom and
        # leaves the newest line below the composer.
        self.call_after_refresh(scroll_flow)

    def _append(self, widget: Static, force: bool = False) -> None:
        self.mount(widget)
        # `animate=False`: a moving target while text streams should stay
        # attached to the bottom, not chase it with overlapping animations.
        self.scroll_end(animate=False, force=force)

    # ---- frames -----------------------------------------------------------

    def ensure_frame(self, label: str, tone: str = "agent", detail: str = "") -> None:
        """Open this frame, closing a different one first.

        Callers name the frame they belong to on every append rather than
        opening and closing around a block. That is what makes an autonomous
        run arriving in the middle of a live turn come out right: the job's
        rows ask for the autonomous frame, the turn's next token asks for the
        agent frame again, and the transitions happen wherever they land
        instead of depending on two processes agreeing about ordering.
        """
        if self._frame == (label, tone, detail):
            return
        self.close_frame()
        self._frame = (label, tone, detail)
        rule = FrameRule(
            label=label, tone=tone, top=True, detail=detail, classes="row frame-top"
        )
        self._append(rule)
        # Mounted with no width yet; the layout pass that gives it one calls
        # `on_resize`. Asking now covers the case where it already has one.
        self.call_after_refresh(rule.repaint)

    def close_frame(self) -> None:
        """Draw the bottom edge, if a frame is open. Safe to call twice."""
        if self._frame is None:
            return
        self.end_answer()
        tone = self._frame[1]
        rule = FrameRule(tone=tone, top=False, classes="row frame-bottom")
        self._frame = None
        self._append(rule)
        self.call_after_refresh(rule.repaint)

    @property
    def frame_tone(self) -> str:
        return self._frame[1] if self._frame else "agent"

    def add_prompt(self, text: str) -> None:
        """What the person said. Outside the frame, and in the one other hue.

        The distinction is asymmetric on purpose: the agent's turn is framed
        and the person's is not, so a bare line is yours. What used to carry
        that was *dimness*, because the palette was three greys within six hex
        points and had nothing else to spend. It now has `render.YOU`, and a
        hue does in one glance what a brightness step could not do at all.
        """
        line = Text()
        line.append("› ", style=screen_style("you"))
        line.append(text, style=screen_style("you"))
        # Forced: the person just sent this. Wherever they had scrolled to, the
        # answer to their own message is what they want to see next.
        self._append(Static(line, classes="row prompt"), force=True)

    def add_queued_prompt(self, text: str) -> Static:
        """Something typed while the agent was still answering.

        It used to vanish: the composer cleared, the message went into a list,
        and the only trace was a small `2 queued` in the status bar. That reads
        as the input having been swallowed, and people retype it.

        Shown in place instead, dimmed and marked, so it is visibly *waiting*
        rather than gone. Replaced by the real prompt row when it is sent.
        """
        line = Text()
        line.append("› ", style="dim")
        line.append(text, style="dim")
        line.append("   queued", style="#a78bfa dim")
        row = Static(line, classes="row prompt queued")
        self._append(row, force=True)
        return row

    def add_note(self, text: str, tone: str = "muted") -> Static:
        """Append a note and return it so one-shot reveals can repaint it."""
        note = Static(
            Text(f"{self._indent()}{text}", style=screen_style(tone)),
            classes="row note",
        )
        self._append(note)
        return note

    def _indent(self) -> str:
        """Two columns inside an open frame, none outside it.

        Indentation is what makes the frame mean containment rather than
        decoration: rows that sit flush against the left edge read as a list
        the rule happens to sit above.
        """
        return "  " if self._frame else ""

    def add_tool(self, summary: str, tier: str) -> None:
        # Filled circle for anything that changes the machine, hollow for a
        # read. One glyph, and it means the same thing as the mark the REPL
        # prints — see `render.tool_call`.
        mark = "○" if tier == "safe_local" else "●"
        # The verb is split off the summary and set in its own column so a run
        # of tool calls lines up. `ToolSpec.summary()` is "<verb> <target>" for
        # every tool that has a target and a bare verb for the ones that do
        # not, which is why the split is bounded to one.
        parts = summary.split(" ", 1)
        verb = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        line = Text()
        line.append(f"{self._indent()}{mark} ", style=screen_style(self.frame_tone))
        line.append(verb.ljust(9)[:9] + " ", style=screen_style("lane"))
        line.append(rest, style=screen_style("muted"))
        self._append(Static(line, classes="row tool"))

    def add_tool_result(self, detail: str, ok: bool) -> None:
        if not detail:
            return
        # Under its call and indented past the glyph, so the eye reads a tool
        # and its outcome as one unit. A tick or a cross carries the outcome;
        # the text after it is free to be whatever the tool said.
        line = Text()
        line.append(f"{self._indent()}  ", style=screen_style("muted"))
        line.append("✓ " if ok else "✗ ", style=screen_style("good" if ok else "bad"))
        line.append(detail, style=screen_style("muted" if ok else "bad"))
        self._append(Static(line, classes="row tool-result"))

    def add_error(self, message: str, hint: str = "") -> None:
        block = Text()
        block.append(f"{self._indent()}✗ ", style=screen_style("bad"))
        block.append(message, style=screen_style("bad"))
        if hint:
            block.append(f"\n{self._indent()}  {hint}", style=screen_style("muted"))
        self._append(Static(block, classes="row error"))

    # ---- the streaming answer ---------------------------------------------

    def feed_answer(self, text: str) -> None:
        """Accumulate. Painting is the tick's job, not the delta's.

        The block is opened lazily, on the first text of a segment, rather than
        when the turn starts. Opening it eagerly puts an empty widget on screen
        *above* the tool lines that come next, so a tool's result row mounts
        below a block created before the tool ran — which is how a live run
        ended up showing `data.txt — 4 lines` underneath the answer that had
        already used it.
        """
        if self._answer is None:
            # No label. The frame carries it once for the whole turn; a label
            # per segment is the thing this replaced.
            self._answer = Painted(
                lambda: render.expand_charts(self._answer_text, streaming=True),
                classes="row answer",
            )
            self._answer_text = ""
            self._append(self._answer)
        self._answer_text += text

    def add_session_link(self, session_id: str, label: str = "") -> None:
        """A clickable way into another conversation, under the answer."""
        self._append(SessionLink(session_id, label, classes="row"))
        self.scroll_end(animate=False)

    def link_sessions_in(self, text: str) -> None:
        """Turn every `andromeda --resume <id>` the agent wrote into a row.

        Done on the finished answer rather than per delta, because a session id
        arrives a few characters at a time and half of one matches nothing.

        Matching the written command rather than a bare id is deliberate: a
        commit hash in a diff is also twelve hex characters, and turning one
        into "open this conversation" would be a link to nothing.
        """
        seen: set[str] = set()
        for found in SESSION_REFERENCE.finditer(text or ""):
            session_id = found.group(1)
            if session_id in seen:
                continue
            seen.add(session_id)
            self.add_session_link(session_id)

    def flush_answer(self, final: bool = False) -> None:
        if self._answer is None or not self._answer_text:
            return
        if final:
            # The last pass renders a still-open fence as the code block it
            # actually is: at the end of the turn it is content, not a chart
            # halfway through arriving.
            answer_text = self._answer_text
            # Bind the completed segment. Pointing every finished block back
            # at `self._answer_text` made an old response repaint with a later
            # response whenever the shared hero/chat flow resized.
            self._answer.source = lambda text=answer_text: render.expand_charts(text)
        self._answer.repaint()
        self.scroll_end(animate=False)

    def end_answer(self) -> None:
        """Close the current segment. Safe to call when there is none open.

        The one place a whole answer is known to be complete, which is why the
        session links are attached here: mid-stream, half an id matches nothing
        and the other half would attach a second row for the same session.
        """
        finished = self._answer_text
        self.flush_answer(final=True)
        self._answer = None
        if finished:
            self.link_sessions_in(finished)

    @property
    def answer_text(self) -> str:
        return self._answer_text


class StudyPanel(VerticalScroll):
    """Leonardo's study, isolated from the conversation scrollback."""

    def add_row(self, text: str, tone: str = "muted") -> Static:
        row = Static(Text(text, style=screen_style(tone)), classes="study-row")
        self.mount(row)
        return row


class SessionsRail(Static):
    """Past conversations, grouped by what each one left running.

    Replaces the changelog that used to sit here. The reasoning it overturns is
    recorded rather than deleted: `set_sessions` was made a no-op on the
    grounds that "session history belongs behind `/sessions`". That was right
    when the rail's job was to announce product changes, and wrong once the
    product grew work that outlives the conversation which started it. A
    scheduled job is invisible by construction — it runs when nobody is
    looking — so the surface a person sees on every launch is the only place
    that fact reliably reaches them.

    **The three groups are derived, never stored.** `Schedule.session_kinds`
    reads the live job store on each refresh, so a deleted job stops labelling
    its session immediately. A tag written onto the session at creation time
    would keep claiming a job that no longer exists, and nothing would ever
    correct it.
    """

    #: Tab order. `all` first because it answers "where was that conversation",
    #: which is the common question; `agent` answers "what is running", which
    #: is rarer and more specific.
    #:
    #: Local and cloud used to be two tabs. They stopped being two things the
    #: moment a job's placement became a per-fire decision: the same job is
    #: local this morning and hosted tonight, so splitting the *rail* by it
    #: made a conversation move between tabs for reasons nobody could see. One
    #: tab, and the badge on each row says where that one is now.
    TABS = (("all", "ALL"), ("agent", "AGENT"))

    #: How many rows fit without pushing the runtime block off the rail.
    VISIBLE = 4

    #: One cell each, so a badge never changes a row's width.
    BADGES = {"local": "⌂", "cloud": "☁"}

    class Opened(Message):
        """A session the person picked. Handled by the app, not here.

        The widget deliberately cannot resume a session itself: that replaces
        the live conversation, which is the app's business and needs the same
        path `/resume` uses.
        """

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class Deleted(Message):
        """A session the person asked to remove, after confirming.

        Posted rather than deleted here for the same reason `Opened` is: the
        live conversation might *be* this session, and only the app knows that.
        A rail that unlinked the file itself would leave the app writing to a
        path that no longer exists and the loss would surface an hour later as
        a save that silently recreated it.
        """

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.provider = ""
        self.model = ""
        self.workspace = ""
        self.tools = 0
        self.approval = ""
        self.thinking = ""
        self.tab = 0
        self.cursor = 0
        #: First visible row. NOT named `offset` — Textual's `Widget`
        #: already owns that name as a `ScalarOffset`, and assigning an
        #: int to it raises before the rail ever paints.
        self.window_top = 0
        self.rows: list[tuple[str, str, str, str]] = []
        self._row_lines: dict[int, int] = {}
        #: The row whose ✕ has been clicked once, by session id.
        #:
        #: Two clicks rather than a modal. A modal is the right answer when the
        #: thing being destroyed is expensive to rebuild; a transcript is not,
        #: and a dialog for every one turns tidying up forty sessions into
        #: eighty keystrokes. The row says `sure?` in the delete hue between
        #: the clicks, so the second one is never a surprise.
        self._confirming = ""
        #: Where each row's ✕ sits, by painted line, for hit-testing.
        self._delete_spans: dict[int, tuple[int, int]] = {}
        #: Which painted line the tab strip landed on. Set during paint
        #: rather than hardcoded, so adding a header line cannot silently
        #: move the tabs out from under the click handler.
        self._tab_line = 2
        self.reload()

    # -- data ---------------------------------------------------------------

    def reload(self) -> None:
        """Re-read sessions and the job store.

        Best-effort by construction. This runs on the launch path, and a
        corrupt job store or an unreadable session directory must degrade to an
        empty rail rather than stop the surface opening — the rail is furniture
        and the composer below it is the product.
        """
        try:
            from andromeda_cli import sessions as store

            found = store.recent(limit=40)
        except Exception:  # noqa: BLE001 - the rail is never worth a crash
            found = []

        try:
            from andromeda_agent.schedule import Schedule
            from andromeda_cli.session import schedule_path

            kinds = Schedule(schedule_path()).session_kinds()
        except Exception:  # noqa: BLE001 - no jobs and a broken store look alike here
            kinds = {}

        self.rows = [
            (
                session.id,
                session.title,
                _relative_age(session.updated_at),
                kinds.get(session.id, ""),
            )
            for session in found
        ]
        self._clamp()

    def _visible_rows(self) -> list[tuple[str, str, str, str]]:
        """ALL is *your* conversations. AGENT is the ones a job produced.

        The two are disjoint on purpose. A job that runs every five minutes
        produces a session that keeps bumping to the top of the list, and with
        both mixed together the conversations somebody actually had get pushed
        off the rail by machine output. ALL means "things I said".
        """
        kind = self.TABS[self.tab][0]
        if kind == "all":
            return [row for row in self.rows if not row[3]]
        # Any badge at all means a job produced this session. Which badge is
        # the row's business, not the tab's.
        return [row for row in self.rows if row[3]]

    def _clamp(self) -> None:
        """Keep the cursor on a real row and the window around the cursor.

        Called after every move *and* after a reload, because a reload can
        shorten the list under a cursor that was valid a moment ago — a job
        removed in another terminal is enough.
        """
        total = len(self._visible_rows())
        if total == 0:
            self.cursor = 0
            self.window_top = 0
            return
        capacity = self._capacity()
        self.cursor = max(0, min(self.cursor, total - 1))
        self.window_top = max(0, min(self.window_top, max(0, total - capacity)))
        if self.cursor < self.window_top:
            self.window_top = self.cursor
        elif self.cursor >= self.window_top + capacity:
            self.window_top = self.cursor - capacity + 1

    # -- interaction --------------------------------------------------------

    def switch_tab(self, delta: int) -> None:
        """Move between ALL / AGENT, wrapping.

        The cursor resets to the top rather than carrying across: row 3 of ALL
        and row 3 of CLOUD are unrelated conversations, and a cursor that
        appears to stay put while pointing at something else is worse than one
        that visibly moves.
        """
        self.tab = (self.tab + delta) % len(self.TABS)
        self.cursor = 0
        self.window_top = 0
        self._confirming = ""
        self._refresh()

    def move(self, delta: int) -> None:
        # Moving off a row disarms its ✕, for the same reason clicking
        # elsewhere does: a confirmation that outlives the cursor is a
        # confirmation nobody remembers giving.
        self._confirming = ""
        self.cursor += delta
        self._clamp()
        self._refresh()

    def open_selected(self) -> None:
        rows = self._visible_rows()
        if 0 <= self.cursor < len(rows):
            self.post_message(self.Opened(rows[self.cursor][0]))

    def on_mouse_scroll_down(self, event) -> None:
        """The wheel scrolls the list.

        The gesture that needs no teaching, and the reason the chorded keys are
        a convenience rather than the only way in. Stopped from propagating so
        the conversation behind the rail does not scroll at the same time.
        """
        event.stop()
        self.move(1)

    def on_mouse_scroll_up(self, event) -> None:
        event.stop()
        self.move(-1)

    def _content_offset(self, event) -> tuple[int, int] | None:
        """Where a click landed in painted-content coordinates.

        A click's offset is relative to the widget's outer box, and this widget
        has CSS padding — one line at the top, two columns at the left. Reading
        the raw offset therefore hit-tested every row one line too high and
        every tab two columns too far right, which is exactly how it behaved:
        the tab strip was unreachable and a click on a row opened its
        neighbour. Padding is read from the resolved style rather than repeated
        as a constant so a stylesheet change cannot silently undo this.
        """
        offset = getattr(event, "offset", None)
        if offset is None:
            return None
        padding = self.styles.padding
        return offset.x - padding.left, offset.y - padding.top

    def on_click(self, event) -> None:
        """Click a tab or a row.

        Mouse support matters more here than elsewhere on the surface: the
        composer holds the keyboard, so every key this rail wants is a chord
        somebody has to learn. A click is the one gesture that needs none.
        """
        where = self._content_offset(event)
        if where is None:
            return
        column, line = where

        if line == self._tab_line:
            for index in range(len(self.TABS)):
                begins, ends = self._tab_span(index)
                if begins <= column < ends:
                    if index != self.tab:
                        self.tab = index
                        self.cursor = 0
                        self.window_top = 0
                        self._refresh()
                    return
            return

        row_index = self._row_lines.get(line)
        if row_index is None:
            return

        rows = self._visible_rows()
        if not 0 <= row_index < len(rows):
            return
        session_id = rows[row_index][0]

        # The ✕ is tested before the row, because it is inside the row. Testing
        # the row first would open the session on the way to deleting it.
        begins, ends = self._delete_spans.get(line, (-1, -1))
        if begins <= column < ends:
            self.cursor = row_index
            if self._confirming == session_id:
                self._confirming = ""
                self.post_message(self.Deleted(session_id))
            else:
                self._confirming = session_id
            self._clamp()
            self._refresh()
            return

        # A click anywhere else on any row clears a pending confirmation. An
        # armed ✕ that survives you looking elsewhere is an armed ✕ that fires
        # on a click you had forgotten was the second one.
        self._confirming = ""
        self.cursor = row_index
        self._clamp()
        self._refresh()
        self.open_selected()

    def delete_selected(self) -> None:
        """Keyboard path to the same act. Confirms exactly as the ✕ does."""
        rows = self._visible_rows()
        if not 0 <= self.cursor < len(rows):
            return
        session_id = rows[self.cursor][0]
        if self._confirming == session_id:
            self._confirming = ""
            self.post_message(self.Deleted(session_id))
        else:
            self._confirming = session_id
        self._refresh()

    def forget(self, session_id: str) -> None:
        """Drop a deleted row without a full re-read of the session directory.

        Called by the app once the file is actually gone. `reload()` would also
        work and is what runs on the next natural refresh; this makes the row
        disappear on the same frame as the click, which is the difference
        between a button that works and a button you press twice.
        """
        self.rows = [row for row in self.rows if row[0] != session_id]
        self._confirming = ""
        self._clamp()
        self._refresh()

    def _tab_span(self, index: int) -> tuple[int, int]:
        """Where a tab's label sits, for hit-testing a click.

        Computed from the same widths `_refresh` paints with rather than
        measured off rendered output: a style can change a cell's appearance
        without changing its position, and hit-testing the render would drift
        the first time one did.
        """
        column = 0
        for position, (_, label) in enumerate(self.TABS):
            width = len(label) + 4  # "[ LABEL ]" and "  LABEL  " are equal width
            if position == index:
                return column, column + width
            column += width
        return column, column

    # -- painting -----------------------------------------------------------

    def configure(
        self,
        *,
        provider: str,
        model: str,
        workspace: str,
        tools: int,
        approval: str,
        thinking: str,
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.tools = tools
        self.approval = approval
        self.thinking = thinking
        self._refresh()

    def set_sessions(self, sessions: list[tuple[str, str, str]]) -> None:
        """Kept for callers that pushed rows in. The rail reads its own now."""
        self.reload()
        self._refresh()

    def push(self, label: str, detail: str, tone: str = "muted") -> None:
        """Compatibility no-op: conversation activity is not session history."""

    def _capacity(self) -> int:
        """How many rows fit, from the height the rail actually has.

        Measured rather than fixed. The constant it replaces showed four rows
        on a full-height terminal that had room for a dozen, which turned
        browsing forty sessions into forty keystrokes.
        """
        height = getattr(self.content_size, "height", 0) or 0
        if height <= 0:
            return self.VISIBLE
        # Header is four lines, the hint two, and the runtime block five.
        room = height - 11
        return max(3, min(room, 14))

    def _refresh(self) -> None:
        out = Text()
        self._row_lines = {}
        self._delete_spans = {}
        line = 0

        def newline(count: int = 1) -> None:
            nonlocal line
            out.append("\n" * count)
            line += count

        out.append("SYS. 001", style=screen_style("muted"))
        out.append("  " + "─" * 14 + "  ", style=screen_style("rule"))
        out.append("SESSIONS", style=screen_style("muted"))

        newline(2)
        self._tab_line = line
        for index, (_, label) in enumerate(self.TABS):
            if index == self.tab:
                out.append("[ " + label + " ]", style=screen_style("accent"))
            else:
                out.append("  " + label + "  ", style=screen_style("muted"))

        rows = self._visible_rows()
        capacity = self._capacity()
        if not rows:
            newline(2)
            out.append(self._empty_line(), style=screen_style("muted"))
        else:
            self.window_top = max(0, min(self.window_top, max(0, len(rows) - capacity)))
            if self.cursor < self.window_top:
                self.window_top = self.cursor
            elif self.cursor >= self.window_top + capacity:
                self.window_top = self.cursor - capacity + 1

            newline(2)
            window = rows[self.window_top : self.window_top + capacity]
            for position, (session_id, title, age, kind) in enumerate(window):
                index = self.window_top + position
                if position:
                    newline()
                # One line per row, not three. Forty sessions is a list to
                # browse, and a layout that spends three lines on each turns
                # the rail into a peephole.
                self._row_lines[line] = index
                selected = index == self.cursor
                confirming = session_id and session_id == self._confirming
                badge = self.BADGES.get(kind, " ")
                # The delete affordance is the last cells of the row and is
                # budgeted for whether or not it is being confirmed, so a row
                # never changes width when it enters the confirm state — a
                # list that reflows under the cursor is a list you misclick.
                mark = "sure? ✕" if confirming else "      ✕"
                tail = f"{age:>9} {badge}"
                width = max(12, (self.content_size.width or 46) - len(tail) - len(mark) - 8)
                label = title[: width - 1] + "…" if len(title) > width else title
                out.append(
                    ("▸ " if selected else "  ") + f"{index + 1:02d}  ",
                    style=screen_style("accent" if selected else "muted"),
                )
                out.append(
                    label.ljust(width),
                    style=render.ZINC_50 if selected else screen_style("muted"),
                )
                out.append(tail, style=screen_style("muted"))
                # Measured off what has actually been appended, so the span is
                # the ✕'s real column range rather than an assumption about the
                # layout. A hit-test computed from widths drifts the first time
                # a badge or a date format changes; this cannot.
                column = len(out.plain.rsplit("\n", 1)[-1])
                out.append(
                    " " + mark,
                    style=screen_style("bad" if confirming else "muted"),
                )
                self._delete_spans[line] = (column, len(out.plain.rsplit("\n", 1)[-1]))

            newline(2)
            if len(rows) > capacity:
                out.append(
                    "  %d of %d   ↑↓ scroll · ⏎ open · ✕ delete"
                    % (self.cursor + 1, len(rows)),
                    style=screen_style("muted"),
                )
            else:
                out.append(
                    "  ⏎ open · ←→ switch · ✕ delete", style=screen_style("muted")
                )

        if self.provider:
            provider = self.provider.split(" (", 1)[0].upper()
            model = self.model.rsplit("/", 1)[-1].upper()
            newline(2)
            out.append("─" * 36, style=screen_style("rule"))
            newline()
            out.append("RUNTIME", style=screen_style("eyebrow"))
            newline()
            out.append("%s / %s" % (provider, model), style=render.ZINC_100)
            newline()
            out.append(
                "TOOLS / %d   APPROVAL / %s" % (self.tools, self.approval.upper()),
                style=screen_style("muted"),
            )
            if self.thinking and self.thinking != "off":
                out.append(
                    "   THINKING / %s" % self.thinking.upper(),
                    style=screen_style("muted"),
                )

        self.update(out)

    def _empty_line(self) -> str:
        """What an empty group says.

        Each group gets its own sentence rather than one shared "nothing here".
        An empty AGENT tab is not a missing feature and must not read like one:
        it means no job has produced a conversation yet, which is the ordinary
        state and worth saying plainly.
        """
        kind = self.TABS[self.tab][0]
        if kind == "all":
            waiting = sum(1 for row in self.rows if row[3])
            if waiting:
                return "  NONE YET — %d IN AGENT" % waiting
            return "  NO SAVED SESSIONS YET"
        return "  NO AGENT JOBS YET"


#: The rail changed from a changelog to a session browser. The old name stays
#: pointing at the new class so an unmigrated import cannot be the reason the
#: surface fails to open.
RecentUpdates = SessionsRail


class ActivityLane(Static):
    """What is happening right now, on one line.

    Separate from the transcript on purpose. A tool that takes forty seconds
    should not push the answer off the screen, and "still working" is a fact
    about now rather than a thing that happened.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.tool = ""
        self.started = 0.0
        self.lanes: list[str] = []
        self.waiting = False
        self._frame = 0

    def start_tool(self, summary: str) -> None:
        self.tool = summary
        self.started = time.monotonic()

    def stop_tool(self) -> None:
        self.tool = ""

    def tick(self, busy: bool) -> None:
        if not busy and not self.lanes:
            self.display = False
            return
        self.display = True
        self._frame = (self._frame + 1) % len(SPINNER)

        if self.waiting:
            # A spinner while a prompt is open says the machine is working. It
            # is not: it is stopped, waiting for the person, and saying so is
            # the difference between "be patient" and "you are the hold-up".
            self.update(
                Text("PAUSED / WAITING FOR YOUR ANSWER", style=screen_style("warn"))
            )
            return

        line = Text()
        line.append(SPINNER[self._frame] if busy else "·", style=render.ZINC_50)
        if self.tool:
            elapsed = time.monotonic() - self.started
            line.append("  WORKING / ", style=screen_style("muted"))
            line.append(self.tool[:90], style=render.ZINC_100)
            # Only once it is long enough to wonder about. A timer that starts
            # at 0.0s on every read makes every call look slow.
            if elapsed >= 2:
                line.append(f"  {elapsed:.0f}s", style=screen_style("muted"))
        elif busy:
            line.append("  THINKING", style=screen_style("muted"))
        if self.lanes:
            line.append(f"   LANES / {len(self.lanes)}  ", style=screen_style("muted"))
            line.append(", ".join(self.lanes), style=render.ZINC_100)
        self.update(line)


class StatusBar(Static):
    """The standing facts: where you are, what may run, how full the window is."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.model = ""
        self.mode = ""
        self.thinking = ""
        self.context = 0.0
        self.hint = ""

    def refresh_status(self) -> None:
        line = Text()
        model = self.model.rsplit("/", 1)[-1]
        line.append(f" MODEL / {model.upper()}", style=screen_style("muted"))
        line.append(f"   {self.mode.upper().replace(':', ' / ')}", style=screen_style("muted"))
        if self.thinking and self.thinking != "off":
            line.append(f"   THINKING / {self.thinking.upper()}", style=screen_style("muted"))
        # The context gauge appears only once it is worth reading. Below a
        # third of the window it is a bar that is always empty, which teaches
        # nobody anything — the same threshold the REPL prompt uses.
        if self.context >= 0.33:
            line.append(
                f"  {render.context_meter(self.context, width=8)} {int(self.context * 100)}%",
                style=screen_style("muted"),
            )
        if self.hint:
            line.append(f"   {self.hint.upper()}", style=screen_style("muted"))
        self.update(line)


class Composer(TextArea):
    """The input line — a real multi-line editor, not a one-line field.

    It was a `textual.widgets.Input`, and that was the wrong widget. `Input` is
    single-line, so its paste handler is literally:

        line = event.text.splitlines()[0]
        ... insert line ...
        event.stop()

    Paste twenty lines and nineteen are gone, silently. Two rounds of working
    around that — intercepting the paste, staging the text elsewhere, showing a
    banner about it — fixed the symptom and left the field still unable to hold
    what people put in it. The field should just take the text.

    `TextArea` does, natively, whatever the terminal sends: a bracketed paste
    inserts every line, and typed newlines insert newlines. Nothing to
    intercept and nothing to stage.

    What has to be reinstated is Enter: `TextArea` treats it as a newline,
    while a chat composer needs it to send. So Enter submits and **shift+enter
    inserts a line break** — with `alt+enter` and `ctrl+j` as fallbacks,
    because a number of terminals do not distinguish shift+enter at all.
    """

    # One line to start, growing with the text and then scrolling. A composer
    # that expands without limit pushes the conversation off the screen, which
    # is the thing you are writing about.
    MIN_LINES = 1
    MAX_LINES = 10

    NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")

    class Submitted(Message):
        """Enter was pressed. Carries everything in the field."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(placeholder=placeholder, soft_wrap=True, **kwargs)
        self.show_line_numbers = False
        # Set by the app once both are mounted. `None` until then, and on any
        # surface that does not want one — the composer works the same without.
        self.palette: "CommandPalette | None" = None

    def _palette_open(self) -> bool:
        return self.palette is not None and self.palette.open

    def _accept(self) -> None:
        """Put the highlighted command in the field, ready for its arguments.

        A trailing space, because most of these take one — `/resume 3`,
        `/approve <id>` — and it also closes the list, since a line with a
        space in it is no longer a bare command.
        """
        chosen = self.palette.chosen if self.palette else None
        if chosen is None:
            return
        self.palette.close()
        self.text = f"{chosen.display} "
        self.move_cursor(self.document.end)
        self.resize_to_content()

    async def _on_key(self, event) -> None:
        """Enter sends; shift+enter breaks the line.

        `prevent_default`, never `super()`: Textual dispatches `_on_key` for
        every class in the MRO, so the base handler runs unless it is
        explicitly suppressed. Everything this does not name falls through to
        it untouched, which is how the editor keeps all its normal editing.

        While the palette is open it takes the navigation keys first, and gives
        every one of them back the moment it closes. That ordering is the whole
        contract: up and down move the highlight rather than the cursor only
        while there is a highlight to move.
        """
        if self._palette_open():
            if event.key in ("down", "ctrl+n"):
                event.prevent_default()
                event.stop()
                self.palette.move(1)
                return
            if event.key in ("up", "ctrl+p"):
                event.prevent_default()
                event.stop()
                self.palette.move(-1)
                return
            if event.key in ("pagedown", "ctrl+f"):
                event.prevent_default()
                event.stop()
                self.palette.page(1)
                return
            if event.key in ("pageup", "ctrl+b"):
                event.prevent_default()
                event.stop()
                self.palette.page(-1)
                return
            if event.key == "home":
                event.prevent_default()
                event.stop()
                self.palette.jump(end=False)
                return
            if event.key == "end":
                event.prevent_default()
                event.stop()
                self.palette.jump(end=True)
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self.palette.close()
                return
            if event.key == "tab":
                event.prevent_default()
                event.stop()
                self._accept()
                return
            if event.key == "enter":
                # Enter completes rather than sends, because a highlighted row
                # is a choice in progress. Sending `/mc` because somebody was
                # looking at `/mcp` would be the surface ignoring what it was
                # visibly showing them.
                event.prevent_default()
                event.stop()
                self._accept()
                return

        if event.key in self.NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))

    def resize_to_content(self) -> None:
        lines = max(self.MIN_LINES, min(len(self.text.splitlines()) or 1, self.MAX_LINES))
        self.styles.height = lines

    def clear_text(self) -> None:
        self.text = ""
        self.resize_to_content()


class CommandPalette(Static):
    """The list that opens under the composer when a line starts with `/`.

    The REPL gets this from `prompt_toolkit` for free. This surface owns its own
    terminal, so it has to be drawn — and it is worth drawing, because the
    full-screen interface is the default and typing `/` in it did nothing at
    all. The help line under the composer has been promising `/ COMMANDS` to
    people the whole time there was nothing behind it.

    Rows come from `vocabulary`, the same registry `/help` and the REPL's
    completer read, so all three cannot disagree.
    """

    # More than fits comfortably above a composer, and the point of the list is
    # to be scanned rather than read. Filtering is what narrows it.
    VISIBLE = 12
    # How far page-up/page-down move. A screen minus one row, so the line you
    # were reading is still on screen after the jump and you do not lose your
    # place in a fifty-item list.
    PAGE = VISIBLE - 1

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.rows: list = []
        self.index = 0
        self.display = False

    @property
    def open(self) -> bool:
        return bool(self.display and self.rows)

    @property
    def chosen(self):
        """The highlighted row, or None when the list is closed or empty."""
        if not self.open:
            return None
        return self.rows[max(0, min(self.index, len(self.rows) - 1))]

    def sync(self, text: str) -> None:
        """Open, filter or close, from whatever is in the composer.

        Only a line that *begins* with `/` and is still one word. A message that
        merely contains a slash is a message, and popping a list over somebody
        writing `and/or` would make the field feel broken.
        """
        from andromeda_cli import vocabulary

        first = (text or "").split("\n")[0]
        if not first.startswith("/") or " " in first:
            self.close()
            return

        rows = vocabulary.matching(first)
        if not rows:
            # An empty box is worse than none: it reads as the surface having
            # frozen rather than as "nothing matches".
            self.close()
            return

        # Held rather than reset, so narrowing a list does not jump the
        # highlight back to the top under somebody's fingers — but clamped, so
        # it cannot point past the end of a shorter list.
        self.rows = rows
        self.index = max(0, min(self.index, len(rows) - 1))
        self.display = True
        self._paint()

    def close(self) -> None:
        self.rows = []
        self.index = 0
        self.display = False

    def move(self, delta: int) -> None:
        """Up and down, wrapping.

        Wrapping because the list is short and the alternative is a keypress
        that silently does nothing at the ends.
        """
        if not self.open:
            return
        self.index = (self.index + delta) % len(self.rows)
        self._paint()

    def page(self, delta: int) -> None:
        """A screenful at a time, clamped rather than wrapped.

        Wrapping a page jump is disorienting in a way wrapping one row is not:
        pressing page-down near the end should land on the end, not back at the
        top of a list you were working your way through.
        """
        if not self.open:
            return
        self.index = max(0, min(self.index + delta * self.PAGE, len(self.rows) - 1))
        self._paint()

    def jump(self, end: bool) -> None:
        """To the first or last row."""
        if not self.open:
            return
        self.index = len(self.rows) - 1 if end else 0
        self._paint()

    # Mouse wheel. The list is taller than it looks and people reach for the
    # wheel before they reach for the arrow keys.
    def on_mouse_scroll_down(self, event) -> None:
        if self.open:
            event.stop()
            self.move(1)

    def on_mouse_scroll_up(self, event) -> None:
        if self.open:
            event.stop()
            self.move(-1)

    def _window(self) -> tuple[list, int]:
        """The visible slice, scrolled to keep the highlight inside it."""
        if len(self.rows) <= self.VISIBLE:
            return self.rows, self.index
        start = min(
            max(0, self.index - self.VISIBLE // 2), len(self.rows) - self.VISIBLE
        )
        return self.rows[start : start + self.VISIBLE], self.index - start

    def _paint(self) -> None:
        window, highlight = self._window()
        width = max((len(row.name) for row in window), default=8)

        text = Text()
        for position, row in enumerate(window):
            selected = position == highlight
            text.append("▸ " if selected else "  ", style="#a78bfa" if selected else "")
            text.append(
                f"/{row.name.ljust(width)}",
                style="bold #fafafa" if selected else "#e4e4e7",
            )
            # The description is the whole reason this is a palette rather than
            # a completion: a list of bare words is one you still have to go
            # and look up.
            text.append(f"  {row.summary}", style="#a1a1aa" if selected else "dim")
            if position != len(window) - 1:
                text.append("\n")

        # Which way there is more, and how much. A bare "… 21 more" under a
        # list you have scrolled into the middle of is wrong in both
        # directions, and reads as though the top of the list is all there is.
        above = self.index - highlight
        below = len(self.rows) - above - len(window)
        if above or below:
            marks = []
            if above:
                marks.append(f"↑ {above}")
            if below:
                marks.append(f"↓ {below}")
            text.append(
                f"\n  {'  '.join(marks)}   [{self.index + 1}/{len(self.rows)}]",
                style="dim",
            )

        self.update(text)


#: A session id as it appears in text the agent wrote. Twelve hex characters is
#: what `sessions.Session` mints, and the `--resume` in front of it is what
#: makes this a link rather than a coincidence — a bare hex run in a diff or a
#: commit hash must not become a clickable row.
SESSION_REFERENCE = re.compile(r"andromeda\s+--resume\s+([0-9a-f]{6,32})\b")


class SessionLink(Static):
    """A row you click to open a conversation.

    Printing `andromeda --resume 78d4aa057c95` and expecting somebody to select
    it, copy it, leave the app and paste it into a shell is asking a person to
    be a terminal. The id is already on screen and the surface can already
    resume a session — the only missing piece was making the thing clickable.

    It reuses `SessionsRail.Opened`, so a click here goes through exactly the
    path the rail and `/resume` use: refused mid-turn, refused for the current
    session, and never resuming from inside a widget.
    """

    def __init__(self, session_id: str, label: str = "", **kwargs) -> None:
        self.session_id = session_id
        self.label = label or "Open the conversation"
        super().__init__(self._painted(), **kwargs)

    def _painted(self) -> Text:
        text = Text()
        text.append("  ↳ ", style="#a78bfa")
        text.append(self.label, style="#fafafa underline")
        text.append(f"  {self.session_id}", style="dim")
        return text

    def on_click(self) -> None:
        self.post_message(SessionsRail.Opened(self.session_id))
