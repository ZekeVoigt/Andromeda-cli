"""The stop button, and what it does and does not stop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from andromeda_agent import pause
from andromeda_cli import config as config_module


@pytest.fixture(autouse=True)
def clean():
    pause.reset_for_tests()
    yield
    pause.reset_for_tests()


@pytest.fixture
def home() -> Path:
    root = config_module.home()
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


# ---------------------------------------------------------------------------
# the sentinel
# ---------------------------------------------------------------------------


def test_nothing_is_paused_by_default(home):
    assert pause.engaged(home) is False
    assert pause.state(home) is None
    assert pause.describe(home) == ""


def test_engaging_and_lifting(home):
    pause.engage(home)
    assert pause.engaged(home) is True
    assert pause.disengage(home) is True
    assert pause.engaged(home) is False


def test_lifting_a_pause_that_is_not_on(home):
    assert pause.disengage(home) is False


def test_the_reason_is_kept(home):
    pause.engage(home, "deploying by hand")
    current = pause.state(home)
    assert current["reason"] == "deploying by hand"
    assert current["engaged_at"]
    assert "deploying by hand" in pause.describe(home)


def test_engaging_twice_refreshes_the_reason(home):
    pause.engage(home, "first")
    pause.engage(home, "second")
    assert pause.state(home)["reason"] == "second"


def test_a_file_made_by_touch_still_pauses(home):
    """It has to be settable by anything — another terminal, a script, a
    `touch` over SSH. The file existing is the mechanism."""
    pause.sentinel(home).touch()
    assert pause.engaged(home) is True
    assert pause.state(home) == {"reason": "", "engaged_at": ""}


def test_a_corrupt_sentinel_still_pauses(home):
    pause.sentinel(home).write_text("{not json", encoding="utf-8")
    assert pause.engaged(home) is True
    assert pause.state(home)["reason"] == ""


def test_an_unreadable_home_reads_as_paused(home, monkeypatch):
    """Failing open here would lift somebody's emergency stop at exactly the
    moment the filesystem is misbehaving."""

    def refuse(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "exists", refuse)
    assert pause.engaged(home) is True
    assert pause.state(home) == {"reason": "", "engaged_at": ""}


def test_the_sentinel_is_readable_json(home):
    pause.engage(home, "why")
    data = json.loads(pause.sentinel(home).read_text())
    assert data["reason"] == "why"


# ---------------------------------------------------------------------------
# the tick check
# ---------------------------------------------------------------------------


def test_check_is_false_when_running(home):
    assert pause.check(home, "the scheduler") is False


def test_check_announces_once_per_engagement(home, caplog):
    pause.engage(home, "maintenance")

    with caplog.at_level("INFO"):
        for _ in range(5):
            assert pause.check(home, "the scheduler") is True

    assert caplog.text.count("is paused") == 1
    assert "maintenance" in caplog.text


def test_the_announcement_rearms_after_a_resume(home, caplog):
    with caplog.at_level("INFO"):
        pause.engage(home)
        pause.check(home, "the scheduler")
        pause.disengage(home)
        pause.check(home, "the scheduler")
        pause.engage(home)
        pause.check(home, "the scheduler")

    assert caplog.text.count("is paused") == 2


def test_two_components_each_announce(home, caplog):
    pause.engage(home)
    with caplog.at_level("INFO"):
        pause.check(home, "the scheduler")
        pause.check(home, "something else")
    assert caplog.text.count("is paused") == 2


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def test_the_pause_command(home, capsys):
    assert run(["pause"]) == 0
    out = capsys.readouterr().out
    assert "Paused" in out
    assert "already running is untouched" in out
    assert pause.engaged(home) is True


def test_the_pause_command_takes_a_reason(home, capsys):
    assert run(["pause", "--reason", "deploying"]) == 0
    assert "deploying" in capsys.readouterr().out
    assert pause.state(home)["reason"] == "deploying"


def test_the_resume_command(home, capsys):
    run(["pause"])
    capsys.readouterr()
    assert run(["resume"]) == 0
    assert "Resumed" in capsys.readouterr().out
    assert pause.engaged(home) is False


def test_resuming_when_nothing_is_paused(home, capsys):
    assert run(["resume"]) == 0
    assert "Not paused" in capsys.readouterr().out


def test_doctor_says_when_it_is_paused(home, capsys):
    """A paused install is the most confusing thing this can be if it does not
    say so: jobs stop and everything else looks healthy."""
    from andromeda_cli.commands import doctor

    run(["pause", "--reason", "holidays"])
    capsys.readouterr()

    doctor.run()

    out = capsys.readouterr().out
    assert "paused" in out
    assert "holidays" in out


def test_doctor_says_nothing_when_running(home, capsys):
    from andromeda_cli.commands import doctor

    doctor.run()
    assert "paused" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# what it holds
# ---------------------------------------------------------------------------


def test_the_scheduler_holds_while_paused(home, capsys, monkeypatch):
    """The dispatch stops; the heartbeat does not, because a stopped heartbeat
    reads as a crash."""
    from andromeda_cli.commands import cron

    fired: list[str] = []
    beats: list[int] = []

    class Provider:
        def describe(self):
            return "built-in"

        def due(self, schedule):
            fired.append("dispatched")
            return []

        def after_run(self, schedule, job, run):
            pass

    monkeypatch.setattr(cron.providers_cron, "get", lambda name: Provider())
    monkeypatch.setattr(cron, "heartbeat", lambda path: beats.append(1))

    run(["pause"])
    capsys.readouterr()

    cron._tick_forever(once=True)

    assert fired == []
    assert beats == [1]


def test_the_scheduler_dispatches_when_not_paused(home, capsys, monkeypatch):
    from andromeda_cli.commands import cron

    fired: list[str] = []

    class Provider:
        def describe(self):
            return "built-in"

        def due(self, schedule):
            fired.append("dispatched")
            return []

        def after_run(self, schedule, job, run):
            pass

    monkeypatch.setattr(cron.providers_cron, "get", lambda name: Provider())
    monkeypatch.setattr(cron, "heartbeat", lambda path: None)

    cron._tick_forever(once=True)

    assert fired == ["dispatched"]


def test_a_paused_scheduler_says_so_at_startup(home, capsys, monkeypatch):
    from andromeda_cli.commands import cron

    class Provider:
        def describe(self):
            return "built-in"

        def due(self, schedule):
            return []

        def after_run(self, schedule, job, run):
            pass

    monkeypatch.setattr(cron.providers_cron, "get", lambda name: Provider())
    monkeypatch.setattr(cron, "heartbeat", lambda path: None)

    run(["pause", "--reason", "maintenance"])
    capsys.readouterr()

    cron._tick_forever(once=True)

    assert "maintenance" in capsys.readouterr().out
