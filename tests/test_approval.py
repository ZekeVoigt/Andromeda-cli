"""The gate. Ordering is the design, so ordering is what is pinned."""

from __future__ import annotations

import pytest

from andromeda_agent.approval import Policy
from andromeda_tools.spec import ToolSpec

ALL = frozenset({"reader", "writer", "nuke"})


def spec(name: str, tier: str, category: str = "write") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        parameters={},
        risk_tier=tier,
        category=category,
        run=lambda: None,
    )


READER = spec("reader", "safe_local", "read")
WRITER = spec("writer", "destructive")
NUKE = spec("nuke", "irreversible")


def test_deny_mode_refuses_everything():
    policy = Policy(mode="deny", enabled=ALL)
    assert policy.decide(READER) == "denied"


def test_a_disabled_tool_cannot_be_approved():
    policy = Policy(mode="ask", enabled=frozenset({"writer"}))
    assert policy.decide(READER) == "denied"


def test_safe_local_runs_without_asking_in_ask_mode():
    assert Policy(mode="ask", enabled=ALL).decide(READER) == "allowed"


def test_destructive_asks_in_ask_mode():
    assert Policy(mode="ask", enabled=ALL).decide(WRITER) == "needs_approval"


def test_auto_mode_does_not_ask():
    assert Policy(mode="auto", enabled=ALL).decide(WRITER) == "allowed"


def test_the_ceiling_denies_above_it_even_in_auto():
    policy = Policy(mode="auto", enabled=ALL, max_tier="destructive")
    assert policy.decide(WRITER) == "allowed"
    assert policy.decide(NUKE) == "denied"


def test_a_session_grant_cannot_reopen_the_ceiling():
    """The rule the ordering exists for.

    A standing allowance is a decision about a tool made by someone watching
    their own turns. It is not a decision that a narrowed context may run that
    tool, so it is read after the ceiling, never before.
    """
    policy = Policy(mode="ask", enabled=ALL, max_tier="outbound")
    granted = policy.grant_for_session("writer")
    assert granted.decide(WRITER) == "denied"


def test_a_session_grant_cannot_reopen_a_disabled_tool():
    policy = Policy(mode="ask", enabled=frozenset({"reader"}))
    granted = policy.grant_for_session("writer")
    assert granted.decide(WRITER) == "denied"


def test_a_session_grant_allows_within_the_ceiling():
    policy = Policy(mode="ask", enabled=ALL, max_tier="destructive")
    assert policy.grant_for_session("writer").decide(WRITER) == "allowed"


def test_an_explicit_override_beats_an_accumulated_grant():
    policy = Policy(
        mode="ask", enabled=ALL, overrides={"writer": "denied"}
    ).grant_for_session("writer")
    assert policy.decide(WRITER) == "denied"


def test_an_override_cannot_beat_the_ceiling():
    policy = Policy(
        mode="ask", enabled=ALL, max_tier="safe_local", overrides={"writer": "allowed"}
    )
    assert policy.decide(WRITER) == "denied"


def test_an_override_cannot_beat_deny_mode():
    policy = Policy(mode="deny", enabled=ALL, overrides={"writer": "allowed"})
    assert policy.decide(WRITER) == "denied"


class TestNarrowing:
    """A child is never more permissive than its parent."""

    parent = Policy(mode="ask", enabled=ALL, max_tier="destructive")

    def test_a_laxer_mode_is_ignored(self):
        assert self.parent.narrow(mode="auto").mode == "ask"

    def test_a_stricter_mode_is_taken(self):
        assert self.parent.narrow(mode="deny").mode == "deny"

    def test_a_higher_ceiling_is_ignored(self):
        assert self.parent.narrow(max_tier="irreversible").max_tier == "destructive"

    def test_a_lower_ceiling_is_taken(self):
        assert self.parent.narrow(max_tier="safe_local").max_tier == "safe_local"

    def test_enabled_is_intersected_never_extended(self):
        child = self.parent.narrow(enabled=frozenset({"writer", "unheard_of"}))
        assert child.enabled == frozenset({"writer"})

    def test_grants_do_not_descend(self):
        """A person approved that tool for the context they were watching."""
        granted = self.parent.grant_for_session("writer")
        assert granted.narrow().session_grants == frozenset()
        assert granted.narrow().decide(WRITER) == "needs_approval"

    def test_narrowing_is_stable_under_repetition(self):
        once = self.parent.narrow(max_tier="safe_local")
        assert once.narrow(max_tier="destructive").max_tier == "safe_local"


@pytest.mark.parametrize("tier", ["safe_local", "outbound", "destructive", "irreversible"])
def test_no_tier_survives_a_disabled_tool(tier):
    policy = Policy(mode="auto", enabled=frozenset())
    assert policy.decide(spec("anything", tier)) == "denied"


class TestSpecialistBelts:
    """A belt is a hard denial, read before anything softer can answer."""

    from andromeda_agent.specialists import SPECIALISTS

    scout = SPECIALISTS["scout"]
    writer = SPECIALISTS["writer"]
    verifier = SPECIALISTS["verifier"]

    def test_a_rejected_tool_is_denied_not_gated(self):
        """The difference between cannot-send and sends-after-a-pause."""
        policy = Policy(mode="ask", enabled=ALL, specialist=self.writer)
        assert policy.decide(WRITER) == "denied"

    def test_a_belt_beats_auto_mode(self):
        policy = Policy(mode="auto", enabled=ALL, specialist=self.writer)
        assert policy.decide(WRITER) == "denied"

    def test_a_belt_beats_an_explicit_override(self):
        policy = Policy(
            mode="auto", enabled=ALL, specialist=self.writer, overrides={"writer": "allowed"}
        )
        assert policy.decide(WRITER) == "denied"

    def test_a_belt_beats_a_session_grant(self):
        policy = Policy(mode="ask", enabled=ALL, specialist=self.writer)
        assert policy.grant_for_session("writer").decide(WRITER) == "denied"

    def test_a_belt_admits_what_it_is_for(self):
        policy = Policy(mode="auto", enabled=ALL, specialist=self.scout)
        assert policy.decide(READER) == "allowed"

    def test_narrowing_cannot_shed_a_belt(self):
        policy = Policy(mode="auto", enabled=ALL, specialist=self.writer)
        assert policy.narrow().specialist is self.writer
        assert policy.narrow(mode="auto").decide(WRITER) == "denied"

    def test_a_belt_can_be_added_by_narrowing(self):
        parent = Policy(mode="auto", enabled=ALL)
        child = parent.narrow(specialist=self.scout)
        assert child.decide(WRITER) == "denied"
        assert parent.decide(WRITER) == "allowed"
