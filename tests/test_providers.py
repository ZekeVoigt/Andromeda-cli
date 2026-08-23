from __future__ import annotations

import pytest
from openai import APIStatusError

from andromeda_agent import errors
from andromeda_agent.providers import build_provider
from andromeda_cli import config as config_module


def _config(**overrides):
    values = config_module.load()
    values.update(overrides)
    return values


def test_relay_lane_refuses_when_unpaired():
    with pytest.raises(errors.NotSignedIn):
        build_provider(_config(provider="relay"))


def test_relay_lane_uses_the_pairing_endpoint_not_the_config_default():
    """The device token was minted by one deployment and is useless to another."""
    config_module.save_credentials(
        config_module.Credentials(
            device_token="t" * 64,
            device_id="cli-abc",
            user_id="user_1",
            base_url="https://paired.test",
        )
    )
    provider = build_provider(_config(provider="relay", base_url="https://other.test"))
    assert str(provider.client.base_url).startswith("https://paired.test/api/inference/v1")


def test_relay_lane_sends_the_device_id_header():
    config_module.save_credentials(
        config_module.Credentials(
            device_token="t" * 64,
            device_id="cli-abc",
            user_id="user_1",
            base_url="https://paired.test",
        )
    )
    provider = build_provider(_config(provider="relay"))
    headers = provider.client.default_headers
    assert headers.get("X-Device-Id") == "cli-abc"


def test_direct_lane_requires_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(errors.AgentError) as caught:
        build_provider(_config(provider="direct"))
    assert "OPENROUTER_API_KEY" in str(caught.value)


def test_direct_lane_builds_with_a_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    provider = build_provider(_config(provider="direct"))
    assert provider.name == "direct"


def test_unknown_lane_is_rejected():
    with pytest.raises(errors.AgentError):
        build_provider(_config(provider="carrier-pigeon"))


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, errors.NotSignedIn),
        (402, errors.OutOfCredit),
        (403, errors.AgentError),
        (503, errors.AgentError),
        (502, errors.AgentError),
    ],
)
def test_status_mapping(status, expected):
    assert isinstance(errors.from_status(status, "msg"), expected)


def test_402_is_the_only_out_of_credit_signal():
    """Depletion is a server flag, never an inferred zero balance."""
    assert isinstance(errors.from_status(402, "no credit"), errors.OutOfCredit)
    assert not isinstance(errors.from_status(403, "no credit"), errors.OutOfCredit)


class TestThinking:
    """The provider carries the setting; `models.reasoning_for` decides the
    field. The mapping itself is tested in `test_thinking.py`, next to the
    model table it depends on."""

    def test_the_setting_reaches_the_provider(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        provider = build_provider(_config(provider="direct", thinking="high"))
        assert provider.thinking == "high"

    def test_it_defaults_to_off(self, monkeypatch):
        """Nothing is sent unless it was asked for."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        provider = build_provider(_config(provider="direct"))
        assert provider.thinking == "off"

    def test_an_invalid_level_is_rejected_at_config_time(self):
        from andromeda_cli import config as config_module

        with pytest.raises(config_module.ConfigError):
            config_module.set_value("thinking", "ludicrous")


