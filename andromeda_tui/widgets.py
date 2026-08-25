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
    "ok": render.ZINC_50,
    "warn": f"bold {render.ZINC_50}",
    "bad": f"bold {render.ZINC_50}",
    # Legacy tone names still arrive from slash commands and tool events. They
    # are deliberately aliases, not an escape hatch back to terminal colours.
    "red": f"bold {render.ZINC_50}",
    "yellow": render.ZINC_100,
    "green": render.ZINC_50,
    "magenta": render.ZINC_100,
    "cyan": render.ZINC_50,
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


class Painted(Static):
    """A block whose content comes from a rich renderable.

    `source` is stored rather than the rendered text so a resize can re-render
    it. It is a callable rather than the renderable itself because the
    streaming answer's renderable changes on every tick, and holding a stale
    one would repaint the wrong thing on resize.
    """

    def __init__(
        self,
        source,
        *,
        content_style: str = "",
        response_frame: bool = False,
        **kwargs,
    ) -> None:
        super().__init__("", **kwargs)
        self.source = source
        self.content_style = content_style
        self.response_frame = response_frame

    def repaint(self) -> None:
        width = self.content_size.width or self.size.width
        if width <= 0:
            # Not laid out yet. The resize that gives it a width will call back.
            return
        painted = render.paint(self.source(), width)
        if self.content_style:
            # Appending one foreground span preserves markdown's weight,
            # emphasis and underline while giving the whole response a stable
            # shade distinct from the brighter user prompt.
            painted.stylize(self.content_style)
        if self.response_frame:
            # Literal brackets are part of the requested grammar, not a label.
            # Build them from the measured content width so both rules keep
            # spanning the row after any terminal resize.
            rule_text = "[" + "─" * max(0, width - 2) + "]"
            painted = Text.assemble(
                Text(rule_text, style=screen_style("rule")),
                "\n",
                painted,
                "\n",
                Text(rule_text, style=screen_style("rule")),
            )
        self.update(painted)

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

    # ---- rows -------------------------------------------------------------

    def scroll_end(self, animate: bool = False) -> None:
        """Keep the shared hero/conversation flow pinned to its newest row."""

        def scroll_flow() -> None:
            flow = getattr(self.app, "conversation_scroll", None)
            if flow is not None:
                flow.scroll_end(animate=animate)

        # A mounted row does not affect the parent's virtual height until the
        # next layout. Scrolling synchronously lands at yesterday's bottom and
        # leaves the newest line below the composer.
        self.call_after_refresh(scroll_flow)

    def _append(self, widget: Static) -> None:
        self.mount(widget)
        # `animate=False`: a moving target while text streams should stay
        # attached to the bottom, not chase it with overlapping animations.
        self.scroll_end(animate=False)

    def add_prompt(self, text: str) -> None:
        self._append(
            Static(Text(text, style=render.ZINC_50), classes="row prompt")
        )

    def add_note(self, text: str, tone: str = "muted") -> Static:
        """Append a note and return it so one-shot reveals can repaint it."""
        note = Static(Text(f"  {text}", style=screen_style(tone)), classes="row note")
        self._append(note)
        return note

    def add_tool(self, summary: str, tier: str) -> None:
        # Filled circle for anything that changes the machine, hollow for a
        # read. One glyph, and it means the same thing as the mark the REPL
        # prints — see `render.tool_call`.
        mark = "○" if tier == "safe_local" else "●"
        line = Text()
        line.append(f"{mark}  TOOL / ", style=screen_style("muted"))
        line.append(summary, style=render.ZINC_100)
        self._append(Static(line, classes="row tool"))

    def add_tool_result(self, detail: str, ok: bool) -> None:
        if not detail:
            return
        self._append(
            Static(
                Text(
                    f"   {detail}",
                    style=screen_style("muted" if ok else "warn"),
                ),
                classes="row tool-result",
            )
        )

    def add_error(self, message: str, hint: str = "") -> None:
        block = Text()
        block.append(
            f"{render.eyebrow('system')}  /  ERROR\n",
            style=screen_style("eyebrow"),
        )
        block.append(message, style=render.ZINC_50)
        if hint:
            block.append(f"\n{hint}", style=screen_style("muted"))
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
            self._answer = Painted(
                lambda: render.expand_charts(
                    self._answer_text,
                    streaming=True,
                ),
                content_style=render.ZINC_200,
                response_frame=True,
                classes="row answer",
            )
            self._answer_text = ""
            self._append(self._answer)
        self._answer_text += text

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
        """Close the current segment. Safe to call when there is none open."""
        self.flush_answer(final=True)
        self._answer = None

    @property
    def answer_text(self) -> str:
        return self._answer_text


class StudyPanel(VerticalScroll):
    """Leonardo's study, isolated from the conversation scrollback."""

    def add_row(self, text: str, tone: str = "muted") -> Static:
        row = Static(Text(text, style=screen_style(tone)), classes="study-row")
        self.mount(row)
        return row


class RecentUpdates(Static):
    """Actual CLI release changes plus the current runtime, never chat events."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.provider = ""
        self.model = ""
        self.workspace = ""
        self.tools = 0
        self.approval = ""
        self.thinking = ""
        self.changes = latest_cli_changes()

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
        """Compatibility no-op: session history belongs behind `/sessions`."""

    def push(self, label: str, detail: str, tone: str = "muted") -> None:
        """Compatibility no-op: conversation activity is not a CLI update."""

    def _refresh(self) -> None:
        out = Text()
        out.append("SYS. 001", style=screen_style("muted"))
        out.append("  " + "─" * 14 + "  ", style=screen_style("rule"))
        out.append("RECENT CLI CHANGES", style=screen_style("muted"))

        if self.changes:
            for index, (label, detail) in enumerate(self.changes, start=1):
                out.append(f"\n\n{index:02d} / {label}", style=screen_style("eyebrow"))
                out.append(f"\n{detail}", style=render.ZINC_100)
        else:
            out.append("\n\n00 / CHANGELOG", style=screen_style("eyebrow"))
            out.append("\nNO RELEASE NOTES FOUND", style=render.ZINC_100)

        if self.provider:
            provider = self.provider.split(" (", 1)[0].upper()
            model = self.model.rsplit("/", 1)[-1].upper()
            out.append("\n\n" + "─" * 36, style=screen_style("rule"))
            out.append("\nRUNTIME", style=screen_style("eyebrow"))
            out.append(f"\n{provider} / {model}", style=render.ZINC_100)
            out.append(
                f"\nTOOLS / {self.tools}   APPROVAL / {self.approval.upper()}",
                style=screen_style("muted"),
            )
            if self.thinking and self.thinking != "off":
                out.append(
                    f"   THINKING / {self.thinking.upper()}",
                    style=screen_style("muted"),
                )

        self.update(out)


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

    async def _on_key(self, event) -> None:
        """Enter sends; shift+enter breaks the line.

        `prevent_default`, never `super()`: Textual dispatches `_on_key` for
        every class in the MRO, so the base handler runs unless it is
        explicitly suppressed. Everything this does not name falls through to
        it untouched, which is how the editor keeps all its normal editing.
        """
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
