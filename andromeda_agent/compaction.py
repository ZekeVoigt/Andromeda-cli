"""Keeping a conversation inside the model's window.

Two stages, in this order, because they cost very differently:

  1. **Micro-compact** — replace the *content* of old tool results with a
     placeholder. Free, instant, and usually enough: a transcript's weight is
     almost always old file reads and command output nobody will look at again.
  2. **Full compaction** — ask the model to summarise the older part of the
     conversation and replace it with that summary. Costs a model call, so it
     is only reached when pruning was not enough.

The constants: a summary budget of 20% of the window, floor 2000 tokens,
ceiling 10000, and a 200-char floor below which pruning a tool result buys
nothing.

**The invariant that makes this safe:** an assistant message carrying
`tool_calls` and the `tool` messages answering them are one unit. Splitting
between them produces a request the API rejects outright — every `tool_call_id`
must have an answer. Every operation here moves whole units or nothing.

**Nothing compacted is actually lost.** Both stages leave the full text in the
session index, so a pruned tool result and a summarised-away turn are both
still readable through `session_search`. The placeholder and the summary say
so, because a model that believes the detail is gone either re-does the work or
answers from the summary when it should have looked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tuned so a long session degrades gradually rather than hitting a cliff.
PRUNE_MIN_CHARS = 200
KEEP_RECENT_TOOLS = 2
PRUNED_PLACEHOLDER = (
    "[Old tool output cleared to save context. The full result is still in this "
    "session's history — search it with session_search if you need it again.]"
)
SUMMARY_RATIO = 0.20
MIN_SUMMARY_TOKENS = 2000
SUMMARY_TOKENS_CEILING = 10_000

# Start compacting well before the wall. Waiting until the request is refused
# means the compaction call itself has no room to run.
COMPACT_AT = 0.75
# How much of the transcript stays verbatim after a full compaction. Too small
# and the model loses the thread it was following; too large and the summary
# buys nothing.
KEEP_RECENT_FRACTION = 0.30

SUMMARY_PREFIX = "[CONTEXT SUMMARY — earlier turns, compacted]"

# Appended to the rendered summary, never to the instruction above. The
# distinction is deliberate: the instruction is what the model writes *from*,
# and telling it there is a safety net while it writes produces a lazier
# summary. The note is what a later turn reads, and by then knowing the
# originals are reachable is the difference between looking and guessing.
RECALL_TEMPLATE = (
    "The {count} turn(s) this replaced are not lost — they are still in this "
    "session's searchable history. `session_search(query=\"…\")` finds them, and "
    "`session_search(session_id=\"{session}\", anchor=N)` reads any of them in "
    "context. Use it rather than guessing at a detail this summary left out."
)

SUMMARY_INSTRUCTION = """Summarise the conversation above for your own future reference.

You are running out of context and this summary will replace those turns \
entirely — anything you leave out is gone. Write for yourself, not for a reader.

Cover, in this order:
1. What the user actually asked for, in their words where it matters.
2. Decisions made and why — especially ones you would otherwise re-litigate.
3. What you have already established about the system: files, paths, structures, \
values you looked up. Be specific; a vague summary means doing the work twice.
4. What was tried and failed, so you do not try it again.
5. Exactly where you are now and what the next step is.

Do not editorialise, do not apologise for the summary, and do not address the \
user. Facts only."""


@dataclass
class CompactionResult:
    happened: bool = False
    stage: str = ""  # "prune" | "summarise"
    before_tokens: int = 0
    after_tokens: int = 0
    pruned_results: int = 0
    summarised_messages: int = 0

    @property
    def freed(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough count, ~4 characters per token.

    The same estimator the relay uses for its pre-flight reservation. Exact
    counting needs the model's tokenizer, which is a dependency and a
    per-provider difference; this is within about 10% and errs high on prose,
    which is the safe direction for a budget.
    """
    characters = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    characters += len(part["text"])
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            characters += len(str(function.get("name", "")))
            characters += len(str(function.get("arguments", "")))
    return characters // 4


def usage_fraction(messages: list[dict[str, Any]], window: int) -> float:
    if window <= 0:
        return 0.0
    return estimate_tokens(messages) / window


def needs_compaction(messages: list[dict[str, Any]], window: int) -> bool:
    return usage_fraction(messages, window) >= COMPACT_AT


def micro_compact(
    messages: list[dict[str, Any]], keep_recent: int = KEEP_RECENT_TOOLS
) -> tuple[list[dict[str, Any]], int]:
    """Blank the content of old tool results, keeping the most recent intact.

    The messages themselves stay — their `tool_call_id`s are still answering
    calls that are still in the transcript, and removing them would strand
    those calls.
    """
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = set(tool_indexes[-keep_recent:]) if keep_recent else set()

    out: list[dict[str, Any]] = []
    pruned = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        if (
            index in tool_indexes
            and index not in protected
            and isinstance(content, str)
            and len(content) > PRUNE_MIN_CHARS
            and content != PRUNED_PLACEHOLDER
        ):
            out.append({**message, "content": PRUNED_PLACEHOLDER})
            pruned += 1
        else:
            out.append(message)
    return out, pruned


def safe_split(messages: list[dict[str, Any]], want_keep: int) -> int:
    """Index of the first message to keep verbatim.

    Walks forward from the desired point to the next message that can legally
    begin a transcript: a `user` message, or an `assistant` message with no
    tool calls. Landing on a `tool` message, or on an assistant whose calls are
    answered below, would strand a `tool_call_id` and the API would reject the
    whole request.
    """
    if want_keep <= 0 or want_keep >= len(messages):
        return len(messages)

    for index in range(want_keep, len(messages)):
        message = messages[index]
        role = message.get("role")
        if role == "user":
            return index
        if role == "assistant" and not message.get("tool_calls"):
            return index
    return len(messages)


def summary_budget(window: int) -> int:
    return max(MIN_SUMMARY_TOKENS, min(int(window * SUMMARY_RATIO), SUMMARY_TOKENS_CEILING))


def plan_summarisation(
    messages: list[dict[str, Any]], window: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (system, to summarise, to keep verbatim).

    The system message never moves: it carries the workspace, the skills
    manifest and the standing memories, and summarising it would quietly change
    the rules the rest of the run operates under.
    """
    system = messages[:1] if messages and messages[0].get("role") == "system" else []
    body = messages[len(system) :]
    if not body:
        return system, [], []

    keep_from = safe_split(body, int(len(body) * (1 - KEEP_RECENT_FRACTION)))
    return system, body[:keep_from], body[keep_from:]


def recall_note(session_id: str, count: int) -> str:
    """How a later turn is told the compacted turns are still reachable.

    Empty when there is no session to search — a one-shot run, a lane, or an
    install whose index could not be written. Promising a lookup that will
    fail is worse than not offering one.
    """
    if not session_id or count <= 0:
        return ""
    return RECALL_TEMPLATE.format(count=count, session=session_id)


def render_summary(text: str, recall: str = "") -> dict[str, Any]:
    """The summary as it re-enters the transcript.

    A `user` message rather than `assistant`: an assistant message the model
    did not actually say in that position confuses turn-taking, and several
    providers reject two assistant messages in a row.
    """
    body = text.strip()
    if recall:
        body = f"{body}\n\n{recall}"
    return {"role": "user", "content": f"{SUMMARY_PREFIX}\n\n{body}"}


def is_summary(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith(SUMMARY_PREFIX)
