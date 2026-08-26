"""Lane isolation through a real session, and `andromeda worktrees`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from andromeda_agent import worktrees
from andromeda_cli import config as config_module
from andromeda_cli.commands import worktrees_cmd
from andromeda_cli.session import build_conversation
from support import ScriptedProvider, call, turn_with


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    run(["git", "config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "first"], root)
    return root


def build(root, script, **overrides):
    config = config_module.load()
    config.update({"approval_mode": "auto", **overrides})
    provider = ScriptedProvider(script=list(script))
    conversation, record = build_conversation(
        config, provider, interactive=True, workspace_root=str(root)
    )
    return conversation, provider, record


def delegate(task: str, specialist: str = "builder", background: bool = False):
    return call(
        "delegate",
        {"task": task, "specialist": specialist, "background": background},
        "call_1",
    )


# ---------------------------------------------------------------------------
# a lane in its own copy
# ---------------------------------------------------------------------------


def test_a_lane_writes_in_its_own_worktree(repo):
    """The parent's checkout is untouched, and the file lands on the lane's
    branch instead."""
    lane_script = [
        turn_with(call("write_file", {"path": "made.txt", "content": "by the lane"})),
        "wrote it",
    ]
    conversation, _, _ = build(
        repo, [turn_with(delegate("write a file")), *lane_script, "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    assert not (repo / "made.txt").exists()
    worktree = next((repo / ".worktrees").iterdir())
    assert (worktree / "made.txt").read_text() == "by the lane"


def test_the_lane_cannot_reach_the_parent_checkout(repo):
    """Confinement does the enforcing, not a sentence in the brief."""
    lane_script = [
        turn_with(call("write_file", {"path": str(repo / "escaped.txt"), "content": "x"})),
        "could not",
    ]
    conversation, _, _ = build(
        repo, [turn_with(delegate("try to escape")), *lane_script, "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    assert not (repo / "escaped.txt").exists()


def test_a_lane_that_wrote_nothing_leaves_nothing_behind(repo):
    conversation, _, _ = build(
        repo, [turn_with(delegate("just look")), "I looked", "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    assert list((repo / ".worktrees").iterdir()) == []


def test_the_parent_is_told_where_the_work_is(repo):
    lane_script = [
        turn_with(call("write_file", {"path": "made.txt", "content": "x"})),
        "wrote it",
    ]
    conversation, _, _ = build(
        repo, [turn_with(delegate("write a file")), *lane_script, "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "worktree" in tool_message["content"]
    assert "andromeda/lane-" in tool_message["content"]
    assert "uncommitted changes" in tool_message["content"]


def test_the_lane_is_told_which_branch_is_its_own(repo):
    conversation, provider, _ = build(
        repo, [turn_with(delegate("look around")), "looked", "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    # The lane's own request carries its brief as the system message.
    briefs = [
        request[0]["content"]
        for request in provider.seen
        if request and "Your working copy" in str(request[0].get("content"))
    ]
    assert briefs
    assert "andromeda/lane-" in briefs[0]


def test_isolation_off_keeps_the_old_behaviour(repo):
    lane_script = [
        turn_with(call("write_file", {"path": "made.txt", "content": "shared"})),
        "wrote it",
    ]
    conversation, _, _ = build(
        repo, [turn_with(delegate("write a file")), *lane_script, "done"]
    )

    conversation.send("go")

    assert (repo / "made.txt").read_text() == "shared"
    assert not (repo / ".worktrees").exists()


def test_isolation_outside_a_repository_is_ignored(tmp_path):
    lane_script = [
        turn_with(call("write_file", {"path": "made.txt", "content": "shared"})),
        "wrote it",
    ]
    conversation, _, _ = build(
        tmp_path, [turn_with(delegate("write a file")), *lane_script, "done"],
        worktree_isolation=True,
    )

    conversation.send("go")

    assert (tmp_path / "made.txt").read_text() == "shared"


def test_two_lanes_do_not_write_over_each_other(repo):
    """Two background lanes, one file name, one repository."""
    first = [turn_with(call("write_file", {"path": "shared.txt", "content": "one"})), "done"]
    second = [turn_with(call("write_file", {"path": "shared.txt", "content": "two"})), "done"]
    conversation, _, _ = build(
        repo,
        [
            turn_with(call("delegate", {"task": "a", "specialist": "builder", "background": False}, "c1")),
            *first,
            turn_with(call("delegate", {"task": "b", "specialist": "builder", "background": False}, "c2")),
            *second,
            "done",
        ],
        worktree_isolation=True,
    )

    conversation.send("go")

    contents = sorted(
        path.read_text()
        for path in (repo / ".worktrees").glob("*/shared.txt")
    )
    assert contents == ["one", "two"]


def test_a_failing_lane_keeps_its_working_copy(repo):
    """The lane that raised is exactly the one whose half-finished work must
    not be swept away."""
    conversation, _, _ = build(
        repo,
        [
            turn_with(delegate("write then fail")),
            turn_with(call("write_file", {"path": "half.txt", "content": "partial"})),
            RuntimeError("the provider fell over mid-lane"),
            "done",
        ],
        worktree_isolation=True,
    )

    conversation.send("go")

    trees = list((repo / ".worktrees").iterdir())
    assert trees, "the lane's copy was removed despite holding a write"
    assert (trees[0] / "half.txt").read_text() == "partial"


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def test_list_says_when_there_is_nothing(repo, capsys):
    assert worktrees_cmd.show_list(str(repo)) == 0
    assert "no lane worktrees" in capsys.readouterr().out


def test_list_outside_a_repository_is_refused(tmp_path, capsys):
    assert worktrees_cmd.show_list(str(tmp_path)) == 2
    assert "Not inside a git repository" in capsys.readouterr().err


def test_list_marks_what_can_go(repo, capsys):
    worktrees.create(repo, "empty")
    keeper = worktrees.create(repo, "busy")
    (Path(keeper.path) / "work.txt").write_text("x", encoding="utf-8")

    assert worktrees_cmd.show_list(str(repo)) == 0

    out = capsys.readouterr().out
    assert "lane-empty" in out
    assert "lane-busy" in out
    assert "1 can be removed" in out


def test_prune_removes_the_empty_one_and_names_what_it_kept(repo, capsys):
    empty = worktrees.create(repo, "empty")
    keeper = worktrees.create(repo, "busy")
    (Path(keeper.path) / "work.txt").write_text("x", encoding="utf-8")

    assert worktrees_cmd.prune(str(repo)) == 0

    out = capsys.readouterr().out
    assert not Path(empty.path).exists()
    assert Path(keeper.path).exists()
    assert "removed lane-empty" in out
    assert "lane-busy" in out
    assert "untracked" in out


def test_a_dry_run_only_says_what_it_would_do(repo, capsys):
    empty = worktrees.create(repo, "empty")

    assert worktrees_cmd.prune(str(repo), dry_run=True) == 0

    assert Path(empty.path).exists()
    assert "would be removed" in capsys.readouterr().out


def test_prune_with_nothing_to_do(repo, capsys):
    assert worktrees_cmd.prune(str(repo)) == 0
    assert "nothing to reclaim" in capsys.readouterr().out


def test_the_verb_is_reachable_from_argv(repo, capsys, monkeypatch):
    from andromeda_cli.__main__ import main

    monkeypatch.chdir(repo)
    assert main(["worktrees", "list"]) == 0
    assert "no lane worktrees" in capsys.readouterr().out


def test_worktree_isolation_is_a_real_setting():
    assert config_module.load()["worktree_isolation"] is False
