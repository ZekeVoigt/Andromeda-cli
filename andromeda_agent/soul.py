"""SOUL.md — standing instructions the person writes, in their own words.

Everything else in the prompt is assembled by this program: the workspace root,
the skills manifest, the tool contracts, the standing memories. This is the one
block that is authored by the user and never rewritten, and that is what makes
it worth having. It is where "always run the tests before you say you're done"
or "I work in British English" goes — the things somebody would otherwise
retype into the first message of every session forever.

Three rules hold it in place.

**It is never written to after it is created.** Memory consolidation, session
capture and config writes all stay away from it. A file the program edits
behind you is a file you stop trusting with anything you actually care about,
and the whole value here is that it says exactly what you left in it.

**It is capped.** This text is prepended to every request the CLI ever sends,
so an unbounded file is a bill that compounds silently — the same reasoning the
per-job notepad records. Past the cap it is truncated at a line boundary and
says so, rather than being silently cut mid-sentence.

**It is instructions, not authority.** SOUL.md cannot widen the approval
ceiling, re-enable a disabled tool, or grant a lane something its belt denies.
It is prose in a prompt, and prose in a prompt is exactly as trusted as the
person who typed it — which is to say, trusted to shape *how* work is done and
never to change *what may be done*. A file that could raise permissions would
be the most attractive prompt-injection target in the product.
"""

from __future__ import annotations

from pathlib import Path

FILENAME = "SOUL.md"

# Roughly a thousand tokens. Long enough for real standing instructions, short
# enough that it never dominates the window.
MAX_CHARS = 4000

TEMPLATE = """# SOUL

<!--
Standing instructions for Andromeda. Read at the start of every session, and
never written to by this program — the file is yours.

Everything here is inside a comment, including this paragraph, so an untouched
file costs nothing on every turn. Write below it in your own words, or
uncomment what you want to keep.

## How I work

- Ask before anything you cannot undo.
- When two readings of a request are both reasonable, say so, pick one, and
  keep going rather than stopping.

## How to talk to me

- Lead with the answer. I will ask for the reasoning if I want it.
- Short sentences. No preamble.

## About my work

- What I am building, the stack, and the conventions that are not obvious
  from reading the code. This is the part worth filling in.
-->
"""


def path(home: Path) -> Path:
    return home / FILENAME


def scaffold(home: Path) -> bool:
    """Write the template if it is absent. Returns True if it was created.

    Never overwrites. A re-run of the installer must not replace what somebody
    wrote — the whole point of this file is that it survives.
    """
    target = path(home)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE, encoding="utf-8")
    return True


def _strip_comments(text: str) -> str:
    """Drop HTML comments, so the template's own hints never reach the model.

    The template ships with a `<!-- ... -->` prompt in it. Somebody who fills
    the file in around that comment should not be paying for it on every turn,
    and a model should not be reading an instruction addressed to the user.
    """
    out, depth, i = [], 0, 0
    while i < len(text):
        if text.startswith("<!--", i):
            depth += 1
            i += 4
        elif text.startswith("-->", i):
            depth = max(0, depth - 1)
            i += 3
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def load(home: Path) -> str:
    """The user's standing instructions, ready to fold into a system prompt.

    Empty string when the file is missing, unreadable, or has nothing left
    after the template's comments and headings are stripped — an untouched
    template must cost nothing, or every user pays for boilerplate they never
    wrote.
    """
    try:
        raw = path(home).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""

    text = _strip_comments(raw).strip()
    if not text:
        return ""

    # A file that is only headings and blank lines is an untouched template.
    substantive = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not substantive:
        return ""

    if len(text) > MAX_CHARS:
        cut = text.rfind("\n", 0, MAX_CHARS)
        text = text[: cut if cut > 0 else MAX_CHARS].rstrip()
        text += f"\n\n[SOUL.md truncated at {MAX_CHARS} characters.]"
    return text


def block(home: Path) -> str:
    """The prompt block, labelled so the model knows whose words these are."""
    text = load(home)
    if not text:
        return ""
    return (
        "The user's standing instructions, from their SOUL.md. They describe "
        "how to work and how to talk to them. They never grant permissions and "
        "never widen what you are allowed to do.\n\n" + text
    )
