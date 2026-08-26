"""One working copy per lane: creation, inspection, pruning, reclaim."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from andromeda_agent import worktrees


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


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


def commit(path: Path, name: str, body: str = "x") -> None:
    (path / name).write_text(body, encoding="utf-8")
    run(["git", "add", name], path)
    run(["git", "commit", "-m", f"add {name}"], path)


# ---------------------------------------------------------------------------
# finding the repository
# ---------------------------------------------------------------------------


def test_the_repo_root_is_found_from_a_subdirectory(repo):
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert Path(worktrees.repo_root(nested)).resolve() == repo.resolve()


def test_a_directory_that_is_not_a_repo_has_no_root(tmp_path):
    assert worktrees.repo_root(tmp_path) is None


def test_a_missing_path_has_no_root():
    assert worktrees.repo_root("/nonexistent/place") is None
    assert worktrees.repo_root(None) is None


# ---------------------------------------------------------------------------
# creating
# ---------------------------------------------------------------------------


def test_a_lane_gets_its_own_checkout(repo):
    worktree = worktrees.create(repo, "abc123")

    assert worktree is not None
    assert Path(worktree.path).is_dir()
    assert (Path(worktree.path) / "README.md").exists()
    assert worktree.branch == "andromeda/lane-abc123"
    assert worktree.base_commit


def test_two_lanes_do_not_share_a_directory(repo):
    first = worktrees.create(repo, "one")
    second = worktrees.create(repo, "two")
    assert first.path != second.path


def test_an_edit_in_one_lane_is_invisible_to_the_other(repo):
    """The whole point. Two lanes writing the same file in the same directory
    is a lane reporting success against a file that no longer says what it
    read."""
    first = worktrees.create(repo, "one")
    second = worktrees.create(repo, "two")

    (Path(first.path) / "README.md").write_text("changed by one\n", encoding="utf-8")

    assert (Path(second.path) / "README.md").read_text() == "hello\n"
    assert (repo / "README.md").read_text() == "hello\n"


def test_the_worktrees_directory_is_ignored(repo):
    worktrees.create(repo, "one")
    assert ".worktrees/" in (repo / ".gitignore").read_text()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    )
    assert ".worktrees" not in status.stdout


def test_the_ignore_entry_is_added_once(repo):
    worktrees.create(repo, "one")
    worktrees.create(repo, "two")
    assert (repo / ".gitignore").read_text().count(".worktrees/") == 1


def test_a_non_repo_gets_no_worktree(tmp_path):
    """Outside git the setting is ignored and lanes share the tree, exactly as
    they did before. A half-applied isolation is worse than none."""
    assert worktrees.create(tmp_path, "one") is None


def test_a_repository_with_no_commits_degrades_quietly(tmp_path, caplog):
    root = tmp_path / "empty"
    root.mkdir()
    run(["git", "init", "-b", "main"], root)
    with caplog.at_level("WARNING"):
        assert worktrees.create(root, "one") is None
    assert "worktree add failed" in caplog.text


def test_a_lane_id_with_a_slash_is_made_safe(repo):
    worktree = worktrees.create(repo, "lane/one")
    assert worktree is not None
    assert "lane-lane-one" in worktree.path


def test_an_unnamed_lane_still_gets_a_worktree(repo):
    assert worktrees.create(repo, "") is not None


# ---------------------------------------------------------------------------
# finalizing
# ---------------------------------------------------------------------------


def test_a_worktree_holding_nothing_is_removed(repo):
    worktree = worktrees.create(repo, "one")
    outcome = worktrees.finalize(worktree)

    assert outcome.pruned is True
    assert outcome.commits == 0
    assert outcome.dirty is False
    assert not Path(worktree.path).exists()

    branches = subprocess.run(
        ["git", "branch", "--list", worktree.branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert branches.stdout.strip() == ""


def test_commits_are_kept_and_counted(repo):
    worktree = worktrees.create(repo, "one")
    commit(Path(worktree.path), "work.txt")

    outcome = worktrees.finalize(worktree)

    assert outcome.commits == 1
    assert outcome.pruned is False
    assert Path(worktree.path).exists()


def test_uncommitted_changes_are_kept(repo):
    worktree = worktrees.create(repo, "one")
    (Path(worktree.path) / "README.md").write_text("half done\n", encoding="utf-8")

    outcome = worktrees.finalize(worktree)

    assert outcome.dirty is True
    assert outcome.pruned is False
    assert Path(worktree.path).exists()


def test_untracked_files_count_as_dirty(repo):
    worktree = worktrees.create(repo, "one")
    (Path(worktree.path) / "scratch.txt").write_text("notes\n", encoding="utf-8")
    assert worktrees.finalize(worktree).dirty is True


def test_prune_can_be_turned_off(repo):
    worktree = worktrees.create(repo, "one")
    outcome = worktrees.finalize(worktree, prune=False)
    assert outcome.pruned is False
    assert Path(worktree.path).exists()


def test_a_worktree_that_is_already_gone_reports_nothing_to_review(repo):
    worktree = worktrees.create(repo, "one")
    worktrees.remove(worktree.root, worktree.path, worktree.branch)

    outcome = worktrees.finalize(worktree)
    assert outcome.pruned is True
    assert outcome.unproven is False


# ---------------------------------------------------------------------------
# the fail-safe
# ---------------------------------------------------------------------------


def test_a_failed_probe_keeps_the_worktree(repo, monkeypatch):
    """A probe that does not answer proves nothing about the tree, and this is
    the code path that deletes things."""
    worktree = worktrees.create(repo, "one")

    real = worktrees.git

    def broken(args, cwd, timeout=30):
        if args[:1] == ["rev-list"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad revision")
        return real(args, cwd, timeout)

    monkeypatch.setattr(worktrees, "git", broken)
    outcome = worktrees.finalize(worktree)

    assert outcome.unproven is True
    assert outcome.pruned is False
    assert Path(worktree.path).exists()
    assert "commits UNKNOWN" in outcome.note


def test_the_note_names_only_what_was_not_measured(repo, monkeypatch):
    """One probe can succeed while the other fails. Calling a measured value
    unknown is its own kind of misreport."""
    worktree = worktrees.create(repo, "one")
    (Path(worktree.path) / "scratch.txt").write_text("notes\n", encoding="utf-8")

    real = worktrees.git

    def broken(args, cwd, timeout=30):
        if args[:1] == ["rev-list"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal")
        return real(args, cwd, timeout)

    monkeypatch.setattr(worktrees, "git", broken)
    outcome = worktrees.finalize(worktree)

    assert "commits UNKNOWN" in outcome.note
    assert "dirty" not in outcome.note.split("UNKNOWN")[0]
    # The measurement that did succeed is still reported as a measurement.
    assert outcome.dirty is True


def test_a_probe_that_raises_keeps_the_worktree(repo, monkeypatch):
    worktree = worktrees.create(repo, "one")

    def raising(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(worktrees, "git", raising)
    outcome = worktrees.finalize(worktree)

    assert outcome.unproven is True
    assert outcome.pruned is False
    assert "inspection raised" in outcome.note


def test_a_missing_base_commit_is_never_pruned(repo):
    """The prune condition reads the commit count, and without a base there is
    no count — only a default that looks like one."""
    worktree = worktrees.create(repo, "one")
    worktree.base_commit = ""

    outcome = worktrees.finalize(worktree)

    assert outcome.unproven is True
    assert outcome.pruned is False
    assert Path(worktree.path).exists()


def test_the_unproven_note_tells_the_parent_where_to_look(repo):
    worktree = worktrees.create(repo, "one")
    worktree.base_commit = ""
    note = worktrees.finalize(worktree).note
    assert worktree.path in note
    assert worktree.branch in note
    assert "before assuming the lane did nothing" in note


def test_removal_takes_the_tree_before_the_branch(repo, monkeypatch):
    """Deleting the branch first and then failing to remove the tree orphans
    commits that were reachable a moment ago."""
    worktree = worktrees.create(repo, "one")
    order: list[str] = []
    real = worktrees.git

    def watched(args, cwd, timeout=30):
        if args[:2] == ["worktree", "remove"]:
            order.append("tree")
        if args[:1] == ["branch"]:
            order.append("branch")
        return real(args, cwd, timeout)

    monkeypatch.setattr(worktrees, "git", watched)
    worktrees.remove(worktree.root, worktree.path, worktree.branch)

    assert order == ["tree", "branch"]


def test_a_branch_survives_a_failed_tree_removal(repo, monkeypatch):
    worktree = worktrees.create(repo, "one")
    real = worktrees.git

    def refusing(args, cwd, timeout=30):
        if args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, "", "is dirty")
        return real(args, cwd, timeout)

    monkeypatch.setattr(worktrees, "git", refusing)
    assert worktrees.remove(worktree.root, worktree.path, worktree.branch) is False

    branches = subprocess.run(
        ["git", "branch", "--list", worktree.branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert worktree.branch in branches.stdout


# ---------------------------------------------------------------------------
# what the parent reads
# ---------------------------------------------------------------------------


def test_the_summary_of_a_pruned_worktree(repo):
    worktree = worktrees.create(repo, "one")
    assert worktrees.finalize(worktree).summary() == "worktree discarded (no changes)"


def test_the_summary_names_the_branch_and_the_work(repo):
    worktree = worktrees.create(repo, "one")
    commit(Path(worktree.path), "work.txt")
    summary = worktrees.finalize(worktree).summary()
    assert worktree.branch in summary
    assert "1 commit" in summary


def test_the_summary_of_an_unproven_outcome_carries_the_warning(repo):
    worktree = worktrees.create(repo, "one")
    worktree.base_commit = ""
    assert "UNKNOWN" in worktrees.finalize(worktree).summary()


def test_the_outcome_serialises_for_the_tool_result(repo):
    worktree = worktrees.create(repo, "one")
    payload = worktrees.finalize(worktree).as_dict()
    assert set(payload) == {
        "path",
        "branch",
        "commits",
        "dirty",
        "pruned",
        "unproven",
        "note",
    }


def test_the_brief_names_the_directory_and_the_branch(repo):
    worktree = worktrees.create(repo, "one")
    note = worktrees.brief_note(worktree)
    assert worktree.path in note
    assert worktree.branch in note
    assert "Commit what you finish" in note


# ---------------------------------------------------------------------------
# reclaiming
# ---------------------------------------------------------------------------


def test_an_audit_of_a_repo_with_no_worktrees_is_empty(repo):
    assert worktrees.audit(repo) == []


def test_a_clean_merged_worktree_is_reapable(repo):
    worktrees.create(repo, "one")
    records = worktrees.audit(repo)
    assert [record.verdict for record in records] == ["reap"]
    assert "clean" in records[0].reason


def test_a_worktree_with_unique_commits_is_kept(repo):
    worktree = worktrees.create(repo, "one")
    commit(Path(worktree.path), "work.txt")

    records = worktrees.audit(repo)

    assert records[0].verdict == "keep"
    assert "exist nowhere else" in records[0].reason


def test_a_worktree_with_tracked_edits_is_kept(repo):
    worktree = worktrees.create(repo, "one")
    (Path(worktree.path) / "README.md").write_text("edited\n", encoding="utf-8")

    records = worktrees.audit(repo)

    assert records[0].verdict == "keep"
    assert "tracked files" in records[0].reason


def test_a_worktree_with_only_untracked_scratch_is_kept(repo):
    """Nobody else has those files. Deleting them because they were never
    committed is exactly the loss the person would not have consented to."""
    worktree = worktrees.create(repo, "one")
    (Path(worktree.path) / "notes.md").write_text("draft\n", encoding="utf-8")

    records = worktrees.audit(repo)

    assert records[0].verdict == "keep"
    assert "untracked" in records[0].reason


def test_an_unreadable_worktree_is_kept(repo, monkeypatch):
    worktrees.create(repo, "one")
    real = worktrees.git

    def refusing(args, cwd, timeout=30):
        if args[:1] == ["status"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal")
        return real(args, cwd, timeout)

    monkeypatch.setattr(worktrees, "git", refusing)
    records = worktrees.audit(repo)

    assert records[0].verdict == "keep"
    assert "could not read" in records[0].reason


def test_reclaim_removes_only_the_empty_ones(repo):
    empty = worktrees.create(repo, "empty")
    working = worktrees.create(repo, "working")
    commit(Path(working.path), "work.txt")

    actions, kept = worktrees.reclaim(repo)

    assert len(actions) == 1
    assert "lane-empty" in actions[0]
    assert not Path(empty.path).exists()
    assert Path(working.path).exists()
    assert [record.name for record in kept] == ["lane-working"]


def test_a_dry_run_changes_nothing(repo):
    empty = worktrees.create(repo, "empty")

    actions, _ = worktrees.reclaim(repo, dry_run=True)

    assert actions == ["would remove lane-empty (andromeda/lane-empty)"]
    assert Path(empty.path).exists()


def test_a_failed_removal_is_reported_as_kept(repo, monkeypatch):
    worktrees.create(repo, "one")
    monkeypatch.setattr(worktrees, "remove", lambda *args, **kwargs: False)

    actions, kept = worktrees.reclaim(repo)

    assert actions == []
    assert kept[0].reason == "removal failed"
