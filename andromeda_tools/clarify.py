"""Asking the user a question mid-run.

Without this the agent guesses or stops. Guessing is worse: a wrong assumption
five steps in costs the whole run, and the question that would have prevented it
takes four seconds to answer.

The schema omits the parts that would need a GUI. Its most load-bearing rule is
the one models get wrong: **options belong in `choices`, never enumerated
inside the question text.** A question reading "Which target? 1) staging 2) prod" renders
as dead prose the user cannot pick.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .spec import ToolResult, failure

MAX_CHOICES = 4
MAX_QUESTIONS = 5

DESCRIPTION = (
    "Ask the user a question when you need a decision before proceeding. "
    "Two modes: pass `choices` (up to 4) for a pick-one question, or omit them "
    "for open-ended free text. Put the option you recommend FIRST — it is "
    "labelled '(Recommended)' and is the default. Ask several independent "
    "questions in one call with `questions` rather than a chain of separate "
    "calls.\n\n"
    "CRITICAL: when you are offering options, put each one ONLY in `choices` — "
    "never enumerate them inside the question text. Right: "
    "question='Which deployment target?', choices=['staging', 'prod']. Wrong: "
    "question='Which target? 1) staging 2) prod'.\n\n"
    "Use this when the task is genuinely ambiguous and the readings lead to "
    "different work. Do NOT use it to confirm a dangerous command — the "
    "approval gate already does that — and do not use it for something you "
    "could establish with a tool."
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "The question itself, and ONLY the question. Do not embed the "
                "answer options here — pass them in `choices`."
            ),
        },
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_CHOICES,
            "description": (
                "Selectable options, one per element, up to 4. ORDER MATTERS: "
                "the one you recommend goes first. Do not write "
                "'(Recommended)' yourself. Omit entirely for a genuinely "
                "open-ended question."
            ),
        },
        "questions": {
            "type": "array",
            "maxItems": MAX_QUESTIONS,
            "description": (
                "Ask 2-5 INDEPENDENT questions in one call instead of several "
                "sequential ones. Only batch questions that are truly "
                "independent — if one answer would change another question, "
                "ask separately."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Short identifier echoed in the answer.",
                    },
                    "question": {"type": "string"},
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_CHOICES,
                    },
                },
                "required": ["question"],
            },
        },
    },
    "required": [],
}


# Escape sequences and the C0/C1 control characters, which every surface that
# renders a question writes straight to a terminal.
#
# This is not tidying. The question text and the choices are model output, and
# a terminal reads a stray `\r`, `\b` or `\x1b[` as a *cursor instruction*
# rather than as characters: one carriage return inside a choice moves the rest
# of that row to column zero, outside the box the prompt is drawn in, over
# whatever was there. The row that moves is the row somebody is trying to pick.
# A tab does the same thing more quietly, jumping to the next multiple of eight.
#
# So the cursor stays where the layout put it: escapes are dropped, controls
# become spaces, and runs of whitespace — newlines included, since every one of
# these surfaces lays a question out as one line and wraps it itself — collapse
# to one.
_ESCAPES = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])"
)
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(raw: Any) -> str:
    """One line of text that cannot move the cursor."""
    text = raw if isinstance(raw, str) else str(raw)
    return " ".join(_CONTROLS.sub(" ", _ESCAPES.sub("", text)).split())


# Keys a model reaches for when it wraps a choice in an object instead of
# writing the string. Ordered: the first one present wins.
_CHOICE_LABELS = ("label", "text", "name", "title", "choice", "option", "value")


def choice_text(raw: Any) -> str:
    """One choice as the line a person reads.

    `str(raw)` was doing this, and on the day a model passed an object instead
    of a string it put a Python dict repr on screen —
    ``{'question': 'auth', 'choices': [...]}`` — inside the prompt asking
    somebody to choose. Unreadable, and unanswerable.

    So: strings pass through, objects are asked for a label, and anything with
    no readable label becomes empty and is dropped by the caller rather than
    rendered as its repr. A choice nobody can read is worse than one fewer
    choice.
    """
    if isinstance(raw, str):
        return plain(raw)
    if isinstance(raw, dict):
        for key in _CHOICE_LABELS:
            value = raw.get(key)
            if isinstance(value, str) and plain(value):
                return plain(value)
        return ""
    if isinstance(raw, (int, float, bool)):
        return str(raw)
    return ""


def _looks_like_a_question(raw: Any) -> bool:
    """Whether a `choices` entry is really a whole question.

    The two shapes are next to each other in the schema and models mix them up:
    the batch form gets passed as `choices` instead of `questions`. Recognising
    it is much better than dropping it — the person still gets asked, rather
    than seeing a question with no options at all.
    """
    return (
        isinstance(raw, dict)
        and isinstance(raw.get("choices"), list)
        and not any(isinstance(raw.get(key), str) for key in _CHOICE_LABELS)
    )


class Question:
    def __init__(self, text: str, choices: list[str] | None = None, key: str = "") -> None:
        self.text = plain(text or "")
        cleaned = [choice_text(c) for c in (choices or [])]
        self.choices = [c for c in cleaned if c][:MAX_CHOICES]
        self.key = key or self.text[:40]


# The surface supplies this. It returns one answer per question, in order.
Asker = Callable[[list[Question]], list[str]]


def ask(asker: Asker | None, question: str = "", choices: list[str] | None = None,
        questions: list[dict[str, Any]] | None = None) -> ToolResult:
    if asker is None:
        # Nobody is at the keyboard. Refused rather than answered with a
        # default, because a default here is exactly the guess this tool exists
        # to replace.
        return failure(
            "There is nobody to ask — this is a non-interactive run. State your "
            "assumption explicitly and carry on, or say what you need."
        )

    batch: list[Question] = []
    if questions:
        for raw in questions[:MAX_QUESTIONS]:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("question") or "").strip()
            if text:
                batch.append(
                    Question(text, raw.get("choices"), str(raw.get("id") or ""))
                )
        if not batch:
            return failure("`questions` had no usable entries.")
    else:
        if not (question or "").strip():
            return failure("A question is required.")
        # A batch passed in the wrong field. Unwrapped rather than refused: the
        # model asked something sensible, it just used the neighbouring
        # parameter, and refusing costs the person a whole turn to find out.
        nested = [c for c in (choices or []) if _looks_like_a_question(c)]
        plain = [c for c in (choices or []) if not _looks_like_a_question(c)]
        if len(nested) == 1 and not plain:
            # The overwhelmingly common shape: the prose question is at the top
            # level and its options got wrapped in one object. Merged rather
            # than split, because splitting asks the person the same thing
            # twice — once with no options, then again under whatever short
            # label the model used for it.
            batch = [Question(question, nested[0].get("choices"))]
        elif nested:
            batch = [Question(question, plain)]
            for raw in nested[: MAX_QUESTIONS - 1]:
                text = str(raw.get("question") or "").strip()
                if text:
                    batch.append(Question(text, raw.get("choices")))
        else:
            batch = [Question(question, choices)]

    try:
        answers = asker(batch)
    except KeyboardInterrupt:
        return failure("The user dismissed the question without answering.")

    lines = []
    for asked, answer in zip(batch, answers):
        answer = (answer or "").strip()
        lines.append(f"{asked.text}\n  → {answer or '(no answer)'}")

    return ToolResult(
        content="\n\n".join(lines),
        display=f"asked {len(batch)} question{'s' if len(batch) != 1 else ''}",
        metadata={"answers": list(answers)},
    )
