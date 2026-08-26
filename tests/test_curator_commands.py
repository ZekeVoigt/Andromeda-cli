"""`andromeda curator`, and the sweep that runs when a session opens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from andromeda_agent import curator
from andromeda_tools import skill_usage
from andromeda_cli import config as config_module
from andromeda_cli.commands import curator_cmd
from andromeda_cli.session import build_conversation
from support import ScriptedProvider


def write_skill(root: Path, name: str, body: str = "Steps.", by_agent: bool = True) -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    provenance = "metadata:\n  andromeda:\n    created_by: agent\n" if by_agent else ""
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: does {name}\n{provenance}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def age(home: Path, name: str, days: float, uses: int = 1) -> None:
    when = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    data = skill_usage.load(home)
    record = data.get(name) or skill_usage.blank_record()
    record.update({"created_at": when, "last_used_at": when if uses else "", "uses": uses})
    data[name] = record
    skill_usage.save(home, data)


@pytest.fixture
def home() -> Path:
    return config_module.home()


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_with_no_agent_skills(home, capsys):
    assert curator_cmd.status() == 0
    out = capsys.readouterr().out
    assert "no agent-written skills" in out
    assert "left alone" in out


def test_status_lists_what_it_curates(home, capsys):
    write_skill(home, "made-up")
    age(home, "made-up", days=45)

    assert curator_cmd.status() == 0

    out = capsys.readouterr().out
    assert "made-up" in out
    assert "idle 45 day(s)" in out


def test_status_says_when_it_last_swept(home, capsys):
    write_skill(home, "made-up")
    curator_cmd.sweep()
    capsys.readouterr()

    curator_cmd.status()

    assert "last sweep:" in capsys.readouterr().out


def test_status_says_when_it_is_paused(home, capsys):
    curator.set_paused(home, True)
    curator_cmd.status()
    assert "paused" in capsys.readouterr().out


def test_status_lists_what_is_archived(home, capsys):
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    curator_cmd.sweep()
    capsys.readouterr()

    curator_cmd.status()

    out = capsys.readouterr().out
    assert "1 archived" in out
    assert "restore" in out


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_a_sweep_reports_what_it_did(home, capsys):
    write_skill(home, "made-up")
    age(home, "made-up", days=45)

    assert curator_cmd.sweep() == 0

    assert "marked stale" in capsys.readouterr().out


def test_a_dry_run_says_it_changed_nothing(home, capsys):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=200)

    assert curator_cmd.sweep(dry_run=True) == 0

    out = capsys.readouterr().out
    assert "would be archived" in out
    assert "this was a preview" in out
    assert directory.exists()


def test_archiving_says_how_to_undo_it(home, capsys):
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    curator_cmd.sweep()
    assert "recoverable" in capsys.readouterr().out


def test_a_sweep_with_nothing_to_curate(home, capsys):
    assert curator_cmd.sweep() == 0
    assert "no agent-written skills" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# pin, restore, pause
# ---------------------------------------------------------------------------


def test_pinning_keeps_a_skill_through_a_sweep(home, capsys):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=500)

    assert curator_cmd.pin("made-up") == 0
    curator_cmd.sweep()

    assert directory.exists()
    assert "pinned and left alone" in capsys.readouterr().out


def test_unpinning_lets_it_go(home, capsys):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=500)
    curator_cmd.pin("made-up")
    curator_cmd.unpin("made-up")
    capsys.readouterr()

    curator_cmd.sweep()

    assert not directory.exists()


def test_pinning_something_unknown_fails(home, capsys):
    assert curator_cmd.pin("nope") == 2
    assert "No skill named" in capsys.readouterr().err


def test_restoring_brings_a_skill_back(home, capsys):
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    curator_cmd.sweep()
    capsys.readouterr()

    assert curator_cmd.restore("made-up") == 0

    assert (home / "skills" / "made-up" / "SKILL.md").exists()


def test_restoring_something_that_is_not_archived_fails(home, capsys):
    assert curator_cmd.restore("nope") == 2
    assert "Could not restore" in capsys.readouterr().err


def test_pausing_and_resuming(home, capsys):
    assert curator_cmd.pause() == 0
    assert curator.is_paused(home) is True
    assert curator_cmd.resume() == 0
    assert curator.is_paused(home) is False


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def test_showing_proposals_when_there_are_none(home, capsys):
    assert curator_cmd.review(show_only=True) == 0
    assert "no proposals on record" in capsys.readouterr().out


def test_stored_proposals_are_printed(home, capsys):
    curator.save_proposals(
        home, [curator.Proposal("made-up", "describe", "say when to use it", "no trigger")]
    )

    assert curator_cmd.review(show_only=True) == 0

    out = capsys.readouterr().out
    assert "made-up" in out
    assert "say when to use it" in out
    assert "no trigger" in out


def test_reviewing_nothing_asks_nothing(home, capsys):
    assert curator_cmd.review() == 0
    assert "no agent-written skills" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# through the entry point
# ---------------------------------------------------------------------------


def test_the_verb_is_reachable_from_argv(home, capsys):
    assert run(["curator", "status"]) == 0
    assert "last sweep" in capsys.readouterr().out


def test_the_subcommands_are_reachable(home, capsys):
    write_skill(home, "made-up")
    assert run(["curator", "sweep", "--dry-run"]) == 0
    assert run(["curator", "pin", "made-up"]) == 0
    assert run(["curator", "unpin", "made-up"]) == 0
    assert run(["curator", "pause"]) == 0
    assert run(["curator", "resume"]) == 0


def test_the_settings_are_real(home):
    config = config_module.load()
    assert config["curator"] is True
    assert config["curator_stale_days"] == 30
    assert config["curator_archive_days"] == 90


# ---------------------------------------------------------------------------
# the sweep a session runs
# ---------------------------------------------------------------------------


def build(root, **overrides):
    config = config_module.load()
    config.update({"approval_mode": "auto", **overrides})
    provider = ScriptedProvider(script=["ok"])
    return build_conversation(
        config, provider, interactive=True, workspace_root=str(root)
    )


def make_due(home: Path) -> None:
    curator.due(home, curator.Settings())  # seeds the clock
    state = curator.load_state(home)
    state["last_run_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(days=30)
    ).isoformat()
    curator.save_state(home, state)


def test_a_session_sweeps_when_it_is_due(tmp_path, home):
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    make_due(home)

    conversation, _ = build(tmp_path)

    assert "curated skills" in conversation.curator_note
    assert not (home / "skills" / "made-up").exists()


def test_a_swept_skill_is_not_in_this_session_s_prompt(tmp_path, home):
    """Sweeping afterwards would list a skill in the prompt and archive it out
    from under the same session."""
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    make_due(home)

    conversation, _ = build(tmp_path)

    assert "made-up" not in conversation.messages[0]["content"]


def test_a_session_says_nothing_when_nothing_moved(tmp_path, home):
    write_skill(home, "made-up")
    make_due(home)
    conversation, _ = build(tmp_path)
    assert conversation.curator_note == ""


def test_a_session_does_not_sweep_before_the_interval(tmp_path, home):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=200)
    curator.due(home, curator.Settings())  # seeds, not due

    conversation, _ = build(tmp_path)

    assert conversation.curator_note == ""
    assert directory.exists()


def test_a_broken_sweep_never_stops_a_session(tmp_path, home, monkeypatch):
    monkeypatch.setattr(
        curator, "due", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError())
    )
    conversation, _ = build(tmp_path)
    assert conversation.curator_note == ""
