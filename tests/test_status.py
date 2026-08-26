"""`andromeda status` — one screen, read from disk, inventing nothing."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from andromeda_cli import config as config_module
from andromeda_cli import sessions as sessions_module
from andromeda_cli.commands import status as status_cmd


@pytest.fixture
def clean_cwd(tmp_path, monkeypatch):
    """A directory that is not a workspace, so `Here` is deterministic."""
    plain = tmp_path / "elsewhere"
    plain.mkdir()
    monkeypatch.chdir(plain)
    return plain


def record(usage: dict, *, age_days: float = 0.0) -> sessions_module.Session:
    session = sessions_module.Session()
    session.model = "test/model"
    session.messages = [{"role": "user", "content": "hi"}]
    session.usage = usage
    session.save()
    when = time.time() - age_days * 86_400
    import os

    os.utime(session.path, (when, when))
    return session


def test_it_runs_with_nothing_on_disk(clean_cwd, capsys) -> None:
    assert status_cmd.run() == 0

    out = capsys.readouterr().out
    assert "Install" in out
    assert "nothing recorded yet" in out


def test_it_says_when_the_account_is_not_signed_in(clean_cwd, capsys) -> None:
    status_cmd.run()

    assert "not signed in" in capsys.readouterr().out


def test_the_byok_lane_reports_the_key_variable_instead(clean_cwd, capsys, monkeypatch) -> None:
    config_module.set_value("provider", "direct")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    status_cmd.run()
    out = capsys.readouterr().out

    assert "OPENROUTER_API_KEY" in out
    assert "not set" in out
    assert "not signed in" not in out


def test_usage_is_summed_across_sessions(clean_cwd, capsys) -> None:
    record({"requests": 2, "input": 1000, "output": 100})
    record({"requests": 1, "input": 500, "output": 50})

    status_cmd.run()
    out = capsys.readouterr().out

    assert "requests    3" in out
    assert "across 2 sessions" in out
    assert "1.6k" in out  # 1,650 total, shown to one decimal


def test_sessions_outside_the_window_are_not_counted(clean_cwd, capsys) -> None:
    record({"requests": 5, "input": 9999, "output": 1}, age_days=30)

    status_cmd.run(days=7)

    assert "nothing recorded yet" in capsys.readouterr().out


def test_a_wider_window_reaches_them(clean_cwd, capsys) -> None:
    record({"requests": 5, "input": 9999, "output": 1}, age_days=30)

    status_cmd.run(days=60)

    assert "requests    5" in capsys.readouterr().out


def test_a_session_with_no_usage_is_not_counted_as_one(clean_cwd, capsys) -> None:
    record({})
    record({"requests": 1, "input": 10, "output": 1})

    status_cmd.run()

    assert "across 1 session" in capsys.readouterr().out


def test_a_corrupted_usage_block_does_not_stop_the_report(clean_cwd, capsys) -> None:
    record("not a dict")  # type: ignore[arg-type]
    record({"requests": 1, "input": 10, "output": 1})

    assert status_cmd.run() == 0
    assert "requests    1" in capsys.readouterr().out


def test_the_cache_share_is_shown_when_the_provider_reports_one(clean_cwd, capsys) -> None:
    record({"requests": 1, "input": 1000, "output": 10, "cached": 800})

    status_cmd.run()
    out = capsys.readouterr().out

    assert "cached" in out
    assert "80%" in out


def test_a_mixed_session_names_both_models(clean_cwd, capsys) -> None:
    record(
        {
            "requests": 2,
            "input": 150,
            "output": 15,
            "by_model": {
                "main": {"requests": 1, "input": 100, "output": 10},
                "aux": {"requests": 1, "input": 50, "output": 5},
            },
        }
    )

    status_cmd.run()
    out = capsys.readouterr().out

    assert "main" in out
    assert "aux" in out


def test_no_money_is_reported_anywhere(clean_cwd, capsys) -> None:
    """There is no price table and there must never be one."""
    record({"requests": 4, "input": 100_000, "output": 20_000})

    status_cmd.run()

    assert "$" not in capsys.readouterr().out


def test_here_says_when_this_is_not_a_workspace(clean_cwd, capsys) -> None:
    status_cmd.run()

    assert "not a workspace" in capsys.readouterr().out


def test_here_reports_the_verify_loop_inside_a_project(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\nname='x'\n\n[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (workspace / "main.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(workspace)

    status_cmd.run()
    out = capsys.readouterr().out

    assert "posture     coding" in out
    assert "pytest" in out


def test_diagnostics_off_is_stated_rather_than_silent(tmp_path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    config_module.set_value("lsp", "false")

    status_cmd.run()

    assert "diagnostics off" in capsys.readouterr().out
