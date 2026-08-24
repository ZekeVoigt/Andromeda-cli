"""The startup study: Leonardo, a frame, and a scan.

This is the terminal's version of `ProportionStudy` on ai-andromeda.com — the
Vitruvian figure inside a circle and square, sky coordinates in the corners, a
scan line sweeping down it, and `HUMAN / SYSTEM / ORBIT · FIG. A—01` beneath.
Those coordinates are not decoration: RA 00h 42m 44s / DEC +41° 16′ 09″ is
where the Andromeda galaxy actually is.

**The figure is pre-rendered; the frame is drawn.** The site does exactly this
— `studyImage` is the JPEG, while `studyCircle` and `studySquare` are elements
— and for the same reason. Sampling ruled lines out of a photograph of a
drawing gives you a mushy approximation of a shape you could have drawn
exactly. `scripts/render-vitruvian.py` converts the figure once, at build time,
so nothing here needs an imaging dependency at runtime.

Braille (U+2800–U+28FF) carries the figure: 2x4 independently settable dots per
character, which is the same halftone-dot idea the landing page uses, and the
only way to get this much resolution out of a text grid.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Two renders of the same drawing, chosen by how much room there is.
#
# One size cannot serve both. At 58 columns the figure is legible but sparse —
# it reads as a suggestion of a body. At 96 it reads as the drawing: the
# square's sides, the spread legs, the outstretched arms. But 96 columns
# overflows an 80-column terminal, and art that wraps is worse than art that
# is small.
#
# So the wide one is used when it fits with room to spare, and the compact one
# otherwise. The threshold is not the terminal's exact width: a figure pressed
# against both edges looks cramped, and the caption line underneath needs to
# sit inside the same measure.
WIDE_PATH = Path(__file__).with_name("vitruvian-wide.txt")
COMPACT_PATH = Path(__file__).with_name("vitruvian.txt")

# Below this, the wide render has no room to breathe.
WIDE_MIN_WIDTH = 104

# Andromeda (M31). The same pair the landing page prints in its corners.
RA = "RA 00h 42m 44s"
DEC = "DEC +41° 16′ 09″"
CAPTION_LEFT = "HUMAN / SYSTEM / ORBIT"
CAPTION_RIGHT = "FIG. A—01"

# How long the scan takes to cross the figure, and how many rows it lights at
# once. Short enough that nobody waits for it — a startup animation that has to
# be sat through is a startup animation people learn to skip.
SCAN_SECONDS = 0.55
SCAN_BAND = 3


def figure(width: int = 0) -> list[str]:
    """The pre-rendered figure at the largest size that fits, or nothing.

    Missing art is not an error worth failing a launch over. Returning an empty
    list lets every caller degrade to a plain header instead of crashing on the
    way to a prompt — and the compact render is the fallback for the wide one,
    so a partial install still draws something.
    """
    candidates = [COMPACT_PATH]
    if width >= WIDE_MIN_WIDTH:
        candidates.insert(0, WIDE_PATH)
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8").rstrip("\n").split("\n")
        except OSError:
            continue
    return []


def supported(stream=None) -> bool:
    """Whether to draw the study at all.

    Three ways out, and each one is a real terminal somebody uses:

    - not a tty — piped or redirected, where art is corruption, not decoration;
    - `NO_COLOR` or `ANDROMEDA_NO_ART` — asked not to;
    - a terminal that cannot encode braille, which is most of the point.

    The encoding check is done by actually encoding, rather than by inspecting
    the locale: `LANG` is routinely unset in containers and over ssh while the
    terminal underneath handles UTF-8 perfectly well.
    """
    stream = stream or sys.stdout
    if os.environ.get("ANDROMEDA_NO_ART") or os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "⣿".encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _pad(lines: list[str], width: int) -> list[str]:
    """Left-pad the figure so it sits centred inside `width`."""
    if not lines:
        return []
    art_width = max(len(line) for line in lines)
    left = max(0, (width - art_width) // 2)
    return [(" " * left) + line for line in lines]


def study(width: int = 64) -> list[tuple[str, str]]:
    """The whole composition as `(text, style)` rows, ready to print.

    Returned as rows rather than printed so the same composition serves the
    static path, the animated path and the tests, and the three cannot drift.
    """
    lines = figure(width)
    if not lines:
        return []

    art_width = max(len(line) for line in lines)
    inner = max(art_width, len(CAPTION_LEFT) + len(CAPTION_RIGHT) + 4)
    inner = min(inner, max(width - 4, 20))

    rows: list[tuple[str, str]] = []
    gap = max(1, inner - len(RA) - len(DEC))
    rows.append((f"  {RA}{' ' * gap}{DEC}", "muted"))
    rows.append(("", ""))
    rows.extend((line, "figure") for line in _pad(lines, inner + 2))
    rows.append(("", ""))
    gap = max(1, inner - len(CAPTION_LEFT) - len(CAPTION_RIGHT))
    rows.append((f"  {CAPTION_LEFT}{' ' * gap}{CAPTION_RIGHT}", "muted"))
    return rows


def scan(console, width: int = 64) -> None:
    """Draw the study with a scan line sweeping down it, then leave it drawn.

    The sweep is the landing page's `studyScan` element. It is one pass, top to
    bottom, and what remains afterwards is exactly what `study()` would have
    printed — so a session that skips the animation and one that plays it end
    on the same screen.

    Falls back to printing the static composition on any terminal that cannot
    take live redraws, and on a `KeyboardInterrupt`, because someone pressing
    ctrl-c during a decoration wants the prompt, not a traceback.
    """
    rows = study(width)
    if not rows:
        return

    figure_rows = [i for i, (_, style) in enumerate(rows) if style == "figure"]
    if not figure_rows:
        for text, style in rows:
            console.print(text, style=style or None)
        return

    from rich.live import Live
    from rich.text import Text

    first, last = figure_rows[0], figure_rows[-1]
    steps = last - first + SCAN_BAND + 1
    delay = SCAN_SECONDS / max(steps, 1)

    def frame(head: int) -> Text:
        out = Text()
        for i, (text, style) in enumerate(rows):
            if style == "figure":
                distance = head - (i - first)
                if distance < 0:
                    # Ahead of the scan: not yet revealed.
                    out.append("\n")
                    continue
                style = "bold white" if distance < SCAN_BAND else "figure"
            out.append(text, style=style or None)
            out.append("\n")
        return out

    try:
        with Live(frame(0), console=console, refresh_per_second=30, transient=False) as live:
            for head in range(steps + 1):
                live.update(frame(head))
                time.sleep(delay)
            live.update(frame(steps + 1))
    except (KeyboardInterrupt, Exception):  # noqa: BLE001 - decoration must never fail a launch
        for text, style in rows:
            console.print(text, style=style or None)
