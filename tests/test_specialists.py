"""The belts themselves, and the predicates they are written in terms of."""

from __future__ import annotations

import pytest

from andromeda_agent.specialists import (
    SPECIALISTS,
    Specialist,
    is_egress,
    is_read_only,
    is_session_tool,
    resolve,
)
from andromeda_tools import MemoryStore, Workspace, build_registry
from andromeda_tools.todo import TodoList


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("belts")
    return build_registry(Workspace(root), TodoList(), {}, MemoryStore(root))


class TestPredicates:
    def test_read_only_needs_both_category_and_tier(self, registry):
        assert is_read_only(registry["read_file"])
        # safe_local but category write — the case that proves both halves.
        assert not is_read_only(registry["memory_store"])
        assert not is_read_only(registry["terminal"])

    def test_web_reads_are_egress_despite_being_safe_local(self, registry):
        from andromeda_tools import web

        spec = registry.get("web_fetch")
        if spec is None:
            pytest.skip("web tools not registered in this fixture")
        assert spec.risk_tier == "safe_local"
        assert is_egress(spec)

    def test_anything_above_safe_local_is_egress(self, registry):
        assert is_egress(registry["terminal"])
        assert is_egress(registry["write_file"])

    def test_local_reads_are_not_egress(self, registry):
        assert not is_egress(registry["read_file"])


class TestScout:
    belt = SPECIALISTS["scout"]

    def test_admits_reads(self, registry):
        assert self.belt.admits(registry["read_file"])
        assert self.belt.admits(registry["search_files"])
        assert self.belt.admits(registry["memory_search"])

    def test_changes_nothing(self, registry):
        for name in ("write_file", "patch", "terminal", "memory_store", "memory_forget"):
            assert not self.belt.admits(registry[name]), name


class TestWriter:
    belt = SPECIALISTS["writer"]

    def test_cannot_reach_the_network(self, registry):
        for name in ("web_fetch", "web_search"):
            spec = registry.get(name)
            if spec is not None:
                assert not self.belt.admits(spec), name

    def test_cannot_write_memory(self, registry):
        assert not self.belt.admits(registry["memory_store"])

    def test_can_still_read_local_files(self, registry):
        assert self.belt.admits(registry["read_file"])


class TestVerifier:
    belt = SPECIALISTS["verifier"]

    def test_cannot_store_memory(self, registry):
        """A checker that can store facts outlives the run it was hired for."""
        assert not self.belt.admits(registry["memory_store"])

    def test_reads_the_world(self, registry):
        assert self.belt.admits(registry["read_file"])
        assert self.belt.admits(registry["memory_search"])

    def test_changes_nothing(self, registry):
        for name in ("write_file", "patch", "terminal"):
            assert not self.belt.admits(registry[name]), name


def test_no_specialist_can_spawn():
    """Depth stops at one. Raising it must be a visible edit."""
    assert all(not belt.can_spawn for belt in SPECIALISTS.values())


def test_every_specialist_has_a_budget():
    assert all(belt.max_turns > 0 for belt in SPECIALISTS.values())


def test_resolve_is_case_insensitive_and_forgiving():
    assert resolve("SCOUT") is SPECIALISTS["scout"]
    assert resolve("  writer ") is SPECIALISTS["writer"]
    assert resolve("nonsense") is None
    assert resolve("") is None


@pytest.fixture(scope="module")
def browser_registry(tmp_path_factory):
    """A registry that includes the browser family, when it can be built."""
    from andromeda_tools import BrowserSession, browser

    if not browser.playwright_available():
        pytest.skip("Playwright is not installed")
    root = tmp_path_factory.mktemp("belts-browser")
    return build_registry(
        Workspace(root), TodoList(), {}, MemoryStore(root), browser=BrowserSession()
    )


class TestBrowserIsOneSurface:
    """Two lanes driving one browser is worse than two in one mailbox:
    neither can see that it is happening."""

    def test_only_the_browser_belt_admits_the_browser_family(self, browser_registry):
        names = [n for n in browser_registry if n.startswith("browser_")]
        assert names, "the fixture built no browser tools"

        for name in names:
            spec = browser_registry[name]
            assert SPECIALISTS["browser"].admits(spec), name
            for other in ("scout", "writer", "verifier"):
                assert not SPECIALISTS[other].admits(spec), f"{other} admits {name}"

    def test_the_browser_belt_can_still_read_local_files(self, browser_registry):
        assert SPECIALISTS["browser"].admits(browser_registry["read_file"])

    def test_the_browser_belt_cannot_write_or_shell(self, browser_registry):
        for name in ("write_file", "patch", "terminal", "memory_store"):
            assert not SPECIALISTS["browser"].admits(browser_registry[name]), name

    def test_browser_tools_count_as_egress(self, browser_registry):
        from andromeda_agent.specialists import is_browser_tool, is_egress

        spec = browser_registry["browser_navigate"]
        assert is_browser_tool(spec) and is_egress(spec)

    def test_the_browser_belt_has_the_largest_budget(self):
        assert SPECIALISTS["browser"].max_turns == max(
            belt.max_turns for belt in SPECIALISTS.values()
        )
