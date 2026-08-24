"""Updating, and specifically not half-updating.

The failure mode this guards is the one the reference implementation carries a
whole repair module for: git moves to the new revision, the dependency install
fails, and the next run is new code against old packages — usually an
ImportError before the CLI can print anything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from andromeda_cli.commands import update as update_cmd


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An origin and a clone of it, with one commit ahead on origin."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "Test")
    (origin / "cli").mkdir()
    (origin / "cli" / "marker.txt").write_text("v1", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "first")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(clone)], check=True, capture_output=True
    )
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "Test")

    (origin / "cli" / "marker.txt").write_text("v2", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "--quiet", "-m", "second")

    monkeypatch.setattr(update_cmd, "install_root", lambda: clone)
    return clone


def stub_install(monkeypatch, returncode: int):
    """Intercept the dependency install, whatever shape it takes.

    Dispatches on "is this git?" rather than on the installer's argv. The
    previous version matched `[sys.executable, "-m", "pip"]` exactly, so when
    the install switched to `uv` these tests silently started running the real
    installer against a fixture repo — and failed for a reason that had nothing
    to do with what they assert. A stub that knows the shape of the thing it
    stubs breaks every time that shape legitimately changes.
    """
    calls = []
    real_run = subprocess.run

    def dispatch(command, **kwargs):
        if command and command[0] == "git":
            return real_run(command, **kwargs)
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="boom")

    monkeypatch.setattr(update_cmd.subprocess, "run", dispatch)
    return calls


def test_a_successful_update_moves_to_origin(repo, monkeypatch):
    stub_install(monkeypatch, 0)
    assert update_cmd.run() == 0
    assert (repo / "cli" / "marker.txt").read_text(encoding="utf-8") == "v2"


def test_a_failed_install_rolls_the_checkout_back(repo, monkeypatch, capsys):
    """The whole point. A failed update leaves the install working."""
    before = git(repo, "rev-parse", "HEAD")
    stub_install(monkeypatch, 1)

    assert update_cmd.run() == 1

    assert git(repo, "rev-parse", "HEAD") == before
    assert (repo / "cli" / "marker.txt").read_text(encoding="utf-8") == "v1"
    assert "Rolled back" in capsys.readouterr().out


def test_being_up_to_date_is_a_success(repo, monkeypatch):
    stub_install(monkeypatch, 0)
    update_cmd.run()
    assert update_cmd.run() == 0


def test_check_reports_without_changing_anything(repo, monkeypatch, capsys):
    before = git(repo, "rev-parse", "HEAD")
    stub_install(monkeypatch, 0)

    assert update_cmd.run(check_only=True) == 0
    assert git(repo, "rev-parse", "HEAD") == before
    assert "1 commit" in capsys.readouterr().out


def test_a_dirty_checkout_is_refused(repo, monkeypatch, capsys):
    """Resetting over someone's edits is not an update, it is data loss."""
    (repo / "cli" / "marker.txt").write_text("local work", encoding="utf-8")
    stub_install(monkeypatch, 0)

    assert update_cmd.run() == 1
    assert (repo / "cli" / "marker.txt").read_text(encoding="utf-8") == "local work"
    assert "uncommitted changes" in capsys.readouterr().err


def test_a_non_checkout_is_reported_not_crashed(monkeypatch, capsys):
    monkeypatch.setattr(update_cmd, "install_root", lambda: None)
    assert update_cmd.run() == 1
    assert "git checkout" in capsys.readouterr().err


def test_install_root_finds_the_repository():
    """Resolved from the package, so `update` works from any directory."""
    root = update_cmd.install_root()
    assert root is not None
    assert (update_cmd.package_dir(root) / "pyproject.toml").exists()


class TestTheLayoutProbe:
    """Both shapes install: the distribution repo and a monorepo checkout.

    Asserted rather than assumed because the two are published from one source
    — the distribution repo is a flattened copy of `cli/` — so a path that
    hardcodes either shape works on the machine it was written on and breaks
    for everyone installing the other way.
    """

    def test_a_monorepo_checkout_resolves_to_the_subdirectory(self, tmp_path):
        (tmp_path / "cli").mkdir()
        (tmp_path / "cli" / "pyproject.toml").write_text("", encoding="utf-8")
        assert update_cmd.package_dir(tmp_path) == tmp_path / "cli"

    def test_a_flat_checkout_resolves_to_the_root(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        assert update_cmd.package_dir(tmp_path) == tmp_path

    def test_a_bare_cli_directory_does_not_win(self, tmp_path):
        """The marker is the `pyproject.toml`, not the directory name.

        A flat checkout is free to carry a `cli/` directory of its own — the
        installer scripts live in one. Keying on the name alone would resolve
        the package to a directory that does not contain it.
        """
        (tmp_path / "cli").mkdir()
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        assert update_cmd.package_dir(tmp_path) == tmp_path


class TestTheInstallCommand:
    """Which installer `update` shells out to.

    This is the check that would have caught a command that could never
    succeed. The installer builds the venv with `uv venv`, which does not
    include pip, so `python -m pip` fails with "No module named pip" on every
    install this project produces. `update` used it anyway: it reset to the new
    revision, failed to install, rolled back, and truthfully reported that the
    install still worked — at the old version, permanently.

    It survived because a development checkout has pip and a real install does
    not, so it worked for everyone who could have noticed.
    """

    def test_it_prefers_uv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(update_cmd.shutil, "which", lambda name: "/usr/bin/uv")
        command = update_cmd._install_command(tmp_path)

        assert command[0] == "/usr/bin/uv"
        assert command[1:3] == ["pip", "install"]
        assert "--python" in command, "uv must be told which interpreter to install into"

    def test_it_targets_this_interpreter(self, tmp_path, monkeypatch):
        """Not whatever `uv` would pick on its own.

        Without `--python`, uv resolves an interpreter from the environment and
        can install into a different one entirely — which updates a venv that
        is not the one running.
        """
        monkeypatch.setattr(update_cmd.shutil, "which", lambda name: "/usr/bin/uv")
        command = update_cmd._install_command(tmp_path)
        assert command[command.index("--python") + 1] == sys.executable

    def test_it_falls_back_to_pip_without_uv(self, tmp_path, monkeypatch):
        """A venv built by hand does have pip, and must still update."""
        monkeypatch.setattr(update_cmd.shutil, "which", lambda name: None)
        command = update_cmd._install_command(tmp_path)

        assert command[:4] == [sys.executable, "-m", "pip", "install"]

    def test_it_installs_the_package_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(update_cmd.shutil, "which", lambda name: "/usr/bin/uv")
        assert str(tmp_path) in update_cmd._install_command(tmp_path)
