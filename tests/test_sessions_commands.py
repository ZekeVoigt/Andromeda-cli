"""The `andromeda sessions` and `andromeda profile` verbs, and `/resume`."""

from __future__ import annotations

import json

import pytest

from andromeda_cli import profiles
from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.__main__ import build_command_parser, main
from andromeda_cli.commands import sessions as sessions_cmd


def written(text="the retry budget is three", indexed=True):
    session = store.Session()
    session.messages = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": f"Yes — {text}."},
    ]
    session.model = "test/model"
    session.provider = "relay"
    session.workspace = "/tmp/w"
    session.save()
    if indexed:
        state.index_session(session)
    return session


class TestParsing:
    def test_the_filters_are_the_same_on_list_and_search(self):
        """Two copies of a filter set is how `--since` ends up meaning one
        thing in a listing and another in a search."""
        parser = build_command_parser()
        listing = parser.parse_args(["sessions", "list", "--since", "7d"])
        searching = parser.parse_args(["sessions", "search", "x", "--since", "7d"])
        assert listing.since == searching.since == "7d"

    def test_export_defaults_to_markdown_on_stdout(self):
        args = build_command_parser().parse_args(["sessions", "export", "abc"])
        assert args.fmt == "markdown" and args.out == ""

    def test_recover_has_two_distinct_repairs(self):
        args = build_command_parser().parse_args(
            ["sessions", "recover", "--apply", "--rebuild-index"]
        )
        assert args.apply and args.rebuild_index

    def test_profile_create_accepts_both_clone_shapes(self):
        args = build_command_parser().parse_args(
            ["profile", "create", "work", "--clone-all"]
        )
        assert args.clone_all and not args.clone


class TestTheProfileFlag:
    def test_it_is_taken_out_of_argv_before_anything_reads_it(self):
        from andromeda_cli.__main__ import _take_profile

        assert _take_profile(["sessions", "-p", "work", "search", "x"]) == (
            ["sessions", "search", "x"],
            "work",
        )

    def test_the_equals_form_works_too(self):
        from andromeda_cli.__main__ import _take_profile

        assert _take_profile(["--profile=work", "doctor"]) == (["doctor"], "work")

    def test_a_dangling_flag_is_left_for_argparse_to_report(self):
        """Rather than this silently dropping a flag somebody typed."""
        from andromeda_cli.__main__ import _take_profile

        assert _take_profile(["doctor", "-p"]) == (["doctor", "-p"], "")

    def test_naming_a_profile_that_does_not_exist_is_a_usage_error(self, monkeypatch):
        monkeypatch.delenv("ANDROMEDA_HOME", raising=False)
        assert main(["-p", "nope", "sessions"]) == 2


class TestListingAndSearching:
    def test_an_empty_install_says_so(self, capsys):
        assert sessions_cmd.show_list() == 0
        assert "No saved sessions" in capsys.readouterr().out

    def test_a_session_written_behind_the_index_still_lists(self, capsys):
        """The index catching up is what makes every verb correct on an
        install where a session was written by a process that could not
        reach the database."""
        session = written(indexed=False)
        sessions_cmd.show_list()
        assert session.id in capsys.readouterr().out

    def test_search_finds_it(self, capsys):
        session = written()
        assert sessions_cmd.find("retry budget") == 0
        assert session.id in capsys.readouterr().out

    def test_search_reports_the_route_it_used(self, capsys):
        """So a surprising result set can be explained rather than guessed at."""
        written()
        sessions_cmd.find("retry budget")
        assert "via fts" in capsys.readouterr().out

    def test_a_miss_offers_title_matches(self, capsys):
        """A partial word matches no token, so full-text search misses it —
        but the session whose opening line contains it is still the answer."""
        session = written("the colima dns thing")
        assert state.search("olima") == []
        assert sessions_cmd.find("olima") == 0
        printed = capsys.readouterr().out
        assert "open with it" in printed and session.id in printed

    def test_an_unreadable_time_is_a_usage_error(self, capsys):
        assert sessions_cmd.show_list(since="lastweek") == 2

    def test_the_filters_are_reported_when_nothing_matches(self, capsys):
        written()
        sessions_cmd.show_list(workspace="/nowhere")
        assert "No sessions matching" in capsys.readouterr().out


class TestShowExportAndRemove:
    def test_show_reads_the_file_not_the_index(self, capsys):
        """A session you cannot open because the index is broken is exactly
        the session you most want to be able to open."""
        session = written(indexed=False)
        state.db_path().unlink(missing_ok=True)
        assert sessions_cmd.show(session.id) == 0
        assert "retry budget" in capsys.readouterr().out

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self, capsys):
        first = written()
        second = store.Session()
        second.id = first.id[:4] + "zzzzzzzz"
        second.messages = [{"role": "user", "content": "other"}]
        second.save()
        state.index_session(second)
        assert sessions_cmd.show(first.id[:4]) == 2

    def test_export_to_stdout_is_unrendered(self, capsys):
        """rich would wrap it, colour it and corrupt the file on the other
        end of the pipe."""
        session = written()
        assert sessions_cmd.export(session.id, "markdown") == 0
        assert "\x1b[" not in capsys.readouterr().out

    def test_export_to_a_directory_names_the_file_after_the_session(self, tmp_path):
        session = written()
        sessions_cmd.export(session.id, "html", str(tmp_path))
        assert (tmp_path / f"{session.id}.html").exists()

    def test_removing_asks_first(self, capsys):
        session = written()
        assert sessions_cmd.remove(session.id) == 2
        assert session.path.exists()

    def test_removing_with_force_drops_it_from_the_index_too(self):
        session = written()
        assert sessions_cmd.remove(session.id, force=True) == 0
        assert not session.path.exists()
        assert state.search("retry budget") == []


class TestHealthVerbs:
    def test_doctor_is_clean_on_a_healthy_install(self, capsys):
        written()
        state.reindex()
        assert sessions_cmd.doctor() == 0
        assert "Everything readable and indexed" in capsys.readouterr().out

    def test_doctor_reports_a_damaged_transcript(self, capsys):
        session = written()
        text = session.path.read_text(encoding="utf-8")
        session.path.write_text(text[: len(text) // 2], encoding="utf-8")
        assert sessions_cmd.doctor() == 1
        assert "recoverable" in capsys.readouterr().out

    def test_reindex_reports_the_split(self, capsys):
        written(indexed=False)
        assert sessions_cmd.reindex() == 0
        assert "1 rebuilt" in capsys.readouterr().out

    def test_recover_defaults_to_a_dry_run(self, capsys):
        session = written()
        text = session.path.read_text(encoding="utf-8")
        session.path.write_text(text[: len(text) // 2], encoding="utf-8")
        assert sessions_cmd.recover() == 0
        assert "Nothing was changed" in capsys.readouterr().out

    def test_active_says_nothing_is_open(self, capsys):
        assert sessions_cmd.active() == 0
        assert "No sessions open" in capsys.readouterr().out


class TestProfileVerbs:
    @pytest.fixture(autouse=True)
    def own_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANDROMEDA_HOME", raising=False)
        monkeypatch.delenv(profiles.ENV_PROFILE, raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def test_deleting_without_force_names_what_would_go(self, capsys):
        from andromeda_cli.commands import profile as profile_cmd

        profiles.create("work")
        assert profile_cmd.delete("work") == 2
        # `output.fail` writes to stderr, which is where a refusal belongs.
        assert "permanently delete" in capsys.readouterr().err
        assert profiles.exists("work")

    def test_the_listing_marks_the_current_profile(self, capsys):
        from andromeda_cli.commands import profile as profile_cmd

        profiles.create("work")
        profiles.use("work")
        profile_cmd.show_list()
        assert "work" in capsys.readouterr().out


class TestShowingCompactedTurns:
    """Compaction removes turns from the transcript on disk, so the file alone
    stops being the conversation. A person reading a session should see what
    the agent can still search."""

    def compacted(self):
        session = written("the retry budget is three")
        state.archive_range(session.id, 0, 0)
        session.messages = [{"role": "user", "content": "a fresh start"}]
        session.save()
        state.index_session(session)
        return session

    def test_they_are_shown_ahead_of_the_live_transcript(self, capsys):
        session = self.compacted()
        assert sessions_cmd.show(session.id) == 0
        printed = capsys.readouterr().out
        assert "compacted out" in printed
        assert printed.index("retry budget") < printed.index("a fresh start")

    def test_live_only_leaves_them_out(self, capsys):
        session = self.compacted()
        sessions_cmd.show(session.id, live_only=True)
        printed = capsys.readouterr().out
        assert "retry budget" not in printed and "a fresh start" in printed

    def test_the_file_is_still_readable_without_the_index(self, capsys):
        """A session you cannot open because the index is broken is exactly
        the one you most want to open."""
        session = self.compacted()
        state.db_path().unlink(missing_ok=True)
        assert sessions_cmd.show(session.id) == 0
        assert "a fresh start" in capsys.readouterr().out

    def test_doctor_reports_them_as_kept_only_there(self, capsys):
        self.compacted()
        sessions_cmd.doctor()
        assert "compacted out, kept only here" in capsys.readouterr().out
