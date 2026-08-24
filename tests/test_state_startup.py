"""The one check that runs before a session, and how often.

Of everything `sessions doctor` reports, a stale index is the only failure a
person cannot notice: search answers "nothing found", which reads exactly like
the truth. So it is checked automatically and the rest is not.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.state import index as index_module
from andromeda_cli.state import startup as startup_module


def written(count=1):
    made = []
    for index in range(count):
        session = store.Session()
        session.messages = [{"role": "user", "content": f"session {index}"}]
        session.save()
        made.append(session)
    return made


class TestWhenItRuns:
    def test_the_first_launch_checks(self):
        assert startup_module._due("0.3.0", now=1_000.0)

    def test_a_second_launch_the_same_day_does_not(self):
        startup_module.check("0.3.0", now=1_000.0)
        assert not startup_module._due("0.3.0", now=1_100.0)

    def test_a_day_later_it_checks_again(self):
        startup_module.check("0.3.0", now=1_000.0)
        assert startup_module._due("0.3.0", now=1_000.0 + startup_module.INTERVAL)

    def test_a_new_version_always_checks(self):
        """A schema addition arrives with an upgrade, and the run right after
        one is exactly when the index is behind."""
        startup_module.check("0.3.0", now=1_000.0)
        assert startup_module._due("0.3.1", now=1_100.0)

    def test_an_unreadable_marker_reads_as_due(self):
        startup_module.marker_path().parent.mkdir(parents=True, exist_ok=True)
        startup_module.marker_path().write_text("{not json", encoding="utf-8")
        assert startup_module._due("0.3.0", now=1_000.0)

    def test_the_marker_is_stamped_before_the_work(self, monkeypatch):
        """A check that crashes must not then run on every single launch.

        Raised as a `RuntimeError` on purpose: a `sqlite3.Error` is handled and
        reported, so it would not prove the ordering.
        """

        def explode():
            raise RuntimeError("boom")

        monkeypatch.setattr(index_module, "stale_count", explode)
        with pytest.raises(RuntimeError):
            startup_module.check("0.3.0", now=1_000.0)
        assert not startup_module._due("0.3.0", now=1_100.0)


class TestWhatItSays:
    def test_a_healthy_install_says_nothing(self):
        session = written()[0]
        state.index_session(session)
        assert startup_module.check("0.3.0", force=True).quiet

    def test_a_small_backlog_is_indexed_rather_than_reported(self):
        written(3)
        findings = startup_module.check("0.3.0", force=True)
        assert findings.reindexed == 3
        assert findings.quiet
        assert state.stale_count() == 0

    def test_a_large_backlog_is_reported_rather_than_indexed(self, monkeypatch):
        """Spending a minute on it before the first prompt appears is worse
        than saying so in one line."""
        monkeypatch.setattr(index_module, "stale_count", lambda: 5_000)
        called = []
        monkeypatch.setattr(
            index_module, "reindex", lambda *a, **k: called.append(1) or {}
        )
        findings = startup_module.check("0.3.0", force=True)
        assert called == []
        assert "sessions reindex" in findings.lines[0]

    def test_an_unopenable_index_names_the_repair(self, monkeypatch):
        def explode():
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(index_module, "stale_count", explode)
        findings = startup_module.check("0.3.0", force=True)
        assert "rebuild-index" in findings.lines[0]
        assert findings.error

    def test_an_unreadable_transcript_is_pointed_at_doctor(self):
        """The startup path deliberately does not parse every transcript, so
        it reports the count the reindex noticed and stops there."""
        session = written()[0]
        session.path.write_text("{not json", encoding="utf-8")
        findings = startup_module.check("0.3.0", force=True)
        assert "sessions doctor" in findings.lines[0]

    def test_leftover_quarantine_is_surfaced_once(self):
        from andromeda_cli.state import recovery as recovery_module

        held = recovery_module.quarantine_dir()
        held.mkdir(parents=True, exist_ok=True)
        (held / "abc.123.json").write_text("{}", encoding="utf-8")
        findings = startup_module.check("0.3.0", force=True)
        assert findings.quarantined == 1
        assert "quarantine" in findings.lines[0]


class TestTheSurfaces:
    def test_the_repl_never_fails_over_it(self, monkeypatch):
        from andromeda_cli import repl

        monkeypatch.setattr(
            state, "startup_check", lambda _v: (_ for _ in ()).throw(RuntimeError())
        )
        repl._mention_state_health()  # must not raise

    def test_both_surfaces_run_the_same_check(self):
        """Two surfaces of one product that disagree about whether the index
        is healthy is worse than neither checking."""
        import inspect

        from andromeda_tui.app import AndromedaApp

        assert "startup_check" in inspect.getsource(AndromedaApp._state_health)
        assert "startup_check" in inspect.getsource(repl_source())


def repl_source():
    from andromeda_cli import repl

    return repl._mention_state_health
