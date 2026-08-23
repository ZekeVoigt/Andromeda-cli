from __future__ import annotations

import json
import os
import stat
import time

from andromeda_cli import sessions as store


def make(messages, session_id=None, updated=None):
    session = store.Session(id=session_id) if session_id else store.Session()
    session.messages = messages
    session.model = "test/model"
    session.save()
    if updated is not None:
        # Rewrite the timestamp after save(), which stamps its own.
        raw = json.loads(session.path.read_text(encoding="utf-8"))
        raw["updated_at"] = updated
        session.path.write_text(json.dumps(raw), encoding="utf-8")
    return session


USER_TURN = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "how do I build the thing"},
    {"role": "assistant", "content": "like this"},
]


def test_a_session_round_trips():
    saved = make(USER_TURN)
    loaded = store.load(saved.id)
    assert loaded is not None
    assert loaded.messages == USER_TURN
    assert loaded.model == "test/model"


def test_the_title_is_the_first_user_message():
    assert make(USER_TURN).title == "how do I build the thing"


def test_a_long_title_is_trimmed():
    long = "x" * 200
    session = make([{"role": "user", "content": long}])
    assert len(session.title) <= store.MAX_TITLE + 1
    assert session.title.endswith("…")


def test_turns_counts_user_messages_only():
    assert make(USER_TURN).turns == 1


def test_the_transcript_is_written_owner_only():
    """A transcript holds whatever the user pasted into it."""
    session = make(USER_TURN)
    mode = stat.S_IMODE(os.stat(session.path).st_mode)
    assert mode == 0o600, f"session file is {oct(mode)}"


def test_recent_is_newest_first():
    make(USER_TURN, updated=time.time() - 500)
    newer = make([{"role": "user", "content": "newer"}], updated=time.time())
    assert store.recent()[0].id == newer.id


def test_recent_skips_empty_sessions():
    store.Session().save()  # no messages
    make(USER_TURN)
    assert len(store.recent()) == 1


def test_an_unreadable_file_does_not_hide_the_others():
    make(USER_TURN)
    (store.sessions_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    assert len(store.recent()) == 1


def test_latest_returns_the_newest():
    make(USER_TURN, updated=time.time() - 500)
    newer = make([{"role": "user", "content": "newer"}], updated=time.time())
    assert store.latest().id == newer.id


def test_latest_is_none_when_there_are_none():
    assert store.latest() is None


class TestResolve:
    def test_an_exact_id_resolves(self):
        session = make(USER_TURN)
        assert store.resolve(session.id).id == session.id

    def test_a_unique_prefix_resolves(self):
        session = make(USER_TURN, session_id="abcdef123456")
        assert store.resolve("abcdef").id == session.id

    def test_an_ambiguous_prefix_resolves_to_nothing(self):
        make(USER_TURN, session_id="abcdef111111")
        make([{"role": "user", "content": "other"}], session_id="abcdef222222")
        assert store.resolve("abcdef") is None

    def test_an_unknown_prefix_resolves_to_nothing(self):
        make(USER_TURN)
        assert store.resolve("zzzzzz") is None

    def test_an_empty_prefix_resolves_to_nothing(self):
        make(USER_TURN)
        assert store.resolve("  ") is None


class TestSearch:
    def test_finds_the_matching_line(self):
        session = make(USER_TURN)
        results = store.search("build the thing")
        assert results and results[0][0].id == session.id
        assert "build the thing" in results[0][1]

    def test_search_is_case_insensitive(self):
        make(USER_TURN)
        assert store.search("BUILD THE THING")

    def test_a_session_matches_once(self):
        make(
            [
                {"role": "user", "content": "needle"},
                {"role": "assistant", "content": "needle needle"},
            ]
        )
        assert len(store.search("needle")) == 1

    def test_an_empty_query_finds_nothing(self):
        make(USER_TURN)
        assert store.search("   ") == []

    def test_no_match_finds_nothing(self):
        make(USER_TURN)
        assert store.search("kubernetes") == []


def test_saving_leaves_no_temporary_file_behind():
    make(USER_TURN)
    assert list(store.sessions_dir().glob("*.tmp")) == []


def test_search_ignores_the_system_prompt():
    """It carries the skills manifest and every standing memory."""
    make(
        [
            {"role": "system", "content": "You know: the user likes kubernetes"},
            {"role": "user", "content": "something unrelated"},
        ]
    )
    assert store.search("kubernetes") == []
    assert store.search("unrelated")
