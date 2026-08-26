"""What a session actually spent, counted rather than estimated.

Until this existed there was no answer to "how much did that cost me". The
balance headers on the relay lane say what an *account* has left, which is a
different question and is unavailable entirely on the BYOK lane. Nothing
recorded tokens, so nothing could say which session, which model, or which day
the spend went to.

One decision governs the whole module: **tokens are measured, money is not
inferred.** There is no price table here and there must never be one. A local
table drifts the moment a provider changes a rate, and a cost figure that is
quietly wrong is worse than no cost figure — somebody plans against it.

Deriving a per-request cost from the account balance moving between requests
was tried and dropped, because the relay settles a reservation when a stream
*ends* and reports the balance in the headers that arrive when it *starts* —
so the difference between two requests is the previous request's cost wearing
this one's name. Money therefore appears in one place only: the account
balance, reported as the account balance, straight from the server.

The counts come from the provider's final usage frame, which is authoritative
and free: it rides on the response that was already paid for. Estimating from
character counts was considered and rejected for the same reason as the price
table.

Usage is stored on the session transcript, which is the source of truth in this
harness — not in the index, which is derived and may be deleted and rebuilt at
any time. Token counts cannot be recovered from a transcript, so an index that
held them would be the one derived thing that was not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# The fields a provider may report. `cached` and `reasoning` are extensions —
# most OpenAI-compatible endpoints omit them, and a zero is then honest: the
# request had no cached prefix and asked for no reasoning.
TOKEN_FIELDS = ("input", "output", "cached", "reasoning")


@dataclass
class Usage:
    """Tokens and requests. No money: see the module docstring.

    Additive by construction, so a session total, a day's total and an
    install's total are all the same operation.
    """

    requests: int = 0
    input: int = 0
    output: int = 0
    # Input tokens the provider served from a cached prefix. Reported by some
    # endpoints and not others; counted separately because it is a *subset* of
    # `input`, not an addition to it, and adding it to the total would
    # double-count the cheapest part of the request.
    cached: int = 0
    # Output tokens spent on reasoning. A subset of `output`, for the same
    # reason.
    reasoning: int = 0
    # Per-model breakdown, so a session that used the auxiliary model for one
    # image does not have it hidden inside a single total.
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Input plus output. Deliberately not including `cached` or
        `reasoning`, which are subsets of the two."""
        return self.input + self.output

    @property
    def empty(self) -> bool:
        return self.requests == 0 and self.total == 0

    def record(
        self,
        model: str,
        *,
        input: int = 0,
        output: int = 0,
        cached: int = 0,
        reasoning: int = 0,
    ) -> None:
        """Add one response's usage."""
        self.requests += 1
        self.input += max(0, input)
        self.output += max(0, output)
        self.cached += max(0, cached)
        self.reasoning += max(0, reasoning)
        slot = self.by_model.setdefault(
            model or "unknown", {"requests": 0, "input": 0, "output": 0}
        )
        slot["requests"] += 1
        slot["input"] += max(0, input)
        slot["output"] += max(0, output)

    def merge(self, other: "Usage") -> None:
        """Fold another total into this one."""
        self.requests += other.requests
        self.input += other.input
        self.output += other.output
        self.cached += other.cached
        self.reasoning += other.reasoning
        for model, counts in other.by_model.items():
            slot = self.by_model.setdefault(
                model, {"requests": 0, "input": 0, "output": 0}
            )
            for key, value in counts.items():
                slot[key] = slot.get(key, 0) + value

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "Usage":
        """Read a stored total back, tolerating anything.

        A transcript is a file on disk that people edit, and a usage block that
        does not parse must cost a figure in a report, never a session that
        will not load.
        """
        if not isinstance(raw, dict):
            return cls()
        usage = cls()
        for name in ("requests", *TOKEN_FIELDS):
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(usage, name, max(0, value))
        by_model = raw.get("by_model")
        if isinstance(by_model, dict):
            for model, counts in by_model.items():
                if isinstance(counts, dict):
                    usage.by_model[str(model)] = {
                        str(key): int(value)
                        for key, value in counts.items()
                        if isinstance(value, int) and not isinstance(value, bool)
                    }
        return usage


def total(items: Iterable[Usage]) -> Usage:
    combined = Usage()
    for item in items:
        combined.merge(item)
    return combined


def from_frame(frame: Any) -> dict[str, int] | None:
    """Read a provider's usage object into plain counts, or `None`.

    Accepts both the SDK's object and a plain dictionary, because the streaming
    frame is the former and a stored transcript is the latter, and writing two
    readers for one shape is how they drift.
    """
    if frame is None:
        return None

    def pick(*names: str) -> int:
        for name in names:
            value = _get(frame, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    counts = {
        "input": pick("prompt_tokens", "input_tokens"),
        "output": pick("completion_tokens", "output_tokens"),
        "cached": 0,
        "reasoning": 0,
    }

    details = _get(frame, "prompt_tokens_details")
    if details is not None:
        value = _get(details, "cached_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            counts["cached"] = int(value)

    details = _get(frame, "completion_tokens_details")
    if details is not None:
        value = _get(details, "reasoning_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            counts["reasoning"] = int(value)

    if not any(counts.values()):
        return None
    return counts


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def compact(count: int) -> str:
    """A token count a person can read at a glance."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.2f}M".replace(".00M", "M")


__all__ = ["TOKEN_FIELDS", "Usage", "compact", "from_frame", "total"]
