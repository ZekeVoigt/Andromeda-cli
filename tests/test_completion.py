"""Shell completion, generated from the parser."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from andromeda_cli import completion
from andromeda_cli.__main__ import COMMANDS, build_command_parser


@pytest.fixture(scope="module")
def parser():
    return build_command_parser()


@pytest.fixture(scope="module")
def scripts(parser):
    return {shell: completion.generate(shell, parser) for shell in completion.SHELLS}


# ---------------------------------------------------------------------------
# reading the parser
# ---------------------------------------------------------------------------


def test_the_tree_finds_every_verb(parser):
    tree = completion.walk(parser)
    assert set(tree["subcommands"]) == set(COMMANDS)


def test_the_tree_finds_subcommands(parser):
    tree = completion.walk(parser)
    assert "list" in tree["subcommands"]["hooks"]["subcommands"]
    assert "doctor" in tree["subcommands"]["hooks"]["subcommands"]


def test_the_tree_finds_flags(parser):
    tree = completion.walk(parser)
    flags = tree["subcommands"]["worktrees"]["subcommands"]["prune"]["flags"]
    assert "--dry-run" in flags


def test_help_text_is_carried(parser):
    tree = completion.walk(parser)
    assert tree["subcommands"]["hooks"]["help"]


def test_quotes_are_removed_from_help_text():
    """Three shells, three escaping rules. A description is not worth a
    quoting bug in somebody's shell startup."""
    assert completion.clean("it's \"quoted\" and \\escaped") == (
        "its quoted and escaped"
    )


def test_help_text_is_truncated():
    assert len(completion.clean("x" * 200)) == 60


# ---------------------------------------------------------------------------
# what the scripts contain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_every_verb_appears(scripts, shell):
    """A hand-kept list is wrong the first time somebody adds a command, and
    the symptom is a tab that does nothing."""
    script = scripts[shell]
    for verb in COMMANDS:
        assert verb in script, f"{verb} missing from {shell} completion"


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_subcommands_appear(scripts, shell):
    script = scripts[shell]
    for name in ("doctor", "revoke", "prune", "untrust"):
        assert name in script


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_profiles_are_completed_dynamically(scripts, shell):
    """The one completion that cannot come from the parser — the names exist
    on disk, not in the code."""
    assert "andromeda-cli/profiles" in scripts[shell]


def test_an_unknown_shell_is_refused(parser):
    with pytest.raises(ValueError):
        completion.generate("csh", parser)


# ---------------------------------------------------------------------------
# the scripts actually parse
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_the_bash_script_is_valid(tmp_path, scripts):
    path = tmp_path / "completion.bash"
    path.write_text(scripts["bash"], encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("zsh") is None, reason="no zsh")
def test_the_zsh_script_is_valid(tmp_path, scripts):
    path = tmp_path / "completion.zsh"
    path.write_text(scripts["zsh"], encoding="utf-8")
    result = subprocess.run(["zsh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("fish") is None, reason="no fish")
def test_the_fish_script_is_valid(tmp_path, scripts):
    path = tmp_path / "completion.fish"
    path.write_text(scripts["fish"], encoding="utf-8")
    result = subprocess.run(
        ["fish", "--no-execute", str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_the_verb_prints_a_script(shell, capsys):
    assert run(["completion", shell]) == 0
    assert "andromeda" in capsys.readouterr().out


def test_an_unknown_shell_is_rejected_at_the_command_line(capsys):
    with pytest.raises(SystemExit):
        run(["completion", "csh"])
