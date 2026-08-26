"""Getting through a turn that the network, the provider or the model spoils.

The stock SDK retries twice on a 429 or a 5xx and then gives up, which is the
right default for a library and the wrong one for a session somebody is sitting
in front of. A rate limit that outlasts two retries ends the turn with a stack
of provider prose; a stream that dies at ninety per cent throws away ninety per
cent of an answer; a model that comes back empty is asked the same question
again, and again, at full input cost each time.

Four guards live here, and each one exists because of a distinct failure:

**Retry with jittered backoff.** Honours `Retry-After` when the provider sends
one, and jitters when it does not — several sessions against the same provider
must not all wake at the same instant and re-create the queue they were waiting
out.

**Retry only before the first token.** Once text has reached the terminal it
cannot be unprinted, so a mid-stream failure is reported rather than retried and
the partial answer is kept. A harness that silently restarts a half-printed
answer produces two half-answers stitched together, which is worse than one
honest failure.

**The empty-response guard.** Two empty completions in a row with the same shape
are treated as deterministic: the same prompt will keep producing the same
nothing, and the third attempt only bills for it. One empty is retried, because
a flaky decode is real; the second one stops.

**The repetition guard.** A model that spends its entire output budget echoing
one fragment has not produced a truncated answer worth continuing. Detected
before anything asks it to continue, so the loop stops with a plain reason
instead of stitching more of the same text onto the end.

Every guard fails **open**. A guard that cannot tell what happened lets the turn
proceed the way it would have without it — the cost of a missed rate limit is
one wasted request, and the cost of a false positive is a session that refuses
to answer.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

# Statuses worth trying again. Deliberately narrow: a 400 means the request is
# wrong and will be wrong next time, and a 401 means the credential is wrong.
# Retrying either burns the user's money to reach the same answer.
#
# 409 is here because a provider under contention returns it for "try again",
# and 408 because a proxy that timed out never reached the model at all.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 504})

# How many times a single model call may be re-issued. Three is enough to ride
# out a minute-window rate limit with backoff, and few enough that a provider
# which is genuinely down is reported rather than waited on for ten minutes.
MAX_ATTEMPTS = 3

# The first backoff, in seconds, when the provider does not say. Doubling from
# here reaches ~20s by the third attempt, which is the shape of a per-minute
# quota window.
BASE_DELAY = 5.0

# The ceiling for one wait. A provider asking for longer than this is asking
# for longer than a person will sit still, and the honest answer is to fail and
# say what it asked for.
MAX_DELAY = 60.0

# The longest `Retry-After` that is obeyed rather than reported. Past this, the
# wait is the answer: the turn fails immediately and names the number, so
# nobody watches a silent terminal for a quarter of an hour.
MAX_HONOURED_RETRY_AFTER = 120.0


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

_jitter_counter = 0
_jitter_lock = threading.Lock()


def parse_retry_after(source: Any) -> float | None:
    """Seconds from a `Retry-After` value or a headers mapping, or `None`.

    Both forms of the header are accepted, because both are sent: a count of
    seconds, and an HTTP-date. Reading only the first silently ignores the
    second and backs off on a guess while the provider was being precise.

    Never raises and never returns a negative: a date already in the past means
    "now", not "go back in time".
    """
    raw: Any = source
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except Exception:  # noqa: BLE001 - a header container that misbehaves
            return None

    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))

    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = BASE_DELAY,
    max_delay: float = MAX_DELAY,
    jitter_ratio: float = 0.5,
) -> float:
    """`base * 2^(attempt-1)`, capped, plus jitter. `attempt` is 1-based.

    The jitter is the point. Without it, every session that hit the same rate
    limit at the same moment wakes at the same moment and re-creates it —
    the retries synchronise into exactly the burst the limit exists to stop.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if base_delay <= 0:
        delay = max_delay
    elif exponent >= 32:
        delay = max_delay
    else:
        delay = min(base_delay * (2**exponent), max_delay)

    # Seeded from the clock and a process-local counter, so two threads that
    # reach this in the same nanosecond still draw different numbers.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    return delay + random.Random(seed).uniform(0.0, jitter_ratio * delay)


@dataclass(frozen=True)
class Retry:
    """What to do about one failed attempt."""

    should_retry: bool
    delay: float = 0.0
    reason: str = ""

    def __bool__(self) -> bool:
        return self.should_retry


def plan_retry(
    error: Any,
    attempt: int,
    *,
    streamed: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
) -> Retry:
    """Whether to re-issue this call, and how long to wait first.

    `attempt` is 1-based and counts the call that just failed. `streamed` says
    whether any text has already reached the user — once it has, nothing is
    retried, because the terminal cannot take it back and two partial answers
    stitched together are worse than one that stopped honestly.
    """
    if streamed:
        return Retry(False, reason="output had already started")
    if attempt >= max_attempts:
        return Retry(False, reason=f"gave up after {attempt} attempts")

    status = _status_of(error)
    if status is None or status not in RETRYABLE_STATUS:
        return Retry(False, reason="")

    asked = parse_retry_after(getattr(error, "retry_after", None))
    if asked is None:
        asked = parse_retry_after(_headers_of(error))

    if asked is not None:
        if asked > MAX_HONOURED_RETRY_AFTER:
            return Retry(
                False,
                reason=f"the provider asked for {int(asked)}s, longer than this "
                f"session will wait",
            )
        # A provider that names a number knows its own window; the jitter is
        # still added, because every client it told the same number to would
        # otherwise return together.
        return Retry(
            True,
            delay=asked + random.Random(time.time_ns() & 0xFFFFFFFF).uniform(0.0, 1.0),
            reason=f"HTTP {status}, retrying in {asked:.0f}s as asked",
        )

    delay = jittered_backoff(attempt)
    return Retry(True, delay=delay, reason=f"HTTP {status}, retrying in {delay:.0f}s")


def _status_of(error: Any) -> int | None:
    for attribute in ("status", "status_code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _headers_of(error: Any) -> Mapping[str, str] | None:
    headers = getattr(error, "headers", None)
    if headers is not None:
        return headers
    response = getattr(error, "response", None)
    return getattr(response, "headers", None)


# ---------------------------------------------------------------------------
# Rate-limit headers
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    """One rate-limit window, as the provider last reported it."""

    limit: int = 0
    remaining: int = 0
    reset_seconds: float = 0.0
    captured_at: float = 0.0

    @property
    def known(self) -> bool:
        return self.limit > 0

    @property
    def used(self) -> int:
        return max(0, self.limit - self.remaining)

    @property
    def used_fraction(self) -> float:
        return (self.used / self.limit) if self.limit > 0 else 0.0

    @property
    def resets_in(self) -> float:
        """Seconds until this window resets, from now rather than from capture."""
        if not self.captured_at:
            return 0.0
        return max(0.0, self.reset_seconds - (time.time() - self.captured_at))


@dataclass
class RateLimit:
    """What the provider's `x-ratelimit-*` headers said on the last call.

    Held rather than acted on. This is a read-out, not a governor: refusing to
    send a request because a header says the window is nearly full would make
    the CLI stop working on the strength of a number the provider is free to
    compute however it likes. It answers "why is this slow" and feeds
    `andromeda status`, which is worth exactly as much as it costs — one
    dictionary comprehension per call.
    """

    requests_minute: Bucket = field(default_factory=Bucket)
    requests_hour: Bucket = field(default_factory=Bucket)
    tokens_minute: Bucket = field(default_factory=Bucket)
    tokens_hour: Bucket = field(default_factory=Bucket)
    captured_at: float = 0.0

    @property
    def known(self) -> bool:
        return self.captured_at > 0.0

    @property
    def tightest(self) -> tuple[str, Bucket] | None:
        """The window closest to its limit, or `None` if nothing was reported.

        The one worth showing: a session is throttled by whichever window runs
        out first, and four lines of percentages make the reader do that
        comparison themselves.
        """
        named = [
            ("requests/min", self.requests_minute),
            ("requests/hour", self.requests_hour),
            ("tokens/min", self.tokens_minute),
            ("tokens/hour", self.tokens_hour),
        ]
        known = [(label, bucket) for label, bucket in named if bucket.known]
        if not known:
            return None
        return max(known, key=lambda pair: pair[1].used_fraction)


def parse_rate_limit(headers: Any) -> RateLimit:
    """Read `x-ratelimit-*` headers. An empty `RateLimit` when there are none.

    Empty rather than `None`, so a caller never has to decide what "unknown"
    means twice — `known` is the one question, and it is asked of the object.
    """
    empty = RateLimit()
    if headers is None:
        return empty
    try:
        items = headers.items()
    except AttributeError:
        return empty

    lowered = {str(key).lower(): value for key, value in items}
    if not any(key.startswith("x-ratelimit-") for key in lowered):
        return empty

    now = time.time()

    def bucket(resource: str, suffix: str = "") -> Bucket:
        tag = f"{resource}{suffix}"
        return Bucket(
            limit=_as_int(lowered.get(f"x-ratelimit-limit-{tag}")),
            remaining=_as_int(lowered.get(f"x-ratelimit-remaining-{tag}")),
            reset_seconds=_as_float(lowered.get(f"x-ratelimit-reset-{tag}")),
            captured_at=now,
        )

    return RateLimit(
        requests_minute=bucket("requests"),
        requests_hour=bucket("requests", "-1h"),
        tokens_minute=bucket("tokens"),
        tokens_hour=bucket("tokens", "-1h"),
        captured_at=now,
    )


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The repetition guard
# ---------------------------------------------------------------------------

# Below this, a fragment is too short to judge. A sentence cut mid-word can
# trivially repeat a phrase, and blocking that would block ordinary truncation.
MIN_FRAGMENT = 400

# The exact-repeat window. Sixty characters of verbatim repetition is far past
# ordinary reuse — a heading, a citation, two similar lines of code.
REPEAT_WINDOW = 60

# However dominant, a window has to repeat at least this often to count. Two
# copies of a long block is a table, not a loop.
MIN_REPEATS = 5

# The share of the fragment repeated windows must cover before it is called a
# loop rather than a document with structure in it.
DOMINANCE = 0.5


def is_repetition_dominated(text: Any) -> bool:
    """Whether `text` is mostly one fragment said over and over.

    The signature of a model that has fallen into a loop and spent its whole
    output budget on it. Worth detecting because the natural response to a
    length-truncated answer is to ask for the rest, and asking a looping model
    for the rest buys more of the loop at full price.

    Deliberately conservative, and fails open on anything it cannot judge:
    a false positive refuses to finish an answer the user wanted.
    """
    if not isinstance(text, str):
        return False
    length = len(text)
    if length < MIN_FRAGMENT:
        return False

    # The common shape first: one line repeated. Cheap, and it catches the
    # echo loops that actually happen without building a dictionary of every
    # window in the text.
    counts: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
    for line, count in counts.items():
        if count >= MIN_REPEATS and count * len(line) >= length * DOMINANCE:
            return True

    # The general shape: fixed windows sliding one character at a time, for a
    # loop that does not land on line boundaries.
    needed = max(MIN_REPEATS, math.ceil(length * DOMINANCE / REPEAT_WINDOW))
    windows: dict[str, int] = {}
    for index in range(length - REPEAT_WINDOW + 1):
        key = text[index : index + REPEAT_WINDOW]
        seen = windows.get(key, 0) + 1
        if seen >= needed:
            return True
        windows[key] = seen
    return False


# ---------------------------------------------------------------------------
# The empty-response guard
# ---------------------------------------------------------------------------

# One empty completion is retried; the second identical one is not. A flaky
# decode happens and is worth one more request. The same prompt producing the
# same nothing twice will produce it a third time, at the same input cost.
MAX_EMPTY_RETRIES = 1


@dataclass
class Empties:
    """Consecutive empty completions in one exchange, and what they looked like.

    Reset by any turn that produced something. The streak is what matters:
    an empty turn between two good ones is noise, and two in a row is a prompt
    this model will not answer.
    """

    signatures: list[tuple[str, str]] = field(default_factory=list)

    def record(self, model: str, finish_reason: str) -> None:
        self.signatures.append((model or "", finish_reason or ""))

    def reset(self) -> None:
        self.signatures.clear()

    @property
    def count(self) -> int:
        return len(self.signatures)

    @property
    def deterministic(self) -> bool:
        """Two in a row from the same model with the same finish reason."""
        return (
            len(self.signatures) >= 2
            and self.signatures[-1] == self.signatures[-2]
        )

    def should_retry(self, *, budget: int = MAX_EMPTY_RETRIES) -> bool:
        if self.deterministic:
            return False
        return self.count <= budget


def is_empty_turn(turn: Any) -> bool:
    """A completion that said nothing and asked for nothing.

    Whitespace counts as nothing: a turn whose entire content is a newline is
    an empty turn wearing a character, and treating it as an answer ends the
    exchange with a blank line where a reply should be.
    """
    if turn is None:
        return True
    content = getattr(turn, "content", "") or ""
    calls = getattr(turn, "tool_calls", None) or ()
    return not content.strip() and not calls


# A nudge, not a re-ask. The transcript is unchanged and the same request goes
# out again; this rides as a trailing instruction so the model is told what
# went wrong rather than left to produce the same nothing.
EMPTY_NUDGE = (
    "Your last response was empty. Answer the question, or call a tool. If you "
    "cannot do either, say why in one sentence."
)


__all__ = [
    "BASE_DELAY",
    "Bucket",
    "Empties",
    "MAX_ATTEMPTS",
    "MAX_DELAY",
    "MAX_EMPTY_RETRIES",
    "MAX_HONOURED_RETRY_AFTER",
    "RETRYABLE_STATUS",
    "RateLimit",
    "Retry",
    "EMPTY_NUDGE",
    "is_empty_turn",
    "is_repetition_dominated",
    "jittered_backoff",
    "parse_rate_limit",
    "parse_retry_after",
    "plan_retry",
]
