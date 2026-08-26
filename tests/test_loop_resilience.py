"""The turn loop's behaviour when the provider or the model misbehaves."""

from __future__ import annotations

from pathlib import Path

import pytest

from andromeda_agent import resilience
from andromeda_agent.approval import Policy
from andromeda_agent.errors import AgentError
from andromeda_agent.loop import Callbacks, Conversation
from andromeda_agent.providers.base import AssistantTurn
from andromeda_tools import Workspace

from support import ScriptedProvider


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Backoff is real seconds. The suite asserts on the decision, not the wait."""
    waited: list[float] = []
    monkeypatch.setattr("andromeda_agent.loop.time.sleep", waited.append)
    return waited


def build(script, tmp_path: Path) -> Conversation:
    return Conversation(
        provider=ScriptedProvider(script=list(script)),
        policy=Policy(mode="auto", enabled=frozenset()),
        workspace=Workspace(str(tmp_path)),
        registry={},
    )


def rate_limited(retry_after=None) -> AgentError:
    return AgentError("slow down", status=429, retry_after=retry_after)


# ---------------------------------------------------------------------------
# Transport retries
# ---------------------------------------------------------------------------


def test_a_rate_limit_is_retried_and_the_turn_completes(tmp_path, no_waiting) -> None:
    conversation = build([rate_limited(), "here you go"], tmp_path)

    answer = conversation.send("hello")

    assert answer == "here you go"
    assert len(no_waiting) == 1


def test_the_user_is_told_why_the_terminal_went_quiet(tmp_path) -> None:
    conversation = build([rate_limited(), "done"], tmp_path)
    said: list[str] = []

    conversation.send("hello", Callbacks(on_retry=said.append))

    assert said
    assert "429" in said[0]


def test_a_failure_that_will_not_change_is_raised_straight_away(tmp_path) -> None:
    conversation = build([AgentError("bad request", status=400)], tmp_path)

    with pytest.raises(AgentError, match="bad request"):
        conversation.send("hello")


def test_it_gives_up_after_the_ceiling(tmp_path, no_waiting) -> None:
    conversation = build([rate_limited()] * 10, tmp_path)

    with pytest.raises(AgentError):
        conversation.send("hello")

    assert len(no_waiting) == resilience.MAX_ATTEMPTS - 1


def test_partial_output_is_kept_when_the_stream_dies(tmp_path) -> None:
    """An answer that died at ninety per cent is worth ninety per cent."""
    failure = AgentError("connection reset", status=0)
    failure.partial = "The three causes are: first, the"
    conversation = build([failure], tmp_path)

    with pytest.raises(AgentError):
        conversation.send("why?")

    assistant = [m for m in conversation.messages if m.get("role") == "assistant"]
    assert assistant
    assert "three causes" in assistant[-1]["content"]


def test_a_partial_answer_is_not_retried_over(tmp_path, no_waiting) -> None:
    """Retrying would print the beginning of the answer a second time."""
    plan = resilience.plan_retry(rate_limited(), attempt=1, streamed=True)

    assert not plan


# ---------------------------------------------------------------------------
# The empty-response guard
# ---------------------------------------------------------------------------


def test_one_empty_response_is_nudged_and_retried(tmp_path) -> None:
    provider = ScriptedProvider(script=["", "the real answer"])
    conversation = Conversation(
        provider=provider,
        policy=Policy(mode="auto", enabled=frozenset()),
        workspace=Workspace(str(tmp_path)),
        registry={},
    )

    answer = conversation.send("hello")

    assert answer == "the real answer"
    # The nudge rode in the request and never in the transcript.
    assert resilience.EMPTY_NUDGE in provider.seen[-1][-1]["content"]
    assert not any(
        resilience.EMPTY_NUDGE in str(m.get("content", ""))
        for m in conversation.messages
    )


def test_two_identical_empties_stop_rather_than_billing_a_third(tmp_path) -> None:
    provider = ScriptedProvider(script=["", "", "never reached"])
    conversation = Conversation(
        provider=provider,
        policy=Policy(mode="auto", enabled=frozenset()),
        workspace=Workspace(str(tmp_path)),
        registry={},
    )

    answer = conversation.send("hello")

    assert "empty response twice" in answer
    assert len(provider.seen) == 2


def test_a_streak_does_not_carry_across_a_good_turn(tmp_path) -> None:
    provider = ScriptedProvider(script=["", "fine", "", "also fine"])
    conversation = Conversation(
        provider=provider,
        policy=Policy(mode="auto", enabled=frozenset()),
        workspace=Workspace(str(tmp_path)),
        registry={},
    )

    assert conversation.send("one") == "fine"
    assert conversation.send("two") == "also fine"


# ---------------------------------------------------------------------------
# The repetition guard
# ---------------------------------------------------------------------------


def test_a_truncated_answer_that_is_a_loop_is_named(tmp_path) -> None:
    looping = AssistantTurn(
        content="Retrying the connection now.\n" * 40, finish_reason="length"
    )
    conversation = build([looping], tmp_path)

    answer = conversation.send("go")

    assert "repeating itself" in answer
    # What it did produce is kept — the start of it is usually a real answer.
    assert "Retrying the connection now." in answer


def test_an_ordinary_truncated_answer_is_left_alone(tmp_path) -> None:
    turn = AssistantTurn(
        content="The first cause is the cache boundary, and the second is",
        finish_reason="length",
    )
    conversation = build([turn], tmp_path)

    answer = conversation.send("go")

    assert "repeating itself" not in answer


def test_a_repetitive_answer_that_finished_normally_is_left_alone(tmp_path) -> None:
    """`finish_reason` is the signal. A deliberate repeated block is content."""
    turn = AssistantTurn(content="tick\n" * 200, finish_reason="stop")
    conversation = build([turn], tmp_path)

    answer = conversation.send("go")

    assert "repeating itself" not in answer
