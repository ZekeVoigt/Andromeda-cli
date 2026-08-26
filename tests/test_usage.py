"""Token accounting: reading it off a response, adding it up, storing it."""

from __future__ import annotations

from pathlib import Path

import pytest

from andromeda_agent import usage as usage_module
from andromeda_agent.approval import Policy
from andromeda_agent.loop import Conversation
from andromeda_agent.providers.base import AssistantTurn
from andromeda_tools import Workspace

from support import ScriptedProvider


class Frame:
    """The shape the SDK hands back on the final streaming frame."""

    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# Reading a usage frame
# ---------------------------------------------------------------------------


def test_the_standard_field_names_are_read() -> None:
    counts = usage_module.from_frame(Frame(prompt_tokens=120, completion_tokens=45))

    assert counts == {"input": 120, "output": 45, "cached": 0, "reasoning": 0}


def test_the_responses_api_names_are_read_too() -> None:
    counts = usage_module.from_frame(Frame(input_tokens=7, output_tokens=3))

    assert counts is not None
    assert counts["input"] == 7


def test_a_plain_dictionary_reads_the_same_as_the_object() -> None:
    """A stored transcript is the former and a live frame is the latter.
    Two readers for one shape is how they drift."""
    assert usage_module.from_frame({"prompt_tokens": 5, "completion_tokens": 1}) == (
        usage_module.from_frame(Frame(prompt_tokens=5, completion_tokens=1))
    )


def test_cached_and_reasoning_come_out_of_their_detail_objects() -> None:
    counts = usage_module.from_frame(
        Frame(
            prompt_tokens=1000,
            completion_tokens=200,
            prompt_tokens_details=Frame(cached_tokens=800),
            completion_tokens_details=Frame(reasoning_tokens=150),
        )
    )

    assert counts == {"input": 1000, "output": 200, "cached": 800, "reasoning": 150}


def test_a_frame_with_nothing_in_it_is_none_rather_than_zeroes() -> None:
    """Zero requests billing nothing is a real state; it must not read as one
    request that cost nothing."""
    assert usage_module.from_frame(None) is None
    assert usage_module.from_frame(Frame()) is None
    assert usage_module.from_frame(Frame(prompt_tokens=0, completion_tokens=0)) is None


def test_nonsense_values_do_not_raise() -> None:
    assert usage_module.from_frame(Frame(prompt_tokens="lots")) is None
    assert usage_module.from_frame(Frame(prompt_tokens=True)) is None


# ---------------------------------------------------------------------------
# Adding it up
# ---------------------------------------------------------------------------


def test_the_total_does_not_double_count_the_subsets() -> None:
    """`cached` is part of input and `reasoning` is part of output. Adding
    them would inflate every total by the cheapest part of the request."""
    usage = usage_module.Usage()
    usage.record("m", input=1000, output=200, cached=900, reasoning=150)

    assert usage.total == 1200


def test_per_model_totals_survive_a_mixed_session() -> None:
    usage = usage_module.Usage()
    usage.record("main", input=100, output=10)
    usage.record("aux", input=50, output=5)
    usage.record("main", input=100, output=10)

    assert usage.by_model["main"]["requests"] == 2
    assert usage.by_model["aux"]["input"] == 50


def test_merging_is_addition() -> None:
    one = usage_module.Usage()
    one.record("m", input=10, output=1)
    two = usage_module.Usage()
    two.record("m", input=20, output=2)

    one.merge(two)

    assert one.requests == 2
    assert one.total == 33
    assert one.by_model["m"]["requests"] == 2


def test_a_round_trip_through_json_keeps_everything() -> None:
    usage = usage_module.Usage()
    usage.record("m", input=10, output=2, cached=5, reasoning=1)

    restored = usage_module.Usage.from_dict(usage.as_dict())

    assert restored == usage


def test_a_corrupted_usage_block_costs_a_figure_and_not_a_session() -> None:
    """A transcript is a file people edit."""
    assert usage_module.Usage.from_dict("nonsense").empty
    assert usage_module.Usage.from_dict({"input": "lots"}).empty
    assert usage_module.Usage.from_dict({"input": -5}).input == 0
    assert usage_module.Usage.from_dict({"by_model": "no"}).by_model == {}


def test_counts_are_readable_at_a_glance() -> None:
    assert usage_module.compact(940) == "940"
    assert usage_module.compact(1_500) == "1.5k"
    assert usage_module.compact(12_000) == "12k"
    assert usage_module.compact(2_400_000) == "2.40M"


# ---------------------------------------------------------------------------
# Through the loop
# ---------------------------------------------------------------------------


def build(script, tmp_path: Path) -> Conversation:
    return Conversation(
        provider=ScriptedProvider(script=list(script)),
        policy=Policy(mode="auto", enabled=frozenset()),
        workspace=Workspace(str(tmp_path)),
        registry={},
    )


def test_a_turn_records_what_the_provider_reported(tmp_path: Path) -> None:
    turn = AssistantTurn(
        content="hello", usage={"input": 900, "output": 30, "cached": 0, "reasoning": 0}
    )
    conversation = build([turn], tmp_path)

    conversation.send("hi")

    assert conversation.usage.requests == 1
    assert conversation.usage.total == 930


def test_a_provider_that_reports_nothing_records_nothing(tmp_path: Path) -> None:
    """Better an empty total than a guessed one."""
    conversation = build(["hello"], tmp_path)

    conversation.send("hi")

    assert conversation.usage.empty


def test_a_retried_empty_response_is_still_counted(tmp_path: Path) -> None:
    """It was billed. A total that counts only the answers people liked is not
    a total."""
    empty = AssistantTurn(content="", usage={"input": 500, "output": 0})
    good = AssistantTurn(content="here", usage={"input": 520, "output": 10})
    conversation = build([empty, good], tmp_path)

    conversation.send("hi")

    assert conversation.usage.requests == 2
    assert conversation.usage.input == 1020


def test_totals_accumulate_across_exchanges(tmp_path: Path) -> None:
    conversation = build(
        [
            AssistantTurn(content="one", usage={"input": 10, "output": 1}),
            AssistantTurn(content="two", usage={"input": 20, "output": 2}),
        ],
        tmp_path,
    )

    conversation.send("a")
    conversation.send("b")

    assert conversation.usage.requests == 2
    assert conversation.usage.total == 33
