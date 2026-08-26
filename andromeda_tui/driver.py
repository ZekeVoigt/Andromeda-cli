"""Running the agent underneath a full-screen UI.

`Conversation.send()` is synchronous and blocking, and its approval gate is
blocking by design: the loop stops, a person answers, the loop continues.
Consent that can be raced is not consent. So the turn runs on a worker thread
and the screen keeps its own thread, and everything crossing between them goes
through the two mechanisms below.

**Downward — events.** The worker appends to a queue and returns immediately;
the UI drains it on a timer. No locks, no re-entrancy, no widget touched from
the wrong thread, and natural batching: a hundred text deltas between two ticks
become one re-render instead of a hundred.

**Upward — answers.** A question the worker asks blocks it on a
`threading.Event`. Only the request id crosses in the event; the handle stays
here. The UI answers by id. That is deliberately the shape a socket would
force, because over one an id is all that *can* cross — keeping it now is what
makes an IPC gateway a later addition rather than a rewrite.

**There is no timeout on a question.** A deadline buys protection only against
a UI that has stopped answering, which
`shutdown()` already handles deterministically by releasing every gate with its
refusal; against a live UI all a deadline does is silently refuse a call the
person was three seconds from approving. Walking away is a refusal because
`shutdown()` says so, not because a clock ran out.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from andromeda_agent import AgentError, ApprovalRequest
from andromeda_agent.approval import Answer
from andromeda_tools.clarify import Question as ClarifyQuestion

from . import events as ev

# How many events one drain may take. A drain that empties the queue lets a
# fast producer — a tool spewing output, a long stream — hold the UI thread for
# as long as it keeps producing. Bounded, the UI always gets back to painting
# and reading keys, and the rest arrives on the next tick.
DRAIN_LIMIT = 512


@dataclass
class Pending:
    """A question the agent thread is blocked on.

    `default` is what the question resolves to if it is released rather than
    answered — a shutdown, an interrupt, a UI that died. For an approval that
    is `"no"`: every path out of an unanswered consent prompt has to be a
    refusal, and making it a field rather than a branch is how it stays that
    way when someone adds a third path.
    """

    id: str
    form: str
    default: Any
    gate: threading.Event = field(default_factory=threading.Event)
    answer: Any = None
    answered: bool = False

    def resolve(self, answer: Any) -> None:
        self.answer = answer
        self.answered = True
        self.gate.set()

    def release(self) -> None:
        """Unblock with the default. Safe to call on an already-answered gate."""
        if not self.answered:
            self.answer = self.default
        self.gate.set()

    def wait(self) -> Any:
        self.gate.wait()
        return self.answer


class TurnInterrupted(BaseException):
    """Raised inside the agent thread to abandon a turn.

    `BaseException`, not `Exception`, on purpose: `Conversation._run` catches
    `Exception` around every tool call so a failing tool cannot end a session,
    and an interrupt caught there would be reported to the model as a tool
    error and the turn would carry on. This is the same reason `KeyboardInterrupt`
    is a `BaseException`.
    """


def _binding_for(record):
    """A binding around a bare record, for a conversation built without one.

    Only the test doubles and anything constructing a `Conversation` directly
    take this path; every real surface goes through `session.build_conversation`,
    which binds it.
    """
    from andromeda_cli import sessions as sessions_store

    return sessions_store.Binding(record)


class AgentDriver:
    """One conversation, one worker thread, one event queue."""

    def __init__(
        self,
        conversation,
        record,
        *,
        checkpoints=None,
        on_event: Callable[[ev.UiEvent], None] | None = None,
    ) -> None:
        self.conversation = conversation
        # Held through the binding, not directly: `/resume` moves which
        # transcript this terminal writes to, and a driver holding its own
        # reference would keep appending turns to the session you just left.
        self.binding = getattr(conversation, "binding", None) or _binding_for(record)
        self.checkpoints = checkpoints
        self._queue: "queue.Queue[ev.UiEvent]" = queue.Queue()
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._interrupt = threading.Event()
        self._closed = False
        # An optional straight-through sink, for tests that want the events
        # without running an event loop. The UI does not use it — it drains.
        self._on_event = on_event

    # ---- the downward channel --------------------------------------------

    def post(self, event: ev.UiEvent) -> None:
        """Called from the agent thread. Must not raise and must not block."""
        self._queue.put(event)
        if self._on_event is not None:
            self._on_event(event)

    def drain(self, limit: int = DRAIN_LIMIT) -> list[ev.UiEvent]:
        """Everything said since the last drain, in order, bounded."""
        out: list[ev.UiEvent] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    # ---- the upward channel ----------------------------------------------

    def _open_question(self, form: str, title: str, body: dict, default: Any) -> Pending:
        pending = Pending(id=uuid.uuid4().hex[:8], form=form, default=default)
        with self._lock:
            if self._closed:
                # The UI is gone. Answering with the default here rather than
                # registering the handle means the agent thread never blocks on
                # a gate nobody will ever open.
                pending.release()
                return pending
            self._pending[pending.id] = pending
        self.post(
            ev.QuestionAsked(request_id=pending.id, form=form, title=title, body=body)
        )
        return pending

    def _close_question(self, pending: Pending) -> None:
        with self._lock:
            self._pending.pop(pending.id, None)
        self.post(ev.QuestionClosed(request_id=pending.id))

    def answer(self, request_id: str, answer: Any) -> bool:
        """Called from the UI thread. False if the question is already gone."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.resolve(answer)
        return True

    @property
    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)

    @property
    def waiting(self) -> bool:
        """True while a question is open. The composer is disabled on this."""
        with self._lock:
            return bool(self._pending)

    # ---- the two questions the agent can ask ------------------------------

    def ask_approval(self, request: ApprovalRequest) -> Answer:
        """The gate. Runs on the agent thread and blocks it.

        The summary is passed through verbatim, split into lines by the screen
        rather than reflowed here — for `terminal` it is the command itself, and
        a prompt that paraphrases what it is asking about is not consent.
        """
        allowlist = getattr(request, "allowlist", None)
        approvals = 0
        if allowlist is not None and allowlist.should_suggest(request.spec.name):
            approvals = allowlist.approvals_of(request.spec.name)

        pending = self._open_question(
            "approval",
            request.spec.name,
            {
                "tool": request.spec.name,
                "tier": request.spec.risk_tier,
                "summary": request.summary,
                # Set when a hook escalated a call the policy would have
                # allowed; the screen shows it under the summary.
                "reason": getattr(request, "reason", None) or "",
                # >0 means "you have approved this N times"; the screen offers
                # to stop asking. The suggestion never widens anything by
                # itself — promotion is always the explicit answer.
                "approvals": approvals,
            },
            default="no",
        )
        try:
            return pending.wait()
        finally:
            self._close_question(pending)

    def ask_questions(self, questions: list[ClarifyQuestion]) -> list[str]:
        """`clarify`. Also blocking, also on the agent thread."""
        pending = self._open_question(
            "clarify",
            "clarify",
            {
                "questions": [
                    {"text": q.text, "choices": list(q.choices), "key": q.key}
                    for q in questions
                ]
            },
            # An unanswered clarify is silence, not a guess. The tool renders
            # an empty answer as "(no answer)" and the model decides what to do
            # with that — which is the whole point of the tool.
            default=[""] * len(questions),
        )
        try:
            answers = pending.wait()
        finally:
            self._close_question(pending)
        return list(answers) if isinstance(answers, list) else [""] * len(questions)

    # ---- running a turn ---------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def submit(self, prompt: str) -> bool:
        """Start a turn. False if one is already running."""
        if self.busy:
            return False
        self._interrupt.clear()

        # Taken before the turn, so rewinding lands where you were when you
        # asked — not after the answer you want to discard. Same ordering as
        # the REPL, and for the same reason.
        if self.checkpoints is not None:
            self.checkpoints.take(self.conversation.messages, prompt)
            self.binding.record.checkpoints = self.checkpoints.to_json()

        self.post(ev.TurnStarted(prompt=prompt))
        self._worker = threading.Thread(
            target=self._run_turn, args=(prompt,), name="andromeda-turn", daemon=True
        )
        self._worker.start()
        return True

    def _run_turn(self, prompt: str) -> None:
        callbacks = ev.callbacks_for(
            self.post,
            ask_approval=self.ask_approval,
            before_text=self._check_interrupt,
        )
        try:
            text = self.conversation.send(prompt, callbacks)
        except TurnInterrupted:
            self._heal_transcript()
            self.post(ev.TurnInterrupted())
            return
        except AgentError as exc:
            self._heal_transcript()
            self.post(ev.TurnFailed(message=str(exc), hint=exc.hint))
            return
        except Exception as exc:  # noqa: BLE001 - a broken turn is not a broken UI
            self._heal_transcript()
            self.post(ev.TurnFailed(message=f"{type(exc).__name__}: {exc}"))
            return

        lanes = getattr(self.conversation, "lane_registry", None)
        processes = getattr(self.conversation, "process_registry", None)
        self.post(
            ev.TurnFinished(
                text=text,
                steps=self.conversation.steps_taken,
                # Reported for the same reason the REPL reports them: a lane
                # the model forgot to wait for is billed and silently discarded
                # unless somebody says it is still running.
                lanes_running=[lane.id for lane in (lanes.running if lanes else [])],
                processes_running=[
                    process.id for process in (processes.running if processes else [])
                ],
            )
        )

    # ---- interruption -----------------------------------------------------

    def interrupt(self) -> None:
        """Abandon the running turn, and refuse anything it is waiting on.

        Refusing first: a turn blocked in the approval gate is not running any
        loop code, so raising alone would never reach it. Releasing the gate
        unblocks the worker with `"no"`, and the interrupt flag stops it at the
        next delta.
        """
        self._interrupt.set()
        with self._lock:
            waiting = list(self._pending.values())
        for pending in waiting:
            pending.release()

    def _check_interrupt(self) -> None:
        """The cooperative stop point, called before every text delta.

        Deliberately only here. `on_text` runs while the provider is streaming
        and *before* the assistant message is appended to the transcript, so
        raising leaves the transcript well formed. Raising from `on_tool_start`
        would strand the `tool_call_id` the model is waiting on and the next
        request would be rejected outright — `_heal_transcript` exists because
        an interrupt during a tool can still land there by another route.
        """
        if self._interrupt.is_set():
            raise TurnInterrupted()

    def _heal_transcript(self) -> None:
        """Drop a trailing assistant turn whose tool calls were never answered.

        An abandoned turn can leave `messages` ending in an assistant message
        with `tool_calls` and no `tool` message answering them. Every provider
        rejects that request outright, so the *next* thing the user types would
        fail with an error about the turn they already abandoned. Whole units
        only, the same invariant compaction protects.
        """
        messages = self.conversation.messages
        while messages:
            last = messages[-1]
            if last.get("role") == "assistant" and last.get("tool_calls"):
                messages.pop()
                continue
            if last.get("role") == "tool":
                # A tool answer whose call is still above it is fine; walk back
                # only over the answers to find whether the call is complete.
                answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
                call_ids = {
                    call.get("id")
                    for m in messages
                    if m.get("role") == "assistant"
                    for call in (m.get("tool_calls") or [])
                }
                if call_ids - answered:
                    # Some call in this batch never got an answer. Drop the
                    # whole batch back to the last well-formed point.
                    messages.pop()
                    continue
            break

    # ---- shutdown ---------------------------------------------------------

    def shutdown(self) -> int:
        """Release every gate, stop the lanes, stop the background processes.

        Releasing the gates first and under the lock is what makes "the UI died
        while a prompt was open" a refusal rather than a hang: the worker is
        sitting in `Pending.wait()` and only this can wake it.

        Returns how many background processes were stopped, so the surface can
        say so — a dev server left holding a port is the thing people notice.
        """
        with self._lock:
            self._closed = True
            waiting = list(self._pending.values())
            self._pending.clear()
        for pending in waiting:
            pending.release()
        self._interrupt.set()

        registry = getattr(self.conversation, "process_registry", None)
        killed = registry.shutdown_all() if registry else 0
        lanes = getattr(self.conversation, "lane_registry", None)
        if lanes is not None:
            lanes.shutdown()
        for server in getattr(self.conversation, "mcp_servers", None) or []:
            server.close()
        return killed
