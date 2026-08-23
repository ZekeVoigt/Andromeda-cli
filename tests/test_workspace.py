"""Confinement. New surface — the hosted runtime never had a user filesystem."""

from __future__ import annotations

import os

import pytest

from andromeda_tools import PathOutsideWorkspace, Workspace


def test_a_relative_path_resolves_under_the_root(tmp_path):
    workspace = Workspace(tmp_path)
    assert workspace.resolve("a/b.txt") == (tmp_path / "a" / "b.txt").resolve()


def test_a_relative_path_is_taken_against_the_root_not_the_cwd(tmp_path, monkeypatch):
    """These diverge the moment a tool changes directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(elsewhere)

    workspace = Workspace(root)
    assert workspace.resolve("x.txt") == (root / "x.txt").resolve()


def test_dotdot_cannot_climb_out(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("../secrets.txt")


def test_a_deep_dotdot_chain_cannot_climb_out(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("a/b/../../../../etc/passwd")


def test_an_absolute_path_outside_is_refused(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("/etc/passwd")


def test_a_symlink_pointing_out_is_refused(tmp_path):
    """resolve() and not absolute() — a symlink inside the root walks out."""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "link")

    workspace = Workspace(root)
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("link/secret.txt")


def test_a_path_that_does_not_exist_yet_is_still_checked(tmp_path):
    """The write_file case."""
    workspace = Workspace(tmp_path)
    assert workspace.resolve("new/file.txt")
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("../new/file.txt")


def test_the_root_itself_is_inside(tmp_path):
    workspace = Workspace(tmp_path)
    assert workspace.resolve(".") == tmp_path.resolve()


def test_relative_display_hides_the_home_prefix(tmp_path):
    workspace = Workspace(tmp_path)
    assert workspace.relative(tmp_path / "a" / "b.txt") == "a/b.txt"
