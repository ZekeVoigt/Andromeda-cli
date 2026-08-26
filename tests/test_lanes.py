"""Concurrent lanes.

Three at a time, staleness measured against progress rather than start time,
and one browser held exclusively for the life of a lane that needs it.
"""

from __future__ import annotations

import threading
import time

import pytest

from andromeda_agent.lanes import (
    MAX_CONCURRENT_LANES,
    STALE_IDLE_SECONDS,
    STALE_IN_TOOL_SECONDS,
    LaneRegistry,
)


@pytest.fixture
def registry():
    live = LaneRegistry()
    yield live
    live.shutdown()


def sleeper(seconds: float, value: str = "ok"):
    def run(_lane):
        time.sleep(seconds)
        return value

    return run


class TestConcurrency:
    def test_lanes_run_alongside_each_other(self, registry):
        started = time.time()
        for index in range(3):
            registry.start("scout", f"L{index}", "task", sleeper(0.3))
        registry.wait(None, timeout=10)

        # Serial would be 0.9s.
        assert time.time() - started < 0.7

    def test_at_most_three_run_at_once(self, registry):
        """The concurrency ceiling, `MAX_CONCURRENT_LANES`."""
        peak = 0
        current = 0
        lock = threading.Lock()

        def run(_lane):
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.2)
            with lock:
                current -= 1
            return "ok"

        for index in range(8):
            registry.start("scout", f"L{index}", "task", run)
        registry.wait(None, timeout=20)

        assert peak <= MAX_CONCURRENT_LANES

    def test_start_returns_immediately(self, registry):
        started = time.time()
        registry.start("scout", "L", "task", sleeper(1.0))
        assert time.time() - started < 0.2


class TestOutcomes:
    def test_a_finished_lane_carries_its_result(self, registry):
        lane = registry.start("scout", "L", "task", sleeper(0.05, "the answer"))
        registry.wait([lane.id], timeout=10)
        assert lane.status == "done" and lane.result == "the answer"

    def test_a_failing_lane_is_recorded_not_raised(self, registry):
        def explode(_lane):
            raise RuntimeError("lane died")

        lane = registry.start("scout", "L", "task", explode)
        registry.wait([lane.id], timeout=10)

        assert lane.status == "failed"
        assert "lane died" in lane.error

    def test_one_failure_does_not_take_the_others_down(self, registry):
        def explode(_lane):
            raise RuntimeError("boom")

        bad = registry.start("scout", "bad", "task", explode)
        good = registry.start("scout", "good", "task", sleeper(0.05))
        registry.wait(None, timeout=10)

        assert bad.status == "failed" and good.status == "done"


class TestWaiting:
    def test_waiting_for_everything_by_default(self, registry):
        lanes = [registry.start("scout", f"L{i}", "t", sleeper(0.05)) for i in range(3)]
        waited = registry.wait(None, timeout=10)
        assert {lane.id for lane in waited} == {lane.id for lane in lanes}
        assert all(lane.status == "done" for lane in lanes)

    def test_waiting_for_a_subset(self, registry):
        first = registry.start("scout", "A", "t", sleeper(0.05))
        registry.start("scout", "B", "t", sleeper(2.0))

        waited = registry.wait([first.id], timeout=10)
        assert [lane.id for lane in waited] == [first.id]

    def test_an_expired_timeout_returns_what_it_has(self, registry):
        lane = registry.start("scout", "slow", "t", sleeper(3.0))
        waited = registry.wait([lane.id], timeout=0.2)
        assert waited and waited[0].status == "running"

    def test_waiting_with_nothing_running_is_not_an_error(self, registry):
        assert registry.wait(None, timeout=1) == []

    def test_an_unknown_id_waits_for_nothing(self, registry):
        assert registry.wait(["nope"], timeout=1) == []


class TestStaleness:
    def test_a_fresh_lane_is_not_stale(self, registry):
        lane = registry.start("scout", "L", "t", sleeper(0.4))
        assert lane.is_stale is False
        registry.wait(None, timeout=10)

    def test_staleness_is_measured_from_progress_not_from_start(self, registry):
        """A lane 15 minutes into honest work is not stalled."""
        lane = registry.start("scout", "L", "t", sleeper(0.05))
        registry.wait(None, timeout=10)

        lane.status = "running"
        lane.started_at = time.time() - 10_000
        lane.last_progress_at = time.time()
        assert lane.is_stale is False

    def test_no_progress_outside_a_tool_goes_stale(self, registry):
        lane = registry.start("scout", "L", "t", sleeper(0.05))
        registry.wait(None, timeout=10)

        lane.status = "running"
        lane.current_tool = ""
        lane.last_progress_at = time.time() - (STALE_IDLE_SECONDS + 10)
        assert lane.is_stale is True

    def test_a_tool_gets_a_longer_leash(self, registry):
        """A tool can legitimately be slow; a model turn cannot."""
        lane = registry.start("scout", "L", "t", sleeper(0.05))
        registry.wait(None, timeout=10)

        lane.status = "running"
        lane.current_tool = "terminal"
        lane.last_progress_at = time.time() - (STALE_IDLE_SECONDS + 10)
        assert lane.is_stale is False

        lane.last_progress_at = time.time() - (STALE_IN_TOOL_SECONDS + 10)
        assert lane.is_stale is True

    def test_a_finished_lane_is_never_stale(self, registry):
        lane = registry.start("scout", "L", "t", sleeper(0.05))
        registry.wait(None, timeout=10)
        lane.last_progress_at = 0
        assert lane.is_stale is False

    def test_progress_is_recorded_with_the_current_tool(self, registry):
        lane = registry.start("scout", "L", "t", sleeper(0.2))
        registry.note_progress(lane, "read_file")
        assert lane.current_tool == "read_file"
        registry.wait(None, timeout=10)
        # Cleared when it settles, so a finished lane does not look busy.
        assert lane.current_tool == ""


class TestBrowserExclusivity:
    def test_two_browser_lanes_do_not_overlap(self, registry):
        """There is one browser. Two lanes in it is worse than two in a mailbox."""
        overlapping = False
        inside = 0
        lock = threading.Lock()

        def run(_lane):
            nonlocal overlapping, inside
            with lock:
                inside += 1
                if inside > 1:
                    overlapping = True
            time.sleep(0.2)
            with lock:
                inside -= 1
            return "ok"

        for index in range(3):
            registry.start("browser", f"B{index}", "t", run, exclusive_browser=True)
        registry.wait(None, timeout=20)

        assert overlapping is False

    def test_a_browser_lane_does_not_block_a_scout(self, registry):
        registry.start("browser", "B", "t", sleeper(0.4), exclusive_browser=True)
        scout = registry.start("scout", "S", "t", sleeper(0.05))
        registry.wait([scout.id], timeout=10)
        assert scout.status == "done"

    def test_a_failing_browser_lane_releases_the_surface(self, registry):
        def explode(_lane):
            raise RuntimeError("boom")

        bad = registry.start("browser", "bad", "t", explode, exclusive_browser=True)
        registry.wait([bad.id], timeout=10)

        good = registry.start("browser", "good", "t", sleeper(0.05), exclusive_browser=True)
        registry.wait([good.id], timeout=10)
        assert good.status == "done"


class TestListing:
    def test_lanes_are_listed_oldest_first(self, registry):
        first = registry.start("scout", "A", "t", sleeper(0.05))
        second = registry.start("scout", "B", "t", sleeper(0.05))
        registry.wait(None, timeout=10)
        assert [lane.id for lane in registry.all()] == [first.id, second.id]

    def test_active_only_excludes_finished(self, registry):
        done = registry.start("scout", "A", "t", sleeper(0.05))
        registry.wait([done.id], timeout=10)
        running = registry.start("scout", "B", "t", sleeper(1.0))

        assert [lane.id for lane in registry.all(active_only=True)] == [running.id]
        registry.wait(None, timeout=10)

    def test_get_finds_a_lane_by_id(self, registry):
        lane = registry.start("scout", "A", "t", sleeper(0.05))
        assert registry.get(lane.id) is lane
        assert registry.get("nope") is None
        registry.wait(None, timeout=10)

    def test_the_summary_line_is_readable(self, registry):
        lane = registry.start("scout", "map the tools", "t", sleeper(0.05))
        registry.wait(None, timeout=10)
        summary = lane.summary()
        assert lane.id in summary and "scout" in summary and "map the tools" in summary


def test_two_tree_writing_lanes_do_not_run_at_once():
    """Without a worktree each, two builders in one directory interleave their
    edits. The lock is the same one the browser has, for the same reason."""
    import threading
    import time

    registry = LaneRegistry()
    overlapping = []
    running = []
    guard = threading.Lock()

    def body(_lane):
        with guard:
            running.append(1)
            overlapping.append(len(running))
        time.sleep(0.05)
        with guard:
            running.pop()
        return "done"

    lanes = [
        registry.start(
            specialist="builder", label=f"l{index}", task="t", run=body, exclusive="tree"
        )
        for index in range(3)
    ]
    for lane in lanes:
        lane._future.result()

    assert max(overlapping) == 1


def test_lanes_on_different_surfaces_do_run_at_once():
    import threading
    import time

    registry = LaneRegistry()
    started = threading.Event()

    def slow(_lane):
        started.set()
        time.sleep(0.2)
        return "slow"

    def quick(_lane):
        assert started.wait(2)
        return "quick"

    first = registry.start(
        specialist="builder", label="a", task="t", run=slow, exclusive="tree"
    )
    second = registry.start(
        specialist="scout", label="b", task="t", run=quick
    )

    assert second._future.result(timeout=5) == "quick"
    assert first._future.result(timeout=5) == "slow"
