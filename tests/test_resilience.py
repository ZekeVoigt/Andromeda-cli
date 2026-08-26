"""Retries, rate-limit headers, and the two content guards."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from andromeda_agent import resilience
from andromeda_agent.errors import AgentError, from_status


class FakeError(AgentError):
    """An `AgentError` shaped the way the provider raises one."""

    def __init__(self, status: int, retry_after=None) -> None:
        super().__init__("boom", status=status, retry_after=retry_after)


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def test_a_plain_number_of_seconds() -> None:
    assert resilience.parse_retry_after("12") == 12.0
    assert resilience.parse_retry_after(7) == 7.0
    assert resilience.parse_retry_after(2.5) == 2.5


def test_an_http_date_is_read_too() -> None:
    """Both forms are sent. Reading one silently backs off on a guess."""
    when = datetime.now(timezone.utc) + timedelta(seconds=30)

    seconds = resilience.parse_retry_after(format_datetime(when))

    assert seconds is not None
    assert 25 <= seconds <= 35


def test_a_date_in_the_past_means_now() -> None:
    when = datetime.now(timezone.utc) - timedelta(hours=1)

    assert resilience.parse_retry_after(format_datetime(when)) == 0.0


def test_a_headers_mapping_is_read_case_insensitively() -> None:
    assert resilience.parse_retry_after({"retry-after": "9"}) == 9.0
    assert resilience.parse_retry_after({"Retry-After": "9"}) == 9.0


def test_nothing_usable_is_none_rather_than_zero() -> None:
    """Zero would mean "retry immediately", which is not what absence means."""
    assert resilience.parse_retry_after(None) is None
    assert resilience.parse_retry_after("") is None
    assert resilience.parse_retry_after("soon") is None
    assert resilience.parse_retry_after(True) is None
    assert resilience.parse_retry_after({}) is None
    assert resilience.parse_retry_after(object()) is None


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_doubles_and_is_capped() -> None:
    first = resilience.jittered_backoff(1, base_delay=2, max_delay=100, jitter_ratio=0)
    second = resilience.jittered_backoff(2, base_delay=2, max_delay=100, jitter_ratio=0)
    huge = resilience.jittered_backoff(40, base_delay=2, max_delay=100, jitter_ratio=0)

    assert first == 2
    assert second == 4
    assert huge == 100


def test_backoff_is_jittered() -> None:
    """Without jitter every session that hit one limit retries in lockstep."""
    draws = {
        resilience.jittered_backoff(1, base_delay=10, max_delay=10) for _ in range(20)
    }

    assert len(draws) > 1
    assert all(10.0 <= draw <= 15.0 for draw in draws)


# ---------------------------------------------------------------------------
# Deciding to retry
# ---------------------------------------------------------------------------


def test_a_rate_limit_is_retried() -> None:
    plan = resilience.plan_retry(FakeError(429), attempt=1)

    assert plan
    assert plan.delay > 0
    assert "429" in plan.reason


@pytest.mark.parametrize("status", sorted(resilience.RETRYABLE_STATUS))
def test_every_retryable_status_is_retried(status: int) -> None:
    assert resilience.plan_retry(FakeError(status), attempt=1)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 503])
def test_a_failure_that_will_not_change_is_not_retried(status: int) -> None:
    """Retrying a bad request burns the user's money to reach the same answer."""
    assert not resilience.plan_retry(FakeError(status), attempt=1)


def test_nothing_is_retried_once_output_has_started() -> None:
    """A terminal cannot unprint. Two half-answers is worse than one failure."""
    plan = resilience.plan_retry(FakeError(429), attempt=1, streamed=True)

    assert not plan
    assert "already started" in plan.reason


def test_the_attempt_ceiling_is_honoured() -> None:
    plan = resilience.plan_retry(FakeError(429), attempt=resilience.MAX_ATTEMPTS)

    assert not plan
    assert "gave up" in plan.reason


def test_the_providers_own_number_is_used_when_it_gives_one() -> None:
    plan = resilience.plan_retry(FakeError(429, retry_after=8), attempt=1)

    assert plan
    assert 8.0 <= plan.delay <= 9.0
    assert "as asked" in plan.reason


def test_an_absurd_retry_after_is_reported_rather_than_waited_out() -> None:
    asked = resilience.MAX_HONOURED_RETRY_AFTER + 60

    plan = resilience.plan_retry(FakeError(429, retry_after=asked), attempt=1)

    assert not plan
    assert str(int(asked)) in plan.reason


def test_an_error_with_no_status_is_not_retried() -> None:
    """Unknown means unknown. Fail open by not retrying something unclassified."""
    assert not resilience.plan_retry(AgentError("network went away"), attempt=1)


def test_the_status_survives_the_translation_to_an_agent_error() -> None:
    """`from_status` used to throw the code away, which made retrying guesswork."""
    assert from_status(429, "slow down").status == 429
    assert from_status(502, "").status == 502
    assert resilience.plan_retry(from_status(429, "x"), attempt=1)
    assert not resilience.plan_retry(from_status(402, "x"), attempt=1)


# ---------------------------------------------------------------------------
# Rate-limit headers
# ---------------------------------------------------------------------------


def test_headers_are_parsed_into_windows() -> None:
    state = resilience.parse_rate_limit(
        {
            "X-RateLimit-Limit-Requests": "60",
            "x-ratelimit-remaining-requests": "12",
            "x-ratelimit-reset-requests": "30",
            "x-ratelimit-limit-tokens-1h": "1000000",
            "x-ratelimit-remaining-tokens-1h": "250000",
        }
    )

    assert state.known
    assert state.requests_minute.limit == 60
    assert state.requests_minute.used == 48
    assert state.tokens_hour.limit == 1_000_000
    assert not state.tokens_minute.known


def test_the_tightest_window_is_the_one_worth_showing() -> None:
    state = resilience.parse_rate_limit(
        {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "90",
            "x-ratelimit-limit-tokens": "1000",
            "x-ratelimit-remaining-tokens": "50",
        }
    )

    tightest = state.tightest
    assert tightest is not None
    assert tightest[0] == "tokens/min"


def test_no_headers_is_empty_rather_than_none() -> None:
    assert not resilience.parse_rate_limit(None).known
    assert not resilience.parse_rate_limit({}).known
    assert not resilience.parse_rate_limit({"content-type": "json"}).known
    assert resilience.parse_rate_limit("not a mapping").known is False


def test_a_reset_counts_down_from_now_not_from_capture() -> None:
    bucket = resilience.Bucket(
        limit=10, remaining=1, reset_seconds=30.0, captured_at=time.time() - 10
    )

    assert 19 <= bucket.resets_in <= 21


def test_garbage_header_values_do_not_raise() -> None:
    state = resilience.parse_rate_limit(
        {"x-ratelimit-limit-requests": "lots", "x-ratelimit-reset-requests": None}
    )

    assert state.requests_minute.limit == 0


# ---------------------------------------------------------------------------
# The repetition guard
# ---------------------------------------------------------------------------


def test_a_repeated_line_is_caught() -> None:
    text = ("The connection was reset by the peer and could not be restored.\n" * 40)

    assert resilience.is_repetition_dominated(text)


def test_a_repeat_that_ignores_line_boundaries_is_caught() -> None:
    text = "abcdefghij" * 200

    assert resilience.is_repetition_dominated(text)


def test_ordinary_prose_is_not_a_loop() -> None:
    text = (
        "The parser reads one frame at a time and routes it by id. A response "
        "frame carries no method, so the reader has to match it against the "
        "pending table rather than dispatching on a name. That is the whole "
        "reason the table exists, and it is why a permission answer can arrive "
        "interleaved with everything else on the same pipe without confusing "
        "the reader about which request it belongs to. The alternative would "
        "be a second channel, which is a protocol nobody asked for.\n"
    ) * 2

    assert not resilience.is_repetition_dominated(text)


def test_code_with_similar_lines_is_not_a_loop() -> None:
    text = "\n".join(
        f"    self.field_{n} = arguments.get('field_{n}', default_{n})"
        for n in range(60)
    )

    assert not resilience.is_repetition_dominated(text)


def test_a_short_fragment_is_never_judged() -> None:
    """A sentence cut mid-word can trivially repeat a phrase."""
    assert not resilience.is_repetition_dominated("ha " * 20)


def test_the_guard_fails_open_on_anything_it_cannot_read() -> None:
    assert not resilience.is_repetition_dominated(None)
    assert not resilience.is_repetition_dominated(b"bytes")
    assert not resilience.is_repetition_dominated("")


# ---------------------------------------------------------------------------
# The empty-response guard
# ---------------------------------------------------------------------------


class FakeTurn:
    def __init__(self, content="", tool_calls=(), finish_reason="stop") -> None:
        self.content = content
        self.tool_calls = list(tool_calls)
        self.finish_reason = finish_reason


def test_whitespace_is_empty() -> None:
    assert resilience.is_empty_turn(FakeTurn(content="\n  \n"))
    assert resilience.is_empty_turn(FakeTurn())
    assert resilience.is_empty_turn(None)


def test_a_turn_that_only_calls_a_tool_is_not_empty() -> None:
    assert not resilience.is_empty_turn(FakeTurn(tool_calls=["x"]))


def test_one_empty_is_retried_and_two_identical_ones_are_not() -> None:
    empties = resilience.Empties()

    empties.record("m", "stop")
    assert empties.should_retry()

    empties.record("m", "stop")
    assert empties.deterministic
    assert not empties.should_retry()


def test_two_empties_of_different_shapes_are_not_deterministic() -> None:
    """A different finish reason is a different failure, not the same one twice."""
    empties = resilience.Empties()
    empties.record("m", "stop")
    empties.record("m", "content_filter")

    assert not empties.deterministic


def test_a_streak_ends_at_the_first_real_answer() -> None:
    empties = resilience.Empties()
    empties.record("m", "stop")
    empties.record("m", "stop")

    empties.reset()

    assert empties.count == 0
    assert empties.should_retry()
