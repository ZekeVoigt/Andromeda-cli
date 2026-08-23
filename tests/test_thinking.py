"""Thinking-level control.

The field is only ever sent to a model that declares it accepts one: some
providers reject the whole request rather than ignoring an unknown field, so a
speculative `reasoning` is a broken turn, not a no-op.
"""

from __future__ import annotations

import pytest

from andromeda_agent.models import (
    ALLOWED_MODEL_IDS,
    THINKING_LEVELS,
    reasoning_for,
    supports_reasoning,
)

SERVED = ALLOWED_MODEL_IDS[0]


class TestSupport:
    def test_the_served_model_can_reason(self):
        assert supports_reasoning(SERVED)

    def test_an_unknown_model_cannot(self):
        assert supports_reasoning("someone/else") is False
        assert supports_reasoning(None) is False


class TestField:
    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_a_real_level_becomes_an_effort(self, level):
        assert reasoning_for(SERVED, level) == {"effort": level}

    def test_off_sends_nothing(self):
        """Not the same as asking for minimal effort."""
        assert reasoning_for(SERVED, "off") is None

    def test_an_unknown_level_sends_nothing(self):
        assert reasoning_for(SERVED, "maximum") is None
        assert reasoning_for(SERVED, "") is None
        assert reasoning_for(SERVED, None) is None

    def test_nothing_is_sent_to_a_model_that_cannot_reason(self):
        assert reasoning_for("someone/else", "high") is None

    def test_case_and_whitespace_are_tolerated(self):
        assert reasoning_for(SERVED, "  HIGH ") == {"effort": "high"}

    def test_every_level_is_covered_by_the_config_enum(self):
        from andromeda_cli.config import VALID_VALUES

        assert set(VALID_VALUES["thinking"]) == set(THINKING_LEVELS)


class TestRequest:
    def _provider(self, thinking: str, model: str = SERVED):
        from andromeda_agent.providers.base import Provider

        captured: dict = {}

        class Client:
            class chat:  # noqa: N801 - mirrors the SDK's shape
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        captured.update(kwargs)
                        return iter(())

        return Provider(
            name="test", model=model, client=Client(), label="Test", thinking=thinking
        ), captured

    def test_the_field_reaches_the_request(self):
        provider, captured = self._provider("high")
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert captured["extra_body"] == {"reasoning": {"effort": "high"}}

    def test_it_travels_in_extra_body_not_as_a_named_argument(self):
        """`reasoning` is an OpenRouter extension; the SDK raises on unknown
        named parameters, so a top-level field is a TypeError, not a no-op."""
        provider, captured = self._provider("high")
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert "reasoning" not in captured

    def test_off_leaves_the_request_alone(self):
        provider, captured = self._provider("off")
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert "extra_body" not in captured

    def test_a_model_that_cannot_reason_never_receives_it(self):
        provider, captured = self._provider("high", model="someone/else")
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert "extra_body" not in captured

    def test_the_default_is_off(self):
        from andromeda_agent.providers.base import Provider

        assert Provider(name="t", model=SERVED, client=None, label="t").thinking == "off"


def test_the_flag_reaches_the_provider():
    from andromeda_cli.__main__ import _config, build_parser

    args = build_parser().parse_args(["--thinking", "medium", "hi"])
    assert _config(args)["thinking"] == "medium"
