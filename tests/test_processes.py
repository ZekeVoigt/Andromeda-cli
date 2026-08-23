"""Background processes: the thing `terminal` cannot do."""

from __future__ import annotations

import sys
import time

import pytest

from andromeda_tools import Workspace
from andromeda_tools.processes import ProcessRegistry, act

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")


@pytest.fixture
def registry():
    live = ProcessRegistry()
    yield live
    live.shutdown_all()


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestStarting:
    def test_start_returns_immediately(self, registry, workspace):
        started = time.time()
        registry.start(workspace, "sleep 5")
        assert time.time() - started < 0.5

    def test_it_runs_in_the_workspace(self, registry, workspace, tmp_path):
        process = registry.start(workspace, "pwd")
        assert wait_for(lambda: not process.running)
        assert str(tmp_path.resolve()) in "\n".join(process.snapshot())

    def test_output_is_drained_while_it_runs(self, registry, workspace):
        """Reading lazily deadlocks when a child fills the pipe buffer."""
        process = registry.start(workspace, "for i in $(seq 1 2000); do echo $i; done; sleep 5")
        assert wait_for(lambda: len(process.snapshot()) > 1000)
        assert process.running

    def test_stderr_is_interleaved_with_stdout(self, registry, workspace):
        process = registry.start(workspace, "echo out; echo err >&2")
        assert wait_for(lambda: not process.running)
        body = "\n".join(process.snapshot())
        assert "out" in body and "err" in body


class TestPolling:
    def test_poll_returns_only_what_is_new(self, registry, workspace):
        process = registry.start(workspace, "echo first; sleep 0.4; echo second; sleep 3")
        assert wait_for(lambda: "first" in "\n".join(process.snapshot()))

        first = act(registry, "poll", process.id)
        assert "first" in first.content

        assert wait_for(lambda: "second" in "\n".join(process.snapshot()))
        second = act(registry, "poll", process.id)
        assert "second" in second.content
        assert "first" not in second.content

    def test_poll_reports_the_exit_code(self, registry, workspace):
        process = registry.start(workspace, "exit 7")
        assert wait_for(lambda: not process.running)
        result = act(registry, "poll", process.id)
        assert result.metadata["exit_code"] == 7
        assert result.metadata["running"] is False


class TestWaiting:
    def test_wait_blocks_until_it_exits(self, registry, workspace):
        process = registry.start(workspace, "sleep 0.3; echo done")
        result = act(registry, "wait", process.id, timeout=5)
        assert result.ok and "done" in result.content

    def test_an_expired_wait_says_it_is_still_running(self, registry, workspace):
        process = registry.start(workspace, "sleep 5")
        result = act(registry, "wait", process.id, timeout=1)
        assert result.ok is False and "still running" in result.content


class TestKilling:
    def test_kill_stops_it(self, registry, workspace):
        process = registry.start(workspace, "sleep 30")
        assert act(registry, "kill", process.id).ok
        assert wait_for(lambda: not process.running)

    def test_kill_takes_the_whole_tree(self, registry, workspace, tmp_path):
        marker = tmp_path / "child-survived"
        process = registry.start(workspace, f"( sleep 3 && touch {marker} ) & sleep 30")
        assert wait_for(lambda: process.popen.pid is not None)
        act(registry, "kill", process.id)
        time.sleep(4)
        assert not marker.exists()

    def test_killing_a_finished_process_is_not_an_error(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert wait_for(lambda: not process.running)
        assert "already exited" in act(registry, "kill", process.id).content

    def test_shutdown_stops_everything(self, registry, workspace):
        processes = [registry.start(workspace, "sleep 30") for _ in range(3)]
        assert registry.shutdown_all() == 3
        assert wait_for(lambda: all(not p.running for p in processes))


class TestStdin:
    def test_submit_answers_a_prompt(self, registry, workspace):
        process = registry.start(workspace, "read answer; echo you said $answer")
        act(registry, "submit", process.id, data="hello")
        assert wait_for(lambda: not process.running)
        assert "you said hello" in "\n".join(process.snapshot())

    def test_writing_to_a_finished_process_is_refused(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert wait_for(lambda: not process.running)
        assert act(registry, "write", process.id, data="x").ok is False

    def test_close_sends_eof(self, registry, workspace):
        process = registry.start(workspace, "cat; echo closed")
        act(registry, "close", process.id)
        assert wait_for(lambda: not process.running)
        assert "closed" in "\n".join(process.snapshot())


class TestResolution:
    def test_a_full_id_resolves(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert registry.resolve(process.id) is process

    def test_a_prefix_resolves(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert registry.resolve(process.id[:9]) is process

    def test_a_bare_prefix_without_the_proc_prefix_resolves(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert registry.resolve(process.id[len("proc_") :][:4]) is process

    def test_an_unknown_id_resolves_to_nothing(self, registry, workspace):
        assert registry.resolve("zzzz") is None

    def test_an_unknown_id_is_a_readable_error(self, registry, workspace):
        result = act(registry, "poll", "zzzz")
        assert result.ok is False and "list" in result.content


class TestActions:
    def test_list_with_nothing_running(self, registry):
        assert "No background processes" in act(registry, "list").content

    def test_list_shows_started_processes(self, registry, workspace):
        registry.start(workspace, "sleep 5")
        assert "sleep 5" in act(registry, "list").content

    def test_log_returns_output(self, registry, workspace):
        process = registry.start(workspace, "echo alpha")
        assert wait_for(lambda: not process.running)
        assert "alpha" in act(registry, "log", process.id).content

    def test_an_unknown_action_is_refused(self, registry, workspace):
        process = registry.start(workspace, "true")
        assert act(registry, "teleport", process.id).ok is False


class TestTerminalIntegration:
    def test_background_true_returns_a_session_id(self, registry, workspace):
        from andromeda_tools import terminal

        result = terminal.run_command(
            workspace, "sleep 5", background=True, processes=registry
        )
        assert result.ok and result.metadata["background"] is True
        assert registry.resolve(result.metadata["session_id"]) is not None

    def test_background_without_a_registry_is_refused(self, workspace):
        from andromeda_tools import terminal

        result = terminal.run_command(workspace, "sleep 1", background=True)
        assert result.ok is False

    def test_foreground_still_blocks(self, registry, workspace):
        from andromeda_tools import terminal

        result = terminal.run_command(workspace, "echo sync", processes=registry)
        assert "sync" in result.content
        assert "session_id" not in result.metadata
