"""What the surface is told about a turn.

`andromeda_agent.loop.Callbacks` is a bundle of function pointers the loop
calls on whatever thread it happens to be running on. That is exactly right for
the REPL, which draws from the same thread. It is exactly wrong for a
full-screen UI, where widgets may only be touched from the event loop.

So the callbacks are adapted once, here, into a stream of small immutable
records. The TUI drains that stream on its own clock and touches widgets on its
own thread. Nothing else in the codebase has to know a UI thread exists.

**Every event serialises.** Not because anything writes them to a socket today
— nothing does — but because the alternative to this design is a TUI in a
separate process speaking JSON-RPC to a gateway (see the note at the top of
`app.py`). If that day comes, the gateway writes `event.to_json()` down a pipe
and the event vocabulary does not change. A test asserts every event round-trips, so the seam
stays real rather than aspirational.

Deliberately *not* here: the `ToolSpec` and `ApprovalRequest` objects
themselves. An event carries names, tiers and rendered summaries — the things a
screen shows. Passing the live objects through would make the stream
un-serialisable and would let a widget reach back into the policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from andromeda_agent.loop import Callbacks
from andromeda_tools import ToolResult, ToolSpec


@dataclass(frozen=True)
class UiEvent:
    """Base for everything the agent thread says.

    `kind` is the wire name. It is written out rather than derived from the
    class name so renaming a class cannot silently change the protocol.
    """

    kind: str = field(default="", init=False)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class TurnStarted(UiEvent):
    kind = "turn.started"
    prompt: str


@dataclass(frozen=True)
class TextDelta(UiEvent):
    """A fragment of the answer, exactly as the provider streamed it."""

    kind = "text.delta"
    text: str


@dataclass(frozen=True)
class ToolStarted(UiEvent):
    kind = "tool.started"
    name: str
    tier: str
    # `ToolSpec.summary()`, which is what the approval prompt shows too. For
    # `terminal` that is the command itself — a paraphrase would make the
    # activity lane and the consent prompt disagree about what ran.
    summary: str


@dataclass(frozen=True)
class ToolFinished(UiEvent):
    kind = "tool.finished"
    name: str
    ok: bool
    # `ToolResult.display`'s first line. A 4000-line file is a legitimate tool
    # result and an illegitimate thing to put on a screen.
    detail: str


@dataclass(frozen=True)
class ToolDenied(UiEvent):
    kind = "tool.denied"
    name: str
    reason: str


@dataclass(frozen=True)
class LaneStarted(UiEvent):
    kind = "lane.started"
    specialist: str
    label: str


@dataclass(frozen=True)
class Compacted(UiEvent):
    kind = "compacted"
    stage: str
    detail: str


@dataclass(frozen=True)
class QuestionAsked(UiEvent):
    """The agent thread is blocked until this is answered.

    The blocking handle stays in the driver, keyed by `request_id`; only the id
    travels. That is what lets a gateway be added later without changing the
    gate: over a socket the id is all that can travel anyway.
    """

    kind = "question.asked"
    request_id: str
    # "approval" | "clarify"
    form: str
    title: str
    # Free-form, per form. Approval: tier, summary lines, the allowlist hint.
    # Clarify: the questions and their choices.
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuestionClosed(UiEvent):
    """The handle is gone — whether it was answered, refused or released.

    Emitted so a screen showing the prompt tears it down even when the answer
    did not come from that screen (a shutdown releasing every gate, an
    interrupt refusing one).
    """

    kind = "question.closed"
    request_id: str


@dataclass(frozen=True)
class TurnFinished(UiEvent):
    kind = "turn.finished"
    text: str
    steps: int
    lanes_running: list[str] = field(default_factory=list)
    processes_running: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TurnFailed(UiEvent):
    kind = "turn.failed"
    message: str
    hint: str = ""


@dataclass(frozen=True)
class TurnInterrupted(UiEvent):
    kind = "turn.interrupted"


@dataclass(frozen=True)
class Notice(UiEvent):
    """Something the surface should say that is not part of the answer."""

    kind = "notice"
    text: str
    tone: str = "muted"


# Every event class, by wire name. Used by the round-trip test and by anything
# that has to decode the stream — a gateway client, a transcript replayer.
EVENT_KINDS: dict[str, type[UiEvent]] = {
    cls.kind: cls
    for cls in (
        TurnStarted,
        TextDelta,
        ToolStarted,
        ToolFinished,
        ToolDenied,
        LaneStarted,
        Compacted,
        QuestionAsked,
        QuestionClosed,
        TurnFinished,
        TurnFailed,
        TurnInterrupted,
        Notice,
    )
}


def first_line(text: str, limit: int = 140) -> str:
    """One line, bounded. Tool output arrives in whatever shape it likes."""
    line = (text or "").splitlines()[0] if text else ""
    return line[:limit]


def callbacks_for(post, *, ask_approval=None, before_text=None) -> Callbacks:
    """Adapt `Callbacks` onto an event sink.

    `post` takes one `UiEvent` and must be safe to call from the agent thread —
    in practice it appends to a queue and returns.

    `before_text` runs on every text delta before the event is posted. That is
    the loop's only guaranteed-frequent call-back into surface code, so it is
    where a cooperative interrupt raises from; see `driver.py`. It is a
    separate hook rather than logic inside `post` because posting must stay a
    pure hand-off — a sink that can raise would leave the transcript in
    whatever state the exception found it.

    Every field of `Callbacks` is populated here on purpose. A callback the
    surface forgets is not a visible bug, it is silence: tools that run without
    a line on screen. A test compares this against `Callbacks`' fields.
    """

    def on_text(text: str) -> None:
        if before_text is not None:
            before_text()
        post(TextDelta(text=text))

    def on_tool_start(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        post(
            ToolStarted(
                name=spec.name, tier=spec.risk_tier, summary=spec.summary(arguments)
            )
        )

    def on_tool_result(spec: ToolSpec, result: ToolResult) -> None:
        post(
            ToolFinished(
                name=spec.name, ok=result.ok, detail=first_line(result.display)
            )
        )

    def on_tool_denied(spec: ToolSpec, reason: str) -> None:
        post(ToolDenied(name=spec.name, reason=reason))

    def on_compaction(result) -> None:
        detail = (
            f"cleared {result.pruned_results} old tool results"
            if result.stage == "prune"
            else f"summarised {result.summarised_messages} earlier messages"
        )
        post(Compacted(stage=result.stage, detail=detail))

    def on_retry(reason: str) -> None:
        # A `Notice` rather than a wire event of its own: a retry is exactly
        # "something the surface should say that is not part of the answer",
        # and a new event kind that every client has to learn buys nothing
        # over the one that already means this.
        post(Notice(text=reason))

    return Callbacks(
        on_text=on_text,
        on_tool_start=on_tool_start,
        on_tool_result=on_tool_result,
        on_tool_denied=on_tool_denied,
        on_compaction=on_compaction,
        on_retry=on_retry,
        ask_approval=ask_approval,
    )
