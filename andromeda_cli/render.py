"""How the terminal looks.

Two rules behind everything here.

**Structure survives the trip.** A model's answer is markdown — headings,
bold, tables, code. Printing it raw makes the reader parse `**` and `|---|`
themselves, which is exactly the "clumps of text" problem. It is rendered.

**A tty is not a pipe.** Rendered output is for a person watching. When stdout
is redirected, everything falls back to plain text with no escape codes,
because `andromeda "..." > out.md` should produce markdown, not a screenshot of
markdown.

The palette is deliberately monochrome: zinc-50 through zinc-200 over black.
Hierarchy comes from weight, spacing and rules, not hue. Colour that carries no
meaning is noise, and a terminal is already busy.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

# These are the exact light zinc steps used by the marketing surface. The
# terminal gets no private accent colour: if a state needs emphasis it earns
# it through bold text, a glyph, or a rule.
ZINC_50 = "#fafafa"
ZINC_100 = "#f4f4f5"
ZINC_200 = "#e4e4e7"

# The four hues, and the whole of the colour budget.
#
# The monochrome rule above is kept for *prose* — an answer is still three
# greys, because colouring a paragraph tells you nothing about it. What earns
# hue is structure: whose turn this is, whether a tool worked, and whether the
# thing on screen was asked for by a person or by the clock. Those are the
# distinctions a reader is actually scanning for, and they are exactly the ones
# a palette of near-identical greys could not carry.
#
# One hue per meaning, never two for the same one:
YOU = "#67e8f9"          # the person's turn — cyan
AGENT = "#a5b4fc"        # the agent's frame — indigo
AUTONOMOUS = "#fbbf24"   # nobody asked for this; the clock did — amber
GOOD = "#4ade80"         # a tool that did what it said
BAD = "#f87171"          # a tool that did not

THEME = Theme(
    {
        "accent": ZINC_50,
        "lane": ZINC_100,
        "muted": f"dim {ZINC_200}",
        # `ok` / `warn` / `bad` were three names for near-white, because the
        # palette had nothing else to give them. They now resolve to the hues
        # they always meant. Kept as separate names from `good`/`bad` below
        # because callers across the CLI already use them.
        "ok": GOOD,
        "warn": f"bold {AUTONOMOUS}",
        # An all-caps, wide-tracked label. The site's eyebrows —
        # EXPRESSIVE INTELLIGENCE, HUMAN / SYSTEM / ORBIT — are the strongest
        # single piece of its visual language and they cost nothing here.
        "eyebrow": f"bold {ZINC_200}",
        "figure": f"dim {ZINC_200}",
        "rule": f"dim {ZINC_200}",
        # The structural hues. Named for what they mean, not what they are, so
        # a later palette change is one edit here rather than a grep for a hex.
        "you": f"bold {YOU}",
        "agent": AGENT,
        "agent.rule": f"dim {AGENT}",
        "autonomous": f"bold {AUTONOMOUS}",
        "autonomous.rule": f"dim {AUTONOMOUS}",
        "good": GOOD,
        "bad": BAD,
        # Markdown elements. Restrained on purpose: rich's defaults colour
        # headings and code aggressively, which fights the prose.
        "markdown.h1": "bold",
        "markdown.h2": "bold",
        "markdown.h3": "bold",
        "markdown.item.number": ZINC_200,
        "markdown.item.bullet": ZINC_200,
        "markdown.code": ZINC_100,
        "markdown.link": f"{ZINC_50} underline",
        "markdown.block_quote": f"italic dim {ZINC_200}",
    }
)


def eyebrow(text: str) -> str:
    """The site's section label: all caps, letter-spaced.

    Tracking is faked with spaces because a terminal has no letter-spacing.
    Only for short labels — it doubles the width, so a long one wraps and the
    effect inverts into noise.
    """
    return " ".join(text.upper())

# `highlight=False` on both, deliberately. Rich's automatic highlighter colours
# anything that looks like a number, a path or a URL, so `/help` comes out
# magenta and `26 tools` has a cyan 26 — colour applied by a guess about
# content rather than by meaning. Model output is highlighted where it earns it
# (code blocks, tables); the CLI's own chrome is not.
console = Console(theme=THEME, highlight=False)
err_console = Console(theme=THEME, stderr=True, highlight=False)

# How often the live view redraws while text streams in. Fast enough to feel
# alive, slow enough that re-rendering a long markdown document is not the
# bottleneck.
REFRESH_HZ = 8

BLOCKS = "▏▎▍▌▋▊▉█"
CHART_FENCE = re.compile(r"```chart\s*\n(.*?)```", re.DOTALL)


def rendering_enabled() -> bool:
    return console.is_terminal


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    label: str
    value: float
    formatted: str


def parse_chart(body: str) -> list[Bar]:
    """`label: value` lines, one per bar.

    Deliberately the simplest format a model can emit without thinking about
    it. Anything unparseable is skipped rather than failing the block — a chart
    with four of five bars is more useful than an error.
    """
    bars: list[Bar] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        label, _, raw = line.rpartition(":")
        cleaned = raw.strip().replace(",", "").replace("%", "").replace("$", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        bars.append(Bar(label=label.strip(), value=value, formatted=raw.strip()))
    return bars


def render_chart(bars: list[Bar], width: int = 32) -> Text:
    """A horizontal bar chart in eighth-block characters.

    Sub-character resolution matters more than it sounds: with whole blocks
    only, every value inside the same 1/32nd of the range draws identically,
    and a chart where different numbers look the same is worse than a list.
    """
    out = Text()
    if not bars:
        return out

    largest = max(abs(bar.value) for bar in bars) or 1.0
    label_width = min(max(len(bar.label) for bar in bars), 28)
    value_width = max(len(bar.formatted) for bar in bars)

    for bar in bars:
        filled = (abs(bar.value) / largest) * width
        whole = int(filled)
        remainder = filled - whole
        glyphs = "█" * whole
        if remainder > 0.05 and whole < width:
            glyphs += BLOCKS[min(int(remainder * 8), 7)]

        out.append(f"{bar.label[:label_width].ljust(label_width)}  ", style="muted")
        out.append(glyphs or "▏", style="accent")
        out.append(f"  {bar.formatted.rjust(value_width)}\n")
    return out


OPEN_FENCE = re.compile(r"```chart\s*\n(?![\s\S]*?```)", re.MULTILINE)


def _hide_unclosed_chart(markdown_text: str) -> str:
    """Drop a chart fence that has not closed yet.

    While a chart streams in, the fence is open and markdown renders it as a
    code block — so the reader watches a code block appear and then be replaced
    by a chart. Holding it back until it closes costs a fraction of a second and
    removes the flicker entirely.
    """
    match = OPEN_FENCE.search(markdown_text)
    return markdown_text[: match.start()] if match else markdown_text


def expand_charts(markdown_text: str, streaming: bool = False) -> Group:
    """Split a document into markdown and chart blocks, rendering each in turn."""
    if streaming:
        markdown_text = _hide_unclosed_chart(markdown_text)
    parts: list = []
    cursor = 0
    for match in CHART_FENCE.finditer(markdown_text):
        before = markdown_text[cursor : match.start()]
        if before.strip():
            parts.append(Markdown(before))
        bars = parse_chart(match.group(1))
        parts.append(render_chart(bars) if bars else Markdown(match.group(0)))
        cursor = match.end()

    tail = markdown_text[cursor:]
    if tail.strip() or not parts:
        parts.append(Markdown(tail))
    return Group(*parts)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class AnswerStream:
    """Renders an answer as it arrives.

    On a terminal this is a `Live` region re-rendering markdown as text lands,
    so the reader sees structure forming rather than a wall that reflows at the
    end. Off a terminal it is `sys.stdout.write` and nothing else.
    """

    def __init__(self, live: bool | None = None) -> None:
        self.buffer: list[str] = []
        self.live_enabled = rendering_enabled() if live is None else live
        self._live: Live | None = None

    def __enter__(self) -> "AnswerStream":
        if self.live_enabled:
            self._live = Live(
                Markdown(""),
                console=console,
                refresh_per_second=REFRESH_HZ,
                vertical_overflow="visible",
            )
            self._live.start()
        return self

    def feed(self, text: str) -> None:
        self.buffer.append(text)
        if self._live is not None:
            self._live.update(expand_charts(self.text, streaming=True))
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    @property
    def text(self) -> str:
        return "".join(self.buffer)

    @contextmanager
    def paused(self):
        """Give the screen back while something else needs it.

        The live region redraws over whatever is below it, so a prompt drawn
        while it is running is overwritten on the next refresh — the question
        flickers and the cursor lands in the wrong place. Anything that reads
        from the user suspends it first.
        """
        if self._live is None:
            yield
            return
        self._live.stop()
        try:
            yield
        finally:
            self._live.start()
            self._live.update(expand_charts(self.text, streaming=True))

    def __exit__(self, *exc_info) -> None:
        if self._live is not None:
            # The final pass: any fence still open at the end is real content,
            # so it renders as the code block it is.
            self._live.update(expand_charts(self.text))
            self._live.stop()
            self._live = None


# ---------------------------------------------------------------------------
# The rest of the surface
# ---------------------------------------------------------------------------


def tool_call(summary: str, tier: str = "safe_local") -> None:
    mark = "○" if tier == "safe_local" else "●"
    console.print(f"  [accent]{mark}[/accent] {summary}", markup=True, highlight=False)


def tool_result(first_line: str, ok: bool = True) -> None:
    style = "muted" if ok else "warn"
    console.print(f"    [{style}]{first_line[:110]}[/{style}]", highlight=False)


def lane_started(specialist: str, label: str, lane_id: str = "") -> None:
    suffix = f" [muted]{lane_id}[/muted]" if lane_id else ""
    console.print(f"  [lane]▸ {specialist}[/lane] [muted]{label}[/muted]{suffix}")


def note(message: str) -> None:
    console.print(f"  [muted]{message}[/muted]")


def rule(label: str = "") -> None:
    console.rule(f"[muted]{label}[/muted]" if label else "", style="muted")


def context_meter(fraction: float, width: int = 12) -> str:
    """A compact gauge for the prompt line.

    Shown always rather than only when it matters: a meter that appears at 80%
    is a meter nobody has learned to read by the time it appears.
    """
    filled = int(max(0.0, min(fraction, 1.0)) * width)
    return "█" * filled + "░" * (width - filled)


def paint(renderable, width: int) -> Text:
    """Render to styled text at a fixed width, outside the live console.

    The full-screen surface (`andromeda_tui/`) owns its own screen and cannot
    print through this module's console — but it must not grow a second
    palette either, which is exactly how two surfaces of one product end up
    looking like two products. So it renders *through* this console, into a
    string, and hands the result to its own widgets.

    `force_terminal` because the target is a screen even when this process's
    stdout is not one under a test harness; the pipe rule is enforced where the
    surface is chosen, not here. Trailing spaces are trimmed: rich pads every
    line to the full width, and in a widget that paints background across cells
    nothing occupies.
    """
    console = Console(
        theme=THEME,
        width=max(20, width),
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
    )
    with console.capture() as captured:
        console.print(renderable)
    body = "\n".join(line.rstrip() for line in captured.get().splitlines())
    return Text.from_ansi(body.rstrip("\n"))
