from __future__ import annotations

import sys

import pytest

from andromeda_tools import Workspace, build_registry, files, terminal
from andromeda_tools.todo import TodoList


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path)


class TestReadFile:
    def test_returns_numbered_lines(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        result = files.read_file(workspace, "a.txt")
        assert "1\tone" in result.content and "2\ttwo" in result.content

    def test_missing_file_is_a_result_not_a_raise(self, workspace):
        result = files.read_file(workspace, "nope.txt")
        assert result.ok is False and "does not exist" in result.content

    def test_a_directory_is_refused_with_a_pointer(self, workspace, tmp_path):
        (tmp_path / "sub").mkdir()
        result = files.read_file(workspace, "sub")
        assert "list_dir" in result.content

    def test_a_binary_file_is_refused(self, workspace, tmp_path):
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
        result = files.read_file(workspace, "b.bin")
        assert result.ok is False and "not a text file" in result.content

    def test_a_window_reports_how_to_continue(self, workspace, tmp_path):
        (tmp_path / "big.txt").write_text("\n".join(str(i) for i in range(100)), encoding="utf-8")
        result = files.read_file(workspace, "big.txt", offset=0, limit=10)
        assert result.metadata["truncated"] is True
        assert "offset 10" in result.content

    def test_an_oversized_file_is_refused_before_it_is_read(self, workspace, tmp_path):
        (tmp_path / "huge.txt").write_text("x" * (files.MAX_READ_BYTES + 1), encoding="utf-8")
        result = files.read_file(workspace, "huge.txt")
        assert result.ok is False and "too large" in result.content

    def test_it_cannot_read_outside_the_workspace(self, workspace):
        result = files.read_file(workspace, "/etc/passwd")
        assert result.ok is False and "outside the workspace" in result.content


class TestWriteFile:
    def test_creates_parents(self, workspace, tmp_path):
        result = files.write_file(workspace, "a/b/c.txt", "hi")
        assert result.ok and (tmp_path / "a/b/c.txt").read_text(encoding="utf-8") == "hi"

    def test_overwrite_reports_the_previous_size(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("original", encoding="utf-8")
        result = files.write_file(workspace, "a.txt", "x")
        assert "Overwrote" in result.content and "8 bytes" in result.content

    def test_it_cannot_write_outside_the_workspace(self, workspace, tmp_path):
        result = files.write_file(workspace, "../escaped.txt", "x")
        assert result.ok is False
        assert not (tmp_path.parent / "escaped.txt").exists()


class TestPatch:
    def test_replaces_a_unique_string(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("alpha beta", encoding="utf-8")
        result = files.patch(workspace, "a.txt", "beta", "gamma")
        assert result.ok
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha gamma"

    def test_an_ambiguous_match_is_refused_with_the_count(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("x\nx\nx\n", encoding="utf-8")
        result = files.patch(workspace, "a.txt", "x", "y")
        assert result.ok is False and "appears 3 times" in result.content
        # Unchanged — an ambiguous edit must not land anywhere.
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x\nx\nx\n"

    def test_replace_all_takes_every_occurrence(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("x\nx\n", encoding="utf-8")
        result = files.patch(workspace, "a.txt", "x", "y", replace_all=True)
        assert result.metadata["replacements"] == 2

    def test_a_missing_string_says_why(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        result = files.patch(workspace, "a.txt", "zeta", "y")
        assert "must match exactly" in result.content

    def test_an_identical_replacement_is_refused(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        assert files.patch(workspace, "a.txt", "a", "a").ok is False


class TestSearch:
    def test_finds_matches_with_path_and_line(self, workspace, tmp_path):
        (tmp_path / "a.py").write_text("import os\nimport sys\n", encoding="utf-8")
        result = files.search_files(workspace, r"^import", glob="*.py")
        assert "a.py:1:" in result.content and "a.py:2:" in result.content

    def test_a_bad_regex_is_a_result_not_a_raise(self, workspace):
        result = files.search_files(workspace, "(unclosed")
        assert result.ok is False and "not a valid regular expression" in result.content

    def test_dotfiles_and_node_modules_are_skipped(self, workspace, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.py").write_text("needle", encoding="utf-8")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "y.py").write_text("needle", encoding="utf-8")
        result = files.search_files(workspace, "needle")
        assert "No matches" in result.content

    def test_no_matches_is_a_success_not_a_failure(self, workspace, tmp_path):
        (tmp_path / "a.txt").write_text("nothing", encoding="utf-8")
        result = files.search_files(workspace, "needle")
        assert result.ok is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
class TestTerminal:
    def test_captures_stdout(self, workspace):
        result = terminal.run_command(workspace, "echo hello")
        assert result.ok and "hello" in result.content

    def test_a_non_zero_exit_is_returned_not_raised(self, workspace):
        result = terminal.run_command(workspace, "exit 3")
        assert result.ok is False
        assert result.metadata["exit_code"] == 3
        assert "[exit 3]" in result.content

    def test_stderr_is_labelled(self, workspace):
        result = terminal.run_command(workspace, "echo oops >&2")
        assert "[stderr]" in result.content and "oops" in result.content

    def test_it_runs_in_the_workspace_root(self, workspace, tmp_path):
        result = terminal.run_command(workspace, "pwd")
        assert str(tmp_path.resolve()) in result.content

    def test_a_timeout_kills_and_reports(self, workspace):
        result = terminal.run_command(workspace, "sleep 30", timeout=1)
        assert result.metadata["timed_out"] is True
        assert "timed out" in result.content

    def test_a_timeout_kills_the_whole_process_group(self, workspace, tmp_path):
        """A child holding the pipe otherwise blocks communicate() forever."""
        marker = tmp_path / "child-still-running"
        command = f"( sleep 20 && touch {marker} ) & sleep 20"
        result = terminal.run_command(workspace, command, timeout=1)
        assert result.metadata["timed_out"] is True
        assert not marker.exists()

    def test_an_empty_command_is_refused(self, workspace):
        assert terminal.run_command(workspace, "   ").ok is False

    def test_the_timeout_is_clamped(self, workspace):
        result = terminal.run_command(workspace, "echo x", timeout=99_999)
        assert result.ok


class TestTodo:
    def test_replaces_the_whole_list(self):
        todos = TodoList()
        todos.replace([{"task": "a", "status": "done"}])
        result = todos.replace([{"task": "b", "status": "pending"}])
        assert todos.items == [{"task": "b", "status": "pending"}]
        assert "b" in result.content

    def test_two_in_progress_is_refused(self):
        result = TodoList().replace(
            [
                {"task": "a", "status": "in_progress"},
                {"task": "b", "status": "in_progress"},
            ]
        )
        assert result.ok is False and "one todo" in result.content

    def test_an_unknown_status_is_refused(self):
        result = TodoList().replace([{"task": "a", "status": "maybe"}])
        assert result.ok is False

    def test_an_empty_task_is_refused(self):
        assert TodoList().replace([{"task": "  ", "status": "pending"}]).ok is False


def test_every_registered_tool_is_on_by_default(tmp_path):
    from andromeda_tools import DEFAULT_ENABLED, MemoryStore

    registry = build_registry(Workspace(tmp_path), TodoList(), {}, MemoryStore(tmp_path))
    assert set(registry) <= set(DEFAULT_ENABLED)


def test_web_search_is_absent_without_a_provider(tmp_path, monkeypatch):
    """Advertising a tool that can only answer "not configured" wastes a turn."""
    from andromeda_tools import MemoryStore, web

    for spec in web.PROVIDERS.values():
        monkeypatch.delenv(spec["env"], raising=False)

    registry = build_registry(Workspace(tmp_path), TodoList(), {}, MemoryStore(tmp_path))
    assert "web_search" not in registry
    assert "web_fetch" in registry


def test_web_search_appears_with_a_provider(tmp_path, monkeypatch):
    from andromeda_tools import MemoryStore

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    registry = build_registry(Workspace(tmp_path), TodoList(), {}, MemoryStore(tmp_path))
    assert "web_search" in registry


def test_memory_tools_are_absent_without_a_store(tmp_path):
    """They bind to a store; an unbound memory tool would fail at call time."""
    registry = build_registry(Workspace(tmp_path), TodoList())
    assert "memory_store" not in registry
    assert "read_file" in registry


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_the_shell_is_not_bound_by_the_workspace_boundary(tmp_path):
    """Pinned because it is a documented limit, not a bug to be discovered.

    Confinement binds the file tools. A shell command is a shell command, and
    no blocklist survives `$(printf ...)`. The containment for `terminal` is its
    `destructive` tier and the approval gate, not a path check.
    """
    outside = tmp_path.parent / "outside_marker.txt"
    outside.write_text("visible", encoding="utf-8")

    result = terminal.run_command(Workspace(tmp_path), f"cat {outside}")

    assert result.ok and "visible" in result.content

    from andromeda_agent.approval import Policy
    from andromeda_tools import build_registry as build

    spec = build(Workspace(tmp_path), TodoList())["terminal"]
    assert spec.risk_tier == "destructive"
    # So the default mode stops for a person, and a pipe never sees it.
    assert Policy(mode="ask", enabled=frozenset({"terminal"})).decide(spec) == "needs_approval"
    assert (
        Policy(mode="ask", enabled=frozenset({"terminal"}), max_tier="safe_local").decide(spec)
        == "denied"
    )
