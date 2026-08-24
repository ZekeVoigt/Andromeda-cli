"""Salvaging a transcript a machine truncated, and repairing the index.

The index needs no salvage: it holds no original data, so the blunt repair is
to delete it and read the transcripts again. The transcripts do — JSON is
all-or-nothing to a parser, so one missing brace loses a conversation that is
almost entirely intact on disk.
"""

from __future__ import annotations

import json

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.state import recovery as recovery_module


def written(count=6):
    session = store.Session()
    session.model = "test/model"
    session.workspace = "/tmp/w"
    session.messages = [
        {"role": "user" if n % 2 == 0 else "assistant", "content": f"message {n}"}
        for n in range(count)
    ]
    session.save()
    return session


def truncate(session, fraction=0.7):
    text = session.path.read_text(encoding="utf-8")
    session.path.write_text(text[: int(len(text) * fraction)], encoding="utf-8")


class TestSalvage:
    def test_a_truncated_transcript_yields_its_complete_messages(self):
        session = written(8)
        truncate(session)
        recovered = recovery_module.salvage(session.path)
        assert recovered is not None
        assert 0 < len(recovered.messages) < 8
        assert recovered.id == session.id

    def test_metadata_survives_the_truncation(self):
        session = written()
        truncate(session)
        recovered = recovery_module.salvage(session.path)
        assert recovered.model == "test/model"
        assert recovered.workspace == "/tmp/w"

    def test_a_brace_inside_a_pasted_string_does_not_end_a_message_early(self):
        """The failure a naive brace counter produces: it recovers half a
        message and calls the transcript whole."""
        session = store.Session()
        session.messages = [
            {"role": "user", "content": 'here is code: {"a": {"b": 1}} and more text'},
            {"role": "assistant", "content": "understood"},
        ]
        session.save()
        truncate(session, 0.9)
        recovered = recovery_module.salvage(session.path)
        assert recovered is not None
        assert recovered.messages[0]["content"].endswith("and more text")

    def test_an_escaped_quote_does_not_end_a_string_early(self):
        session = store.Session()
        session.messages = [
            {"role": "user", "content": 'she said \\"no\\" and then {left}'},
            {"role": "assistant", "content": "ok"},
        ]
        session.save()
        recovered = recovery_module.salvage(session.path)
        assert len(recovered.messages) == 2

    def test_a_file_with_nothing_recoverable_returns_none(self):
        session = written()
        session.path.write_text("garbage", encoding="utf-8")
        assert recovery_module.salvage(session.path) is None

    def test_an_intact_transcript_round_trips_unchanged(self):
        session = written()
        recovered = recovery_module.salvage(session.path)
        assert recovered.messages == session.messages


class TestChecking:
    def test_a_healthy_install_reports_healthy(self):
        session = written()
        state.index_session(session)
        report = state.check()
        assert report.healthy
        assert report.integrity == "ok"

    def test_a_damaged_transcript_is_named_with_what_can_be_saved(self):
        session = written(8)
        truncate(session)
        report = state.check()
        assert len(report.damaged) == 1
        assert report.damaged[0].path == session.path
        assert report.damaged[0].salvageable > 0

    def test_a_transcript_newer_than_the_index_counts_as_stale(self):
        written()
        assert state.check().stale == 1


class TestRepair:
    def test_a_dry_run_changes_nothing(self):
        """Recovery that rewrites files the first time it is asked a question
        is recovery nobody runs twice."""
        session = written(8)
        truncate(session)
        before = session.path.read_bytes()
        outcome = recovery_module.repair(apply=False)
        assert outcome.recovered == [session.id]
        assert not outcome.applied
        assert session.path.read_bytes() == before
        assert not recovery_module.quarantine_dir().exists()

    def test_applying_writes_the_salvage_and_keeps_the_original(self):
        """Nothing is deleted, so a recovery that guessed wrong is undoable."""
        session = written(8)
        truncate(session)
        recovery_module.repair(apply=True)

        restored = store.load(session.id)
        assert restored is not None and restored.messages
        held = list(recovery_module.quarantine_dir().glob("*.json"))
        assert len(held) == 1

    def test_the_index_is_healthy_afterwards(self):
        session = written(8)
        truncate(session)
        recovery_module.repair(apply=True)
        assert state.check().healthy

    def test_an_unrecoverable_file_is_reported_not_quarantined(self):
        session = written()
        session.path.write_text("garbage", encoding="utf-8")
        outcome = recovery_module.repair(apply=True)
        assert outcome.lost == [session.path]
        assert session.path.exists()


class TestRebuildingTheIndex:
    def test_the_index_can_be_thrown_away_and_rebuilt(self):
        """The index holds no original data, so the worst case of deleting it
        is the time it takes to read every transcript once."""
        session = written()
        state.reindex()
        assert state.search("message 0")

        counts = state.rebuild_index()
        assert counts["rebuilt"] == 1
        assert [hit.session_id for hit in state.search("message 0")] == [session.id]

    def test_it_works_when_the_index_was_never_created(self):
        written()
        state.db_path().unlink(missing_ok=True)
        assert state.rebuild_index()["scanned"] == 1
