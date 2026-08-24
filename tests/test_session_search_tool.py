"""The `session_search` tool: four shapes, one schema.

The dispatch rules matter more than the rendering. A model that asked for a
specific slice and got a search back has been answered a question it did not
ask, and it will usually accept the wrong answer.
"""

from __future__ import annotations

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_tools import session_search


def indexed(messages, model="test/model"):
    session = store.Session()
    session.messages = list(messages)
    session.model = model
    session.workspace = "/tmp/w"
    session.save()
    state.index_session(session)
    return session


EXCHANGE = [
    {"role": "user", "content": "what did we decide about the retry budget"},
    {"role": "assistant", "content": "Three attempts, then stop."},
]


class TestDispatch:
    def test_nothing_browses_the_recent_sessions(self):
        session = indexed(EXCHANGE)
        result = session_search.run()
        assert session.id in result.content

    def test_a_query_discovers(self):
        indexed(EXCHANGE)
        result = session_search.run(query="retry budget")
        assert "match(es) for" in result.content

    def test_a_session_id_reads(self):
        session = indexed(EXCHANGE)
        result = session_search.run(session_id=session.id)
        assert "Three attempts" in result.content

    def test_an_anchor_beats_a_query(self):
        """The model asked for a specific slice."""
        session = indexed(EXCHANGE)
        anchor = state.transcript(session.id)[0]["id"]
        result = session_search.run(
            query="something else entirely", session_id=session.id, anchor=anchor
        )
        assert "Session " + session.id in result.content

    def test_a_bad_anchor_is_refused_rather_than_guessed(self):
        session = indexed(EXCHANGE)
        assert session_search.run(session_id=session.id, anchor="soon").ok is False


class TestDiscovery:
    def test_a_miss_falls_back_to_matching_titles(self):
        """"The session about X" is a different question from "sessions
        mentioning X", and answering only the second buries the first."""
        session = indexed(
            [{"role": "user", "content": "the colima dns thing"}]
        )
        result = session_search.run(query="colima dns")
        assert session.id in result.content

    def test_nothing_found_says_so_without_claiming_it_never_happened(self):
        indexed(EXCHANGE)
        result = session_search.run(query="quantum tunnelling")
        assert "may simply not have been discussed" in result.content

    def test_results_name_the_anchor_to_read_next(self):
        indexed(EXCHANGE)
        result = session_search.run(query="retry budget")
        assert "anchor=" in result.content

    def test_a_role_filter_narrows(self):
        indexed(EXCHANGE)
        result = session_search.run(query="retry budget", role="assistant")
        assert "[user]" not in result.content

    @pytest.mark.parametrize(
        "limit,expected",
        [
            # 0 and a non-number both mean "the model did not really choose",
            # so both land on the default rather than on an empty result.
            (0, session_search.DEFAULT_LIMIT),
            (99, session_search.MAX_LIMIT),
            ("x", session_search.DEFAULT_LIMIT),
        ],
    )
    def test_the_limit_is_clamped_not_trusted(self, limit, expected):
        for index in range(30):
            indexed([{"role": "user", "content": f"topic {index} retry"}])
        result = session_search.run(query="retry", limit=limit)
        assert result.metadata.get("count", 0) <= expected


class TestReadingInContext:
    def test_a_scroll_shows_how_the_session_opened_and_ended(self):
        session = indexed(
            [{"role": "user", "content": f"message {n}"} for n in range(30)]
        )
        anchor = state.transcript(session.id)[15]["id"]
        result = session_search.run(session_id=session.id, anchor=anchor, window=1)
        assert "how it started" in result.content
        assert "how it ended" in result.content

    def test_the_anchor_is_marked(self):
        session = indexed(EXCHANGE)
        anchor = state.transcript(session.id)[0]["id"]
        result = session_search.run(session_id=session.id, anchor=anchor)
        assert "←" in result.content

    def test_a_missing_anchor_asks_for_a_fresh_search(self):
        session = indexed(EXCHANGE)
        result = session_search.run(session_id=session.id, anchor=999_999)
        assert result.ok is False
        assert "Search again" in result.content

    def test_a_compacted_hit_is_labelled(self):
        """"I read this earlier" and "this is in front of me now" are
        different claims."""
        session = indexed(EXCHANGE)
        state.archive_range(session.id, 0, 0)
        result = session_search.run(query="retry budget")
        assert "compacted" in result.content

    def test_reading_a_session_reports_how_much_was_compacted_out(self):
        session = indexed(EXCHANGE)
        state.archive_range(session.id, 0, 0)
        result = session_search.run(session_id=session.id)
        assert "compacted out" in result.content

    def test_a_long_read_is_elided_rather_than_dumped(self):
        session = indexed(
            [{"role": "user", "content": f"message {n}"} for n in range(200)]
        )
        result = session_search.run(session_id=session.id)
        assert "message(s) omitted" in result.content
        assert len(result.content.splitlines()) < 30

    def test_an_id_prefix_resolves(self):
        session = indexed(EXCHANGE)
        result = session_search.run(session_id=session.id[:6])
        assert "Three attempts" in result.content

    def test_an_unknown_id_fails_rather_than_returning_nothing(self):
        assert session_search.run(session_id="deadbeef").ok is False


class TestTheContract:
    def test_it_is_registered_and_enabled_by_default(self):
        from andromeda_tools import DEFAULT_ENABLED

        assert "session_search" in DEFAULT_ENABLED

    def test_it_is_a_safe_local_read(self, tmp_path):
        """So a read-only lane can check whether something was discussed
        before, which is the case it exists for."""
        from andromeda_tools import MemoryStore, Workspace, build_registry
        from andromeda_tools.todo import TodoList
        from andromeda_agent.specialists import is_read_only

        registry = build_registry(
            Workspace(tmp_path), TodoList(), {}, MemoryStore(tmp_path)
        )
        spec = registry["session_search"]
        assert spec.risk_tier == "safe_local" and spec.category == "read"
        assert is_read_only(spec)

    def test_it_is_not_a_delegation_tool(self):
        """SESSION_TOOLS is a closed list about spawning children, and reading
        history is not that."""
        from andromeda_agent.specialists import SESSION_TOOLS

        assert "session_search" not in SESSION_TOOLS

    def test_the_summary_says_what_will_happen(self):
        assert "search past sessions" in session_search.summarize({"query": "x"})
        assert "read session" in session_search.summarize({"session_id": "abc"})
        assert "around 7" in session_search.summarize(
            {"session_id": "abc", "anchor": 7}
        )
        assert "recent" in session_search.summarize({})
