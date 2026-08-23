"""The model lock.

The relay enforces its allowlist server-side, so a client cannot get around it.
The BYOK lane has no such backstop — the key is the user's and the request goes
straight to the provider — so the same list is enforced locally, at every place
a model id can enter.
"""

from __future__ import annotations

import re

import pytest
import ts_registry

from andromeda_agent.errors import AgentError
from andromeda_agent.models import ALLOWED_MODEL_IDS, is_allowed
from andromeda_agent.providers import build_provider
from andromeda_cli import config as config_module

SERVED = ALLOWED_MODEL_IDS[0]


def test_the_served_model_is_allowed():
    assert is_allowed(SERVED)


@pytest.mark.parametrize(
    "candidate",
    [
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4",
        # The rolling id specifically: its price row is stale against the live
        # feed, so anything costing against it undercharges.
        "deepseek/deepseek-v4-flash",
        "",
        None,
        123,
    ],
)
def test_everything_else_is_refused(candidate):
    assert is_allowed(candidate) is False


def test_case_and_whitespace_do_not_get_around_it():
    assert is_allowed(f"  {SERVED.upper()}  ")


class TestChokepoints:
    """Every path a model id can take into a request."""

    def test_config_set_refuses(self):
        with pytest.raises(config_module.ConfigError):
            config_module.set_value("model", "openai/gpt-4o")

    def test_a_hand_edited_file_is_caught_on_load(self):
        config_module.config_path().parent.mkdir(parents=True, exist_ok=True)
        config_module.config_path().write_text("model: openai/gpt-4o\n", encoding="utf-8")
        with pytest.raises(config_module.ConfigError):
            config_module.load()

    def test_an_environment_override_is_caught_on_load(self, monkeypatch):
        monkeypatch.setenv("ANDROMEDA_MODEL", "openai/gpt-4o")
        with pytest.raises(config_module.ConfigError):
            config_module.load()

    def test_a_model_flag_is_caught_before_any_request(self, monkeypatch):
        """`--model` never passes through set_value, so the provider gates it."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        config = config_module.load()
        config.update({"provider": "direct", "model": "openai/gpt-4o"})

        with pytest.raises(AgentError) as caught:
            build_provider(config)
        assert "serves" in str(caught.value)

    def test_the_served_model_builds(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        config = config_module.load()
        config.update({"provider": "direct", "model": SERVED})
        assert build_provider(config).model == SERVED


def test_the_local_list_matches_the_relays():
    """Two allowlists that disagree is one of them being wrong."""
    root = ts_registry.repo_root()
    if root is None:
        pytest.skip("running outside the monorepo checkout")

    # `repo_root()` proves the *registry* sources are present, which is a
    # different file from this one. Skip rather than crash on a partial
    # checkout: a missing file here means nothing to compare against, and a
    # FileNotFoundError inside a comparison reads like a real disagreement.
    source = root / "lib/inference-relay/policy.ts"
    if not source.is_file():
        pytest.skip("no relay policy in this checkout")

    policy = source.read_text(encoding="utf-8")
    # From the `= [` rather than from the identifier: the type annotation is
    # `readonly string[]`, whose own `]` would end the slice early.
    start = policy.index("RELAY_ALLOWED_MODEL_IDS")
    opening = policy.index("[", policy.index("=", start))
    block = policy[opening : policy.index("]", opening)]
    theirs = tuple(re.findall(r'"([^"]+)"', block))

    assert theirs, "could not parse RELAY_ALLOWED_MODEL_IDS"
    assert set(theirs) == set(ALLOWED_MODEL_IDS), (
        f"the relay serves {sorted(theirs)} and this build serves "
        f"{sorted(ALLOWED_MODEL_IDS)}"
    )


class TestContextWindow:
    """Getting this wrong is not harmless in either direction."""

    def test_the_served_model_has_its_real_window(self):
        from andromeda_agent.models import CONTEXT_WINDOWS, context_window

        assert context_window(SERVED) == CONTEXT_WINDOWS[SERVED]
        # Not the conservative default: that was the bug — compaction firing
        # ten times earlier than it needed to.
        assert context_window(SERVED) > 1_000_000

    def test_an_unknown_model_gets_the_conservative_default(self):
        from andromeda_agent.models import DEFAULT_CONTEXT_WINDOW, context_window

        assert context_window("something/unknown") == DEFAULT_CONTEXT_WINDOW
        assert context_window(None) == DEFAULT_CONTEXT_WINDOW

    def test_an_explicit_setting_wins(self, monkeypatch):
        """The only way to test compaction without a million-token conversation."""
        from andromeda_cli.session import _window
        from support import ScriptedProvider

        provider = ScriptedProvider(model=SERVED)
        assert _window({"context_window": 5_000}, provider) == 5_000
        assert _window({"context_window": 0}, provider) > 1_000_000
