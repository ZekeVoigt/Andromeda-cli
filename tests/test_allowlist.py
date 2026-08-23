"""Learned approvals, and the rules that keep them from becoming a rubber stamp."""

from __future__ import annotations

import os
import stat

import pytest

from andromeda_agent.allowlist import SUGGEST_AFTER, Allowlist
from andromeda_agent.approval import Policy
from andromeda_agent.specialists import SPECIALISTS
from andromeda_tools.spec import ToolSpec


def spec(name: str = "terminal", tier: str = "destructive", category: str = "admin") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        parameters={},
        risk_tier=tier,
        category=category,
        run=lambda: None,
    )


@pytest.fixture
def allowlist(tmp_path):
    return Allowlist(tmp_path / "approvals.json")


class TestPersistence:
    def test_it_round_trips(self, allowlist, tmp_path):
        allowlist.trust("terminal", "destructive", "always_allow")
        reloaded = Allowlist(tmp_path / "approvals.json")
        assert reloaded.entry_for("terminal", "destructive") is not None

    def test_the_file_is_owner_only(self, allowlist):
        """It grants permissions."""
        allowlist.trust("terminal", "destructive", "always_allow")
        assert stat.S_IMODE(os.stat(allowlist.path).st_mode) == 0o600

    def test_a_corrupt_file_grants_nothing(self, allowlist, tmp_path):
        """Failing closed is the only safe direction for a permissions file."""
        allowlist.path.parent.mkdir(parents=True, exist_ok=True)
        allowlist.path.write_text("{not json", encoding="utf-8")
        reloaded = Allowlist(allowlist.path)
        assert reloaded.all() == []

    def test_no_file_grants_nothing(self, tmp_path):
        assert Allowlist(tmp_path / "missing.json").all() == []

    def test_saving_leaves_no_temporary_file(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        assert not allowlist.path.with_suffix(".json.tmp").exists()


class TestTierBinding:
    """The rule that makes learned trust safe."""

    def test_trust_applies_at_the_tier_it_was_learned_at(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        assert allowlist.entry_for("terminal", "destructive") is not None

    def test_trust_does_not_survive_the_tool_becoming_more_dangerous(self, allowlist):
        allowlist.trust("some_tool", "safe_local", "always_allow")
        assert allowlist.entry_for("some_tool", "destructive") is None

    def test_a_tier_change_puts_it_back_at_the_gate(self, allowlist):
        allowlist.trust("some_tool", "safe_local", "always_allow")
        policy = Policy(mode="ask", enabled=frozenset({"some_tool"}), allowlist=allowlist)
        assert policy.decide(spec("some_tool", "destructive")) == "needs_approval"


class TestGateOrdering:
    def test_an_entry_allows_what_would_otherwise_be_gated(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(mode="ask", enabled=frozenset({"terminal"}), allowlist=allowlist)
        assert policy.decide(spec()) == "allowed"

    def test_always_deny_refuses(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_deny")
        policy = Policy(mode="auto", enabled=frozenset({"terminal"}), allowlist=allowlist)
        assert policy.decide(spec()) == "denied"

    def test_an_entry_cannot_reopen_the_ceiling(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(
            mode="ask",
            enabled=frozenset({"terminal"}),
            max_tier="safe_local",
            allowlist=allowlist,
        )
        assert policy.decide(spec()) == "denied"

    def test_an_entry_cannot_reopen_a_belt(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(
            mode="ask",
            enabled=frozenset({"terminal"}),
            allowlist=allowlist,
            specialist=SPECIALISTS["writer"],
        )
        assert policy.decide(spec()) == "denied"

    def test_an_entry_cannot_reopen_a_disabled_tool(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(mode="ask", enabled=frozenset(), allowlist=allowlist)
        assert policy.decide(spec()) == "denied"

    def test_an_explicit_override_beats_a_learned_entry(self, allowlist):
        """The deliberate deviation: a statement beats a habit."""
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(
            mode="ask",
            enabled=frozenset({"terminal"}),
            allowlist=allowlist,
            overrides={"terminal": "denied"},
        )
        assert policy.decide(spec()) == "denied"

    def test_deny_mode_beats_everything(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        policy = Policy(mode="deny", enabled=frozenset({"terminal"}), allowlist=allowlist)
        assert policy.decide(spec()) == "denied"

    def test_learned_trust_does_not_descend_to_a_lane(self, allowlist):
        """Taught about their own turns, not about a lane out of their sight."""
        allowlist.trust("read_file", "safe_local", "always_allow")
        parent = Policy(mode="ask", enabled=frozenset({"read_file"}), allowlist=allowlist)
        assert parent.narrow().allowlist is None


class TestCounting:
    def test_approvals_accumulate(self, allowlist):
        for _ in range(3):
            allowlist.record("terminal", "destructive", approved=True)
        assert allowlist.approvals_of("terminal") == 3

    def test_a_denial_resets_the_run(self, allowlist):
        """Four approvals then a refusal is not a settled habit."""
        for _ in range(4):
            allowlist.record("terminal", "destructive", approved=True)
        allowlist.record("terminal", "destructive", approved=False)
        assert allowlist.approvals_of("terminal") == 0

    def test_a_suggestion_arrives_at_the_threshold(self, allowlist):
        for _ in range(SUGGEST_AFTER - 1):
            allowlist.record("terminal", "destructive", approved=True)
        assert allowlist.should_suggest("terminal") is False

        allowlist.record("terminal", "destructive", approved=True)
        assert allowlist.should_suggest("terminal") is True

    def test_no_suggestion_once_it_is_already_trusted(self, allowlist):
        for _ in range(SUGGEST_AFTER + 2):
            allowlist.record("terminal", "destructive", approved=True)
        allowlist.trust("terminal", "destructive", "always_allow")
        assert allowlist.should_suggest("terminal") is False

    def test_counts_never_promote_on_their_own(self, allowlist):
        """Promotion is always an explicit answer, never an accumulation."""
        for _ in range(SUGGEST_AFTER * 4):
            allowlist.record("terminal", "destructive", approved=True)
        assert allowlist.entry_for("terminal", "destructive") is None


class TestForgetting:
    def test_forget_removes_an_entry(self, allowlist):
        allowlist.trust("terminal", "destructive", "always_allow")
        assert allowlist.forget("terminal") is True
        assert allowlist.entry_for("terminal", "destructive") is None

    def test_forgetting_something_unknown_reports_false(self, allowlist):
        assert allowlist.forget("nope") is False

    def test_clear_removes_everything(self, allowlist):
        allowlist.trust("a", "safe_local", "always_allow")
        allowlist.trust("b", "safe_local", "always_allow")
        assert allowlist.clear() == 2
        assert allowlist.all() == []
