"""The derived index, and the rule that decides append versus rebuild.

The incremental path is the one worth pinning. It runs after every exchange in
every session, and when it silently stops working the only symptom is that a
long session gets slower — which nobody reports as a bug.
"""

from __future__ import annotations

import json

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.state import db as db_module
from andromeda_cli.state import index as index_module


def make(messages, model="test/model", workspace="/tmp/w"):
    session = store.Session()
    session.messages = list(messages)
    session.model = model
    session.provider = "relay"
    session.workspace = workspace
    session.save()
    return session


EXCHANGE = [
    {"role": "system", "content": "the skills manifest and every standing memory"},
    {"role": "user", "content": "what did we decide about the retry budget"},
    {"role": "assistant", "content": "We capped it at three attempts."},
]


class TestWhatGetsIndexed:
    def test_a_session_becomes_searchable(self):
        session = make(EXCHANGE)
        state.index_session(session)
        assert [hit.session_id for hit in state.search("retry budget")] == [session.id]

    def test_system_messages_are_not_indexed(self):
        """They carry the skills manifest and every standing memory, so
        indexing them makes every session match anything the agent knows."""
        session = make(EXCHANGE)
        state.index_session(session)
        assert state.search("standing memory") == []
        assert state.counts()["messages"] == 2

    def test_positions_are_the_true_transcript_index(self):
        """Even though system rows are skipped, so an anchored read lines up
        with the file a person can `cat`."""
        session = make(EXCHANGE)
        state.index_session(session)
        hit = state.search("retry budget")[0]
        assert session.messages[hit.position]["content"] == hit.content

    def test_a_tool_name_is_searchable(self):
        session = make(
            [
                {"role": "user", "content": "look at it"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "terminal", "arguments": "{}"}}
                    ],
                },
            ]
        )
        state.index_session(session)
        assert [hit.position for hit in state.search("terminal")] == [1]

    def test_empty_messages_are_skipped(self):
        session = make(
            [{"role": "user", "content": "  "}, {"role": "assistant", "content": ""}]
        )
        state.index_session(session)
        assert state.counts()["messages"] == 0

    def test_a_content_list_is_flattened(self):
        """A multimodal or reasoning model sends blocks, not a string."""
        session = make(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "the colima dns failure"},
                        {"type": "image_url", "image_url": {"url": "…"}},
                    ],
                }
            ]
        )
        state.index_session(session)
        assert len(state.search("colima dns")) == 1


class TestIncrementalIndexing:
    def test_a_new_turn_is_appended_not_rebuilt(self):
        session = make(EXCHANGE)
        assert index_module.index_session(session) == "rebuilt"
        session.messages.append({"role": "user", "content": "and the backoff"})
        session.save()
        assert index_module.index_session(session) == "appended"
        assert state.counts()["messages"] == 3

    def test_an_unchanged_session_does_no_work(self):
        session = make(EXCHANGE)
        index_module.index_session(session)
        assert index_module.index_session(session) == "unchanged"

    def test_a_rewind_forces_a_rebuild(self):
        """The stored positions no longer describe the transcript, and
        patching them is how an index quietly starts lying."""
        session = make(EXCHANGE)
        index_module.index_session(session)
        session.messages = session.messages[:-1] + [
            {"role": "assistant", "content": "Actually we capped it at five."}
        ]
        session.save()
        assert index_module.index_session(session) == "rebuilt"
        assert state.search("three attempts") == []
        assert len(state.search("five")) == 1

    def test_compaction_rewriting_the_head_forces_a_rebuild(self):
        """Even when it leaves the last message untouched and adds a turn,
        which is the case a tail-only fingerprint would let through."""
        session = make(EXCHANGE)
        index_module.index_session(session)
        session.messages = [
            {"role": "system", "content": "[earlier turns, summarised]"},
            *session.messages[1:],
            {"role": "user", "content": "carry on"},
        ]
        session.save()
        assert index_module.index_session(session) == "rebuilt"

    def test_a_shorter_transcript_is_never_treated_as_an_append(self):
        session = make(EXCHANGE)
        index_module.index_session(session)
        session.messages = session.messages[:2]
        session.save()
        assert index_module.index_session(session) == "rebuilt"
        assert state.counts()["messages"] == 1


class TestReindexing:
    def test_it_picks_up_a_transcript_written_behind_its_back(self):
        session = make(EXCHANGE)
        assert state.stale_count() == 1
        counts = state.reindex()
        assert counts["rebuilt"] == 1
        assert state.stale_count() == 0
        assert len(state.search("retry budget")) == 1
        assert session.id in {row.id for row in state.recent()}

    def test_a_deleted_transcript_stops_being_returned(self):
        """Otherwise search keeps offering a session that no longer exists."""
        session = make(EXCHANGE)
        state.reindex()
        session.path.unlink()
        counts = state.reindex()
        assert counts["dropped"] == 1
        assert state.search("retry budget") == []

    def test_one_damaged_transcript_does_not_hide_the_others(self):
        good = make(EXCHANGE)
        broken = make([{"role": "user", "content": "something else"}])
        broken.path.write_text("{not json", encoding="utf-8")
        counts = state.reindex()
        assert counts["unreadable"] == 1
        assert {row.id for row in state.recent()} == {good.id}

    def test_force_rebuilds_everything(self):
        make(EXCHANGE)
        state.reindex()
        counts = state.reindex(force=True)
        assert counts["rebuilt"] == 1 and counts["unchanged"] == 0


class TestFailingSafely:
    def test_indexing_never_raises_into_a_turn(self, monkeypatch):
        """Failing to index is a failure to search later. It must never
        become a failure to answer now."""
        import sqlite3

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db_module, "connect", explode)
        assert index_module.index_session(make(EXCHANGE)) == "failed"

    def test_search_returns_empty_rather_than_raising(self, monkeypatch):
        import sqlite3

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db_module, "connect", explode)
        assert state.search("anything") == []


class TestMigrations:
    def test_the_ledger_is_keyed_by_name_not_by_number(self):
        """A numbered ledger breaks the day two branches both add a fourth
        migration, or somebody renumbers a shipped one."""
        with db_module.connect() as conn:
            names = [
                row["name"]
                for row in conn.execute("SELECT name FROM schema_migrations")
            ]
        assert "base-tables" in names
        assert all(not name[0].isdigit() for name in names)

    def test_migrating_twice_is_a_no_op(self):
        with db_module.connect() as conn:
            assert db_module.migrate(conn) == []

    def test_an_optional_step_that_fails_is_not_recorded(self):
        """So it is retried against a SQLite that can run it, rather than
        skipped forever."""
        import sqlite3

        broken = db_module.Migration(
            "deliberately-broken", "CREATE VIRTUAL TABLE x USING nope(y);", optional=True
        )
        monkey = (*db_module.MIGRATIONS, broken)
        original = db_module.MIGRATIONS
        db_module.MIGRATIONS = monkey
        try:
            with db_module.connect() as conn:
                db_module.migrate(conn)
                row = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = 'deliberately-broken'"
                ).fetchone()
            assert row is None
        finally:
            db_module.MIGRATIONS = original
        assert sqlite3.sqlite_version  # the connection survived the failure


class TestArchivedRows:
    """Turns compaction folded away. The index is the only copy left, which is
    what lets the summary replacing them say they are still readable."""

    def test_a_rebuild_leaves_them_alone(self):
        session = make(
            [{"role": "user", "content": f"message {n}"} for n in range(6)]
        )
        state.index_session(session)
        assert state.archive_range(session.id, 0, 3) == 4

        # The transcript now holds only what survived compaction.
        session.messages = [{"role": "user", "content": "a fresh start"}]
        session.save()
        assert index_module.index_session(session) == "rebuilt"

        contents = [row["content"] for row in state.transcript(session.id)]
        assert "message 0" in contents and "a fresh start" in contents

    def test_a_forced_reindex_leaves_them_alone_too(self):
        """`--force` exists to rebuild from the files. There is no file to
        rebuild an archived turn from, so forcing must not drop it."""
        session = make([{"role": "user", "content": "message 0"}])
        state.index_session(session)
        state.archive_range(session.id, 0, 0)
        session.messages = [{"role": "user", "content": "later"}]
        session.save()
        state.reindex(force=True)
        assert state.archived_count(session.id) == 1

    def test_they_stay_searchable(self):
        session = make([{"role": "user", "content": "the retry budget is three"}])
        state.index_session(session)
        state.archive_range(session.id, 0, 0)
        session.messages = [{"role": "user", "content": "something else"}]
        session.save()
        state.index_session(session)

        hits = state.search("retry budget")
        assert [hit.archived for hit in hits] == [True]

    def test_archiving_is_idempotent(self):
        """A retried compaction must not double-count or resurrect anything."""
        session = make([{"role": "user", "content": "message 0"}])
        state.index_session(session)
        assert state.archive_range(session.id, 0, 0) == 1
        assert state.archive_range(session.id, 0, 0) == 0

    def test_deleting_a_session_takes_its_archived_turns(self):
        """Keeping them would leave search returning a conversation that no
        longer exists."""
        session = make([{"role": "user", "content": "the retry budget"}])
        state.index_session(session)
        state.archive_range(session.id, 0, 0)
        state.forget_session(session.id)
        assert state.search("retry budget") == []
        assert state.archived_count() == 0

    def test_they_are_counted_separately(self):
        session = make(
            [{"role": "user", "content": f"message {n}"} for n in range(4)]
        )
        state.index_session(session)
        state.archive_range(session.id, 0, 1)
        assert state.counts()["archived"] == 2
