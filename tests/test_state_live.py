"""Claims on open sessions, and the rule that releases them.

The rule is the whole feature: a claim is released only when its owner is
*proved* gone, by pid and process start time. Reaping on the pid alone hands a
live session's transcript to a second terminal the moment the machine reuses a
pid, and the two then interleave their turns into one file.
"""

from __future__ import annotations

import os

import pytest

from andromeda_agent import liveness
from andromeda_cli.state import live as live_module


class TestClaiming:
    def test_a_fresh_session_is_claimable(self):
        assert live_module.claim("abc123", surface="repl", workspace="/tmp/w") is True

    def test_this_process_can_reclaim_its_own_session(self):
        live_module.claim("abc123")
        assert live_module.claim("abc123") is True

    def test_a_live_holder_blocks_a_second_claim(self, monkeypatch):
        live_module.claim("abc123")
        # A different process, still alive. The pid is captured before it is
        # patched, or the replacement calls itself.
        other = os.getpid() + 1
        monkeypatch.setattr(os, "getpid", lambda: other)
        monkeypatch.setattr(liveness, "owner_is_live", lambda _pid, _start: True)
        assert live_module.claim("abc123") is False

    def test_a_dead_holder_does_not_block(self, monkeypatch):
        live_module.claim("abc123")
        other = os.getpid() + 1
        monkeypatch.setattr(os, "getpid", lambda: other)
        monkeypatch.setattr(liveness, "owner_is_live", lambda _pid, _start: False)
        assert live_module.claim("abc123") is True

    def test_claiming_never_raises_when_the_index_is_unavailable(self, monkeypatch):
        """A registry must not be the reason a session cannot be opened."""
        import sqlite3

        from andromeda_cli.state import db as db_module

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db_module, "connect", explode)
        assert live_module.claim("abc123") is True


class TestReleasing:
    def test_a_process_releases_its_own_claim(self):
        live_module.claim("abc123")
        live_module.release("abc123")
        assert live_module.all_live() == []

    def test_a_process_cannot_release_another_process_claim(self, monkeypatch):
        live_module.claim("abc123")
        other = os.getpid() + 1
        monkeypatch.setattr(os, "getpid", lambda: other)
        live_module.release("abc123")
        # Deliberately no `monkeypatch.undo()`: the autouse `isolated_home`
        # fixture shares this monkeypatch instance, so undoing here would also
        # unset ANDROMEDA_HOME and the assertion below would read the
        # developer's real one. `all_live` needs no pid to answer.
        assert [item.session_id for item in live_module.all_live(prune=False)] == ["abc123"]

    def test_reaping_drops_only_claims_whose_owner_is_proved_gone(self, monkeypatch):
        live_module.claim("gone")
        live_module.claim("here")

        monkeypatch.setattr(
            liveness,
            "owner_is_live",
            lambda _pid, start: start is not None and start == -1,
        )
        # Force the stored start time of one row to the "live" sentinel.
        from andromeda_cli.state import db as db_module

        with db_module.connect() as conn:
            conn.execute(
                "UPDATE live_sessions SET pid_started = -1 WHERE session_id = 'here'"
            )
            conn.execute(
                "UPDATE live_sessions SET pid_started = 7 WHERE session_id = 'gone'"
            )
        assert live_module.reap() == 1
        assert [item.session_id for item in live_module.all_live(prune=False)] == ["here"]

    def test_an_unknowable_start_time_is_treated_as_alive(self):
        """Being unable to prove death must never rewrite state."""
        assert liveness.owner_is_live(os.getpid(), None) is True


class TestReporting:
    def test_held_by_ignores_a_dead_holder(self, monkeypatch):
        live_module.claim("abc123")
        monkeypatch.setattr(liveness, "owner_is_live", lambda _pid, _start: False)
        assert live_module.held_by("abc123") is None

    def test_a_claim_by_this_process_is_marked_as_mine(self):
        live_module.claim("abc123", surface="tui", workspace="/tmp/w")
        [held] = live_module.all_live()
        assert held.mine and held.surface == "tui" and held.workspace == "/tmp/w"

    def test_a_heartbeat_is_not_proof_of_anything(self, monkeypatch):
        """It moves a timestamp and nothing reads it to decide liveness."""
        live_module.claim("abc123")
        before = live_module.all_live()[0].heartbeat_at
        live_module.beat("abc123")
        after = live_module.all_live()[0].heartbeat_at
        assert after >= before
        monkeypatch.setattr(liveness, "owner_is_live", lambda _pid, _start: False)
        assert live_module.reap() == 1
