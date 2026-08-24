"""Search: the query sanitizer, the three routes, and the filters.

The sanitizer gets the most attention because its failure mode is the worst
one available — FTS5 raises on a grammar it does not like, and a caller that
swallows that reports "nothing found", which is indistinguishable from the
truth.
"""

from __future__ import annotations

import sqlite3

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.state import db as db_module
from andromeda_cli.state import filters as filters_module
from andromeda_cli.state import queries as queries_module


def indexed(messages, **kwargs):
    session = store.Session()
    session.messages = list(messages)
    session.model = kwargs.get("model", "test/model")
    session.provider = kwargs.get("provider", "relay")
    session.workspace = kwargs.get("workspace", "/tmp/w")
    session.save()
    state.index_session(session)
    return session


class TestTheSanitizer:
    @pytest.mark.parametrize(
        "raw",
        [
            "TODO: fix the thing",
            "it's broken",
            "gateway/run.py",
            "user@host",
            "a,b",
            "50% slower",
            "(parenthesised)",
            "trailing AND",
            "OR leading",
            "***",
            "* prefix",
            'unbalanced "quote',
            "^caret $dollar ~tilde",
        ],
    )
    def test_every_shape_survives_a_real_match(self, raw):
        """Asserted against a live FTS5 table, not against the regex. The
        characters that raise were found this way; reasoning about the grammar
        found only some of them."""
        indexed([{"role": "user", "content": "something to match"}])
        with db_module.connect() as conn:
            match = queries_module.sanitize(raw)
            if not match:
                return
            conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (match,)
            ).fetchall()

    def test_a_balanced_phrase_is_preserved(self):
        assert queries_module.sanitize('"retry budget"') == '"retry budget"'

    def test_a_hyphenated_term_is_quoted_not_split(self):
        """FTS5 tokenizes on the hyphen, so `chat-send` would mean
        `chat AND send` and match sessions that discussed neither together."""
        assert queries_module.sanitize("chat-send") == '"chat-send"'

    def test_a_dotted_term_is_quoted(self):
        assert queries_module.sanitize("P2.2") == '"P2.2"'

    def test_a_path_is_quoted_once_not_twice(self):
        """The bug a sequential dotted-then-hyphenated pass would produce."""
        assert queries_module.sanitize("my-app.config.ts") == '"my-app.config.ts"'

    def test_a_long_query_is_bounded(self):
        assert len(queries_module.sanitize("x " * 5000)) <= queries_module.MAX_QUERY_CHARS

    def test_a_colon_query_finds_what_it_should(self):
        """The failure this exists for: `TODO: fix` parses as a column filter,
        raises "no such column", and the caller reports nothing found."""
        indexed([{"role": "user", "content": "TODO: fix the retry budget"}])
        assert len(state.search("TODO: fix")) == 1


class TestRouting:
    def test_plain_text_uses_the_default_index(self):
        indexed([{"role": "user", "content": "the retry budget"}])
        assert state.search("retry budget")[0].route == "fts"

    def test_cjk_uses_the_trigram_index(self):
        """The default tokenizer splits CJK into single characters, so phrase
        matching against it does not work at all."""
        indexed([{"role": "user", "content": "デプロイの手順を教えて"}])
        hits = state.search("デプロイ")
        assert hits and hits[0].route == "trigram"

    def test_a_short_cjk_term_falls_back_to_substring(self):
        """A token shorter than three characters produces no trigrams and can
        never match, so the trigram route would return nothing."""
        indexed([{"role": "user", "content": "手順を教えて"}])
        hits = state.search("手順")
        assert hits and hits[0].route == "like"

    def test_a_query_that_sanitizes_to_nothing_falls_back(self):
        indexed([{"role": "user", "content": "a & b"}])
        assert queries_module.sanitize("&") == ""
        hits = state.search("&")
        assert all(hit.route == "like" for hit in hits)

    def test_a_raising_match_falls_back_rather_than_reporting_nothing(
        self, monkeypatch
    ):
        indexed([{"role": "user", "content": "the retry budget"}])

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("fts5: syntax error")

        monkeypatch.setattr(queries_module, "_fts_search", explode)
        hits = state.search("retry budget")
        assert [hit.route for hit in hits] == ["like"]


class TestSubstringFallback:
    def test_terms_are_anded_not_concatenated(self):
        """A person typing two words expects both present, which is what every
        other route does."""
        indexed([{"role": "user", "content": "alpha only"}])
        both = indexed([{"role": "user", "content": "alpha and beta"}])
        rows = queries_module._like_search
        with db_module.connect() as conn:
            found = rows(
                conn,
                "alpha beta",
                filters=state.Filters(),
                limit=10,
                offset=0,
            )
        assert {row["session_id"] for row in found} == {both.id}

    def test_a_wildcard_in_the_query_matches_literally(self):
        """`_` is a LIKE wildcard and is common in tool names; a match
        documented as "contains" must not silently widen."""
        exact = indexed([{"role": "user", "content": "call read_file now"}])
        indexed([{"role": "user", "content": "call readXfile now"}])
        with db_module.connect() as conn:
            found = queries_module._like_search(
                conn, "read_file", filters=state.Filters(), limit=10, offset=0
            )
        assert {row["session_id"] for row in found} == {exact.id}


class TestFilters:
    def test_a_role_filter_narrows_to_what_was_asked(self):
        indexed(
            [
                {"role": "user", "content": "the retry budget"},
                {"role": "assistant", "content": "the retry budget is three"},
            ]
        )
        hits = state.search("retry budget", filters=state.Filters(roles=("user",)))
        assert [hit.role for hit in hits] == ["user"]

    def test_a_workspace_filter_narrows_by_path(self):
        here = indexed(
            [{"role": "user", "content": "the retry budget"}], workspace="/tmp/here"
        )
        indexed(
            [{"role": "user", "content": "the retry budget"}], workspace="/tmp/there"
        )
        hits = state.search(
            "retry budget", filters=state.Filters(workspace="/tmp/here")
        )
        assert [hit.session_id for hit in hits] == [here.id]

    def test_a_time_filter_excludes_older_sessions(self):
        import time

        old = indexed([{"role": "user", "content": "the retry budget"}])
        with db_module.connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time() - 86400 * 10, old.id),
            )
        assert state.search("retry budget", filters=state.Filters(since=time.time() - 3600)) == []

    @pytest.mark.parametrize(
        "text,offset",
        [("7d", 604800), ("2h", 7200), ("30m", 1800), ("1w", 604800)],
    )
    def test_relative_times_parse(self, text, offset):
        now = 1_800_000_000.0
        assert filters_module.parse_when(text, now=now) == now - offset

    def test_an_absolute_date_parses(self):
        assert filters_module.parse_when("2026-08-01") > 0

    def test_today_means_the_start_of_today_not_this_instant(self):
        """`--since today` returning nothing is never what was meant."""
        now = 1_800_000_000.0
        assert filters_module.parse_when("today", now=now) < now

    def test_an_unreadable_time_is_an_error_not_no_filter(self):
        """Silently ignoring it searches a wider range than was asked for and
        looks like a successful search."""
        with pytest.raises(filters_module.FilterError):
            filters_module.parse_when("lastweek")


class TestReadingAround:
    def test_bookends_carry_the_opening_and_the_closing(self):
        """A match deep in a long session says what was said and nothing about
        what it was for."""
        session = indexed(
            [{"role": "user", "content": f"message number {n}"} for n in range(30)]
        )
        rows = state.transcript(session.id)
        view = state.anchored(session.id, rows[15]["id"], window=1, bookend=2)
        assert [row["position"] for row in view["opening"]] == [0, 1]
        assert [row["position"] for row in view["closing"]] == [28, 29]
        assert view["before"] and view["after"]

    def test_the_anchor_survives_a_role_filter(self):
        """A filter that drops the message you asked to see is not a filter."""
        session = indexed(
            [
                {"role": "user", "content": "run it"},
                {"role": "tool", "name": "terminal", "content": "exit 0"},
                {"role": "assistant", "content": "done"},
            ]
        )
        tool_row = state.transcript(session.id)[1]
        view = state.anchored(
            session.id, tool_row["id"], window=1, roles=("user", "assistant")
        )
        assert tool_row["id"] in [row["id"] for row in view["window"]]

    def test_an_anchor_that_is_not_there_returns_empty(self):
        session = indexed([{"role": "user", "content": "hello"}])
        assert state.anchored(session.id, 999_999)["window"] == []

    def test_an_anchor_survives_compaction_restarting_the_positions(self):
        """The reason anchors are row ids: compaction folds turns away and the
        new transcript starts at position 0 again, so an offset handed out
        before it would silently address a different message."""
        session = indexed(
            [{"role": "user", "content": f"message number {n}"} for n in range(6)]
        )
        rows = state.transcript(session.id)
        anchor = rows[1]["id"]
        state.archive_range(session.id, 0, 3)

        session.messages = [{"role": "user", "content": "a fresh start"}]
        session.save()
        state.index_session(session)

        view = state.anchored(session.id, anchor, window=0, bookend=0)
        assert [row["content"] for row in view["window"]] == ["message number 1"]
        assert view["archived"] == 4


class TestListingAndResolving:
    def test_recent_is_newest_first(self):
        import time

        first = indexed([{"role": "user", "content": "one"}])
        time.sleep(0.01)
        second = indexed([{"role": "user", "content": "two"}])
        assert [row.id for row in state.recent()][:2] == [second.id, first.id]

    def test_a_prefix_resolves_like_a_short_sha(self):
        session = indexed([{"role": "user", "content": "hello"}])
        assert state.resolve_prefix(session.id[:6]) == [session.id]

    def test_a_title_search_is_separate_from_a_message_search(self):
        """"The session about the retry budget" is a different question from
        "sessions mentioning retry budget"."""
        opener = indexed([{"role": "user", "content": "the retry budget please"}])
        indexed(
            [
                {"role": "user", "content": "unrelated"},
                {"role": "assistant", "content": "the retry budget came up"},
            ]
        )
        assert [row.id for row in state.by_title("retry budget")] == [opener.id]
