"""Rewinding a conversation.

The most-felt annoyance in agentic work: the agent goes wrong at step five of
twelve and the only recovery is starting over, re-establishing everything it had
already learned.

A checkpoint is taken before every user turn, so "go back two" means "put me
where I was before the last two things I asked". Cheap, because it is a copy of
a list of dicts — and the transcript is already the whole state.

**Whole units only.** A restore point is always a message index at which the
transcript is well formed: no assistant message with unanswered `tool_calls`.
Every checkpoint is taken at a user-message boundary, which is one by
construction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Enough to undo a bad stretch, bounded so a long session does not hold twenty
# copies of a large transcript.
MAX_CHECKPOINTS = 20


@dataclass
class Checkpoint:
    index: int
    label: str
    taken_at: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def turns(self) -> int:
        return sum(1 for message in self.messages if message.get("role") == "user")

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "takenAt": self.taken_at,
            "messages": self.messages,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Checkpoint | None":
        messages = raw.get("messages")
        if not isinstance(messages, list):
            return None
        return cls(
            index=int(raw.get("index") or 0),
            label=str(raw.get("label") or ""),
            taken_at=float(raw.get("takenAt") or 0),
            messages=[m for m in messages if isinstance(m, dict)],
        )


class CheckpointStack:
    def __init__(self, limit: int = MAX_CHECKPOINTS) -> None:
        self._stack: list[Checkpoint] = []
        self._limit = limit
        self._next_index = 1

    def take(self, messages: list[dict[str, Any]], label: str) -> Checkpoint:
        checkpoint = Checkpoint(
            index=self._next_index,
            label=" ".join(label.split())[:60],
            # A copy, and a copy of each message: restoring must not hand back
            # the same dicts the live transcript went on to mutate.
            messages=[dict(message) for message in messages],
        )
        self._next_index += 1
        self._stack.append(checkpoint)
        del self._stack[: max(0, len(self._stack) - self._limit)]
        return checkpoint

    def all(self) -> list[Checkpoint]:
        return list(self._stack)

    def resolve(self, index: int | None = None) -> Checkpoint | None:
        """`None` means the most recent. An index means that checkpoint."""
        if not self._stack:
            return None
        if index is None:
            return self._stack[-1]
        for checkpoint in reversed(self._stack):
            if checkpoint.index == index:
                return checkpoint
        return None

    def rewind_to(self, checkpoint: Checkpoint) -> list[dict[str, Any]]:
        """Restore, and drop everything taken after it.

        Discarding the later checkpoints is the point: keeping them would let a
        second rewind jump *forward* into a transcript that no longer describes
        what happened.
        """
        self._stack = [item for item in self._stack if item.index <= checkpoint.index]
        return [dict(message) for message in checkpoint.messages]

    def __len__(self) -> int:
        return len(self._stack)

    # ---- persistence ------------------------------------------------------
    #
    # Saved with the session, so `--resume` brings the ability to rewind back
    # with it. A checkpoint stack that dies at the end of a session is only
    # half the feature: the run you most want to undo is often the one you came
    # back to the next morning.

    def to_json(self) -> list[dict[str, Any]]:
        return [checkpoint.to_json() for checkpoint in self._stack]

    @classmethod
    def from_json(cls, raw: Any, limit: int = MAX_CHECKPOINTS) -> "CheckpointStack":
        stack = cls(limit=limit)
        if not isinstance(raw, list):
            return stack
        for item in raw:
            if not isinstance(item, dict):
                continue
            checkpoint = Checkpoint.from_json(item)
            if checkpoint is not None:
                stack._stack.append(checkpoint)
        del stack._stack[: max(0, len(stack._stack) - limit)]
        # Continue numbering above whatever was restored, so a resumed session's
        # indexes never collide with the ones already on screen.
        stack._next_index = max((c.index for c in stack._stack), default=0) + 1
        return stack
