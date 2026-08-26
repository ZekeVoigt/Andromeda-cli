"""`approvals test` and `approvals suggest`."""

from __future__ import annotations

import pytest

from andromeda_agent.allowlist import SUGGEST_AFTER, Allowlist
from andromeda_agent.approval import Policy
from andromeda_agent.specialists import SPECIALISTS
from andromeda_cli import config as config_module
from andromeda_cli.commands import approvals
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("approvals")
    return build_registry(Workspace(root), TodoList())


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


def allowlist() -> Allowlist:
    return Allowlist(config_module.home() / "approvals.json")


ALL = frozenset(
    {"read_file", "list_dir", "search_files", "write_file", "patch", "terminal", "todo"}
)


# ---------------------------------------------------------------------------
# the explanation matches the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,tier,enabled",
    [
        ("ask", "destructive", ALL),
        ("auto", "destructive", ALL),
        ("deny", "destructive", ALL),
        ("ask", "safe_local", ALL),
        ("ask", "destructive", frozenset({"read_file"})),
    ],
)
def test_the_explanation_never_disagrees_with_the_decision(registry, mode, tier, enabled):
    """The explanation is re-derived rather than instrumented into the gate,
    so the one thing worth pinning is that the two always agree."""
    policy = Policy(mode=mode, enabled=enabled, max_tier=tier)
    for spec in registry.values():
        decision, reason = approvals.why(spec, policy)
        assert decision == policy.decide(spec), spec.name
        assert reason


def test_a_refusing_mode_is_named(registry):
    policy = Policy(mode="deny", enabled=ALL)
    _, reason = approvals.why(registry["read_file"], policy)
    assert "`deny`" in reason


def test_a_disabled_tool_is_named(registry):
    policy = Policy(mode="ask", enabled=frozenset({"read_file"}))
    _, reason = approvals.why(registry["terminal"], policy)
    assert "switched off" in reason


def test_the_ceiling_is_named(registry):
    policy = Policy(mode="auto", enabled=ALL, max_tier="safe_local")
    decision, reason = approvals.why(registry["terminal"], policy)
    assert decision == "denied"
    assert "above the ceiling" in reason


def test_a_belt_is_named(registry):
    policy = Policy(
        mode="auto", enabled=ALL, max_tier="destructive", specialist=SPECIALISTS["scout"]
    )
    decision, reason = approvals.why(registry["write_file"], policy)
    assert decision == "denied"
    assert "belt does not admit" in reason


def test_an_override_is_named(registry):
    policy = Policy(
        mode="ask", enabled=ALL, max_tier="destructive", overrides={"terminal": "allowed"}
    )
    _, reason = approvals.why(registry["terminal"], policy)
    assert "override" in reason


def test_a_session_grant_is_named(registry):
    policy = Policy(
        mode="ask", enabled=ALL, max_tier="destructive"
    ).grant_for_session("write_file")
    decision, reason = approvals.why(registry["write_file"], policy)
    assert decision == "allowed"
    assert "for this session" in reason


def test_a_learned_entry_is_named(registry, tmp_path):
    store = Allowlist(tmp_path / "approvals.json")
    store.trust("write_file", "destructive", "always_allow")
    policy = Policy(mode="ask", enabled=ALL, max_tier="destructive", allowlist=store)

    decision, reason = approvals.why(registry["write_file"], policy)

    assert decision == "allowed"
    assert "learned entry" in reason


def test_a_learned_entry_at_the_wrong_tier_is_explained(registry, tmp_path):
    """The most confusing verdict this gate produces: trust exists, and does
    not apply, because the tool got more dangerous."""
    store = Allowlist(tmp_path / "approvals.json")
    store.trust("write_file", "safe_local", "always_allow")
    policy = Policy(mode="ask", enabled=ALL, max_tier="destructive", allowlist=store)

    decision, reason = approvals.why(registry["write_file"], policy)

    assert decision == "needs_approval"
    assert "granted at tier safe_local" in reason


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def test_testing_a_tool_reports_the_verdict(capsys):
    assert approvals.test("read_file") == approvals.EXIT_ALLOWED
    out = capsys.readouterr().out
    assert "read_file" in out
    assert "allowed" in out


def test_a_tool_that_stops_for_a_person_exits_two(capsys):
    assert approvals.test("terminal") == approvals.EXIT_ASKS
    assert "needs_approval" in capsys.readouterr().out


def test_a_denied_tool_exits_three(capsys):
    assert approvals.test("terminal", mode="deny") == approvals.EXIT_DENIED
    assert "denied" in capsys.readouterr().out


def test_an_unknown_tool_is_a_usage_error(capsys):
    assert approvals.test("nope") == approvals.EXIT_USAGE
    assert "No tool named" in capsys.readouterr().err


def test_the_mode_can_be_tried_without_changing_anything(capsys):
    before = config_module.load()["approval_mode"]

    approvals.test("terminal", mode="auto")

    assert "allowed" in capsys.readouterr().out
    assert config_module.load()["approval_mode"] == before


def test_testing_runs_nothing(tmp_path, capsys):
    """The point of the command: safe to run against `terminal`."""
    marker = tmp_path / "ran"
    approvals.test("terminal", workspace=str(tmp_path))
    assert not marker.exists()


def test_testing_writes_nothing_down(capsys):
    approvals.test("terminal")
    assert allowlist().all() == []


def test_the_verb_is_reachable_from_argv(capsys):
    assert run(["approvals", "test", "read_file"]) == 0
    assert "read_file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------


def approve(tool: str, times: int, tier: str = "outbound") -> None:
    store = allowlist()
    for _ in range(times):
        store.record(tool, tier, approved=True)


def test_nothing_is_suggested_at_first(capsys):
    assert approvals.suggest() == 0
    assert "nothing you have approved often enough" in capsys.readouterr().out


def test_a_repeatedly_approved_tool_is_proposed(capsys):
    approve("web_fetch", SUGGEST_AFTER)

    assert approvals.suggest() == 0

    out = capsys.readouterr().out
    assert "web_fetch" in out
    assert "--apply" in out


def test_a_destructive_tool_is_never_proposed(capsys):
    """Approving `git status` twenty times says nothing about the next command
    the model puts through the same tool."""
    approve("terminal", SUGGEST_AFTER * 4)

    approvals.suggest()

    out = capsys.readouterr().out
    assert "stays at the gate" in out
    assert "--apply" not in out


def test_a_withheld_tool_is_named_rather_than_hidden(capsys):
    approve("terminal", SUGGEST_AFTER)
    approvals.suggest()
    assert "terminal" in capsys.readouterr().out


def test_a_tool_below_the_threshold_is_not_proposed(capsys):
    approve("web_fetch", SUGGEST_AFTER - 1)
    approvals.suggest()
    assert "web_fetch" not in capsys.readouterr().out


def test_a_tool_already_trusted_is_not_proposed(capsys):
    approve("web_fetch", SUGGEST_AFTER)
    allowlist().trust("web_fetch", "safe_local", "always_allow")

    approvals.suggest()

    assert "nothing you have approved often enough" in capsys.readouterr().out


def test_applying_a_proposal_records_the_trust(capsys):
    approve("web_fetch", SUGGEST_AFTER)
    capsys.readouterr()

    assert approvals.suggest(apply="1") == 0

    # Trusted at the tool's real tier, whatever tier the counts were recorded
    # against — the entry has to describe the tool as it is now.
    entry = allowlist().entry_for("web_fetch", "safe_local")
    assert entry is not None
    assert entry.trust_level == "always_allow"
    assert "will not be asked about again" in capsys.readouterr().out


def test_applying_something_not_listed_is_refused(capsys):
    approve("web_fetch", SUGGEST_AFTER)
    assert approvals.suggest(apply="7") == approvals.EXIT_USAGE
    assert "not one of the numbers" in capsys.readouterr().err


def test_applying_nonsense_is_refused(capsys):
    approve("web_fetch", SUGGEST_AFTER)
    assert approvals.suggest(apply="all of them") == approvals.EXIT_USAGE


def test_applying_with_nothing_proposed_is_refused(capsys):
    assert approvals.suggest(apply="1") == approvals.EXIT_USAGE
    assert "Nothing is being proposed" in capsys.readouterr().err


def test_nothing_is_promoted_without_being_asked(capsys):
    """Learned trust never widens itself — the whole point of the allowlist."""
    approve("web_fetch", SUGGEST_AFTER * 10)

    approvals.suggest()

    assert allowlist().entry_for("web_fetch", "safe_local") is None


def test_suggest_is_reachable_from_argv(capsys):
    assert run(["approvals", "suggest"]) == 0
    assert "nothing you have approved" in capsys.readouterr().out
