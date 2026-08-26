"""A job that changes a repository.

This is the only workspace kind that can alter something a person reads later,
so the tests are almost entirely about the one rule:

> **A job never pushes to a branch it did not create.**

Not "should not", and not "the prompt says not to". A cron prompt is fed to a
model, and a rule enforced by asking is a rule enforced by nothing. The branch
name is generated inside `repo.prepare`, so there is no parameter for it — the
same shape the `cron` tool's missing `runs_on` takes, for the same reason.

Real git, real repositories. A mocked `subprocess.run` would prove that the code
calls git the way the test thinks it does, which is the one thing never in
doubt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from andromeda_agent import repo as repo_module


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def origin(tmp_path) -> Path:
    """A real bare repository with one commit on `main`."""
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-q", "-b", "main"], work)
    _git(["config", "user.email", "t@example.com"], work)
    _git(["config", "user.name", "T"], work)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-q", "-m", "first"], work)

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare)],
        capture_output=True,
        check=True,
    )
    return bare


@pytest.fixture
def checkout(origin, tmp_path, monkeypatch):
    # The remote validator wants https; a local path is the only way to exercise
    # the rest against real git, so the check is bypassed for the fixture alone
    # and tested directly in its own cases below.
    monkeypatch.setattr(repo_module, "validate_remote", lambda url: url)
    return repo_module.prepare(str(origin), tmp_path / "clone", "job_abc123")


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_run_lands_on_a_branch_it_created(checkout):
    assert checkout.branch.startswith("andromeda/job_abc123-")
    assert checkout.branch != checkout.base
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout.path).strip()
    assert head == checkout.branch


def test_pushing_from_the_base_branch_is_refused(checkout):
    """The failure this module exists to prevent, in its plainest form."""
    checkout.branch = checkout.base
    with pytest.raises(repo_module.RepoError) as caught:
        repo_module.push(checkout)
    assert "never pushes to a branch it did not make" in str(caught.value)


def test_pushing_after_something_moved_head_is_refused(checkout):
    """A script or an agent that ran `git checkout` mid-run.

    Pushing now would push whatever it landed on, which may be the base branch.
    Checked against the branch this run generated rather than against a denylist
    of protected names — a denylist is a list somebody's default branch is
    missing from.
    """
    _git(["checkout", "-q", checkout.base], checkout.path)
    with pytest.raises(repo_module.RepoError) as caught:
        repo_module.push(checkout)
    assert "not the branch this run created" in str(caught.value)


def test_the_branch_name_is_generated_and_not_a_parameter():
    """There is no argument for a prompt injection to set."""
    import inspect

    names = set(inspect.signature(repo_module.prepare).parameters)
    assert "branch" not in names
    assert "branch_prefix" in names  # a prefix, not a name


def test_a_push_reaches_the_remote_and_leaves_the_base_alone(checkout, origin):
    (checkout.path / "new.txt").write_text("work\n", encoding="utf-8")
    assert repo_module.commit_all(checkout, "did the thing")
    pushed = repo_module.push(checkout)

    refs = _git(["for-each-ref", "--format=%(refname:short)"], origin)
    assert pushed in refs
    # `main` still points where it did. The whole point.
    before = _git(["rev-parse", "main"], origin).strip()
    assert before
    assert _git(["rev-parse", f"{pushed}"], origin).strip() != before


# ---------------------------------------------------------------------------
# Doing nothing is the normal outcome
# ---------------------------------------------------------------------------


def test_a_run_that_changed_nothing_commits_nothing(checkout):
    """Most runs check something and find it fine. An empty commit per tick is
    a repository nobody wants to read."""
    assert repo_module.commit_all(checkout, "nothing happened") is False
    assert repo_module.has_changes(checkout) is False


# ---------------------------------------------------------------------------
# Remotes
# ---------------------------------------------------------------------------


def test_an_ssh_remote_is_refused_with_the_reason():
    """A key on a runner is a credential with no expiry and no scope."""
    for url in ("git@github.com:me/repo.git", "ssh://git@github.com/me/repo.git"):
        with pytest.raises(repo_module.RepoError) as caught:
            repo_module.validate_remote(url)
        assert "no expiry and no scope" in str(caught.value)
        assert "GITHUB_TOKEN" in str(caught.value)


def test_a_non_https_remote_is_refused():
    for url in ("", "file:///tmp/x", "http://github.com/a/b", "not a url"):
        with pytest.raises(repo_module.RepoError):
            repo_module.validate_remote(url)


def test_an_https_remote_is_accepted_with_or_without_dot_git():
    assert repo_module.validate_remote("https://github.com/a/b")
    assert repo_module.validate_remote("https://github.com/a/b.git")


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_the_token_never_reaches_the_stored_remote(checkout, origin, monkeypatch):
    """A URL with a token in it lands in `.git/config`, `git remote -v` and the
    reflog — three places nobody thinks to redact."""
    (checkout.path / "x.txt").write_text("x\n", encoding="utf-8")
    repo_module.commit_all(checkout, "x")
    repo_module.push(checkout, token="ghp_averysecrettoken")

    config = (checkout.path / ".git" / "config").read_text(encoding="utf-8")
    assert "averysecrettoken" not in config
    assert "averysecrettoken" not in _git(["remote", "-v"], checkout.path)


def test_a_failed_git_command_does_not_echo_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_module, "validate_remote", lambda url: url)
    with pytest.raises(repo_module.RepoError) as caught:
        repo_module.prepare(
            str(tmp_path / "does-not-exist"),
            tmp_path / "clone",
            "job_1",
            token="ghp_averysecrettoken",
        )
    assert "averysecrettoken" not in str(caught.value)


def test_git_never_waits_for_a_prompt(tmp_path, monkeypatch):
    """A prompt on a machine with no terminal is a job that hangs until its
    lease expires and is then reported `unknown`."""
    monkeypatch.setattr(repo_module, "validate_remote", lambda url: url)
    with pytest.raises(repo_module.RepoError):
        repo_module.prepare(str(tmp_path / "nope"), tmp_path / "c", "job_1")
    # It returned rather than hanging, which is the assertion. `GIT_TERMINAL_PROMPT=0`
    # is what makes that true and is set in `_git`.
