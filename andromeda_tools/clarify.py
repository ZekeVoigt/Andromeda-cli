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


class Question:
    def __init__(self, text: str, choices: list[str] | None = None, key: str = "") -> None:
        self.text = (text or "").strip()
        self.choices = [str(c).strip() for c in (choices or []) if str(c).strip()][:MAX_CHOICES]
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
