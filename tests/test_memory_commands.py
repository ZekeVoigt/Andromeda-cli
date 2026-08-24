"""`andromeda memory` — the person's side of the store.

Standing memories go into every prompt, so a wrong one is a false premise the
agent argues from until somebody removes it. Until this existed the only way to
remove one was to ask the agent to, which requires knowing it is there.
"""

from __future__ import annotations

import json

import pytest

from andromeda_cli import config as config_module
from andromeda_cli.commands import memory_cmd


@pytest.fixture(params=["json", "sqlite"])
def backend(request):
    """Every command goes through the store, so both backends must behave the
    same — otherwise the two can be left disagreeing."""
    config_module.set_value("memory_backend", request.param)
    return request.param


class TestListing:
    def test_an_empty_store_says_so(self, backend, capsys):
        assert memory_cmd.show_list() == 0
        assert "Nothing remembered" in capsys.readouterr().out

    def test_standing_memories_are_marked(self, backend, capsys):
        memory_cmd.remember("Zeke prefers clickable links", scope="standing")
        memory_cmd.remember("the retry budget is three")
        capsys.readouterr()

        memory_cmd.show_list()
        printed = capsys.readouterr().out
        assert "★" in printed
        assert "1 standing" in printed

    def test_a_scope_filter_narrows(self, backend, capsys):
        memory_cmd.remember("a standing thing", scope="standing")
        memory_cmd.remember("an episodic thing")
        capsys.readouterr()

        memory_cmd.show_list(scope="standing")
        printed = capsys.readouterr().out
        assert "a standing thing" in printed and "an episodic thing" not in printed

    def test_an_unknown_scope_is_a_usage_error(self, backend):
        assert memory_cmd.show_list(scope="permanent") == 2

    def test_it_names_where_the_memories_really_are(self, backend, capsys):
        memory_cmd.remember("something")
        capsys.readouterr()
        memory_cmd.show_list()
        printed = capsys.readouterr().out
        assert ("memories.json" in printed) == (backend == "json")
        assert ("state.db" in printed) == (backend == "sqlite")


class TestRemembering:
    def test_it_stores_without_spending_a_turn(self, backend, capsys):
        assert memory_cmd.remember("the retry budget is three") == 0
        assert "Remembered" in capsys.readouterr().out

    def test_standing_says_what_that_costs(self, backend, capsys):
        memory_cmd.remember("a rule", scope="standing")
        assert "every prompt" in capsys.readouterr().out

    def test_tags_are_split_on_commas(self, backend):
        from andromeda_tools import MemoryStore

        memory_cmd.remember("something", tags="a, b ,c")
        store = MemoryStore(config_module.home() / "memory", backend)
        assert store.load()[0].tags == ["a", "b", "c"]

    def test_empty_content_is_refused(self, backend):
        assert memory_cmd.remember("   ") == 2


class TestSearching:
    def test_it_shows_what_the_agent_would_receive(self, backend, capsys):
        """"Why did it not recall that" is answered by seeing the same thing
        it saw."""
        memory_cmd.remember("the retry budget is three attempts")
        capsys.readouterr()

        assert memory_cmd.find("retry budget") == 0
        assert "[episode] the retry budget is three attempts" in capsys.readouterr().out

    def test_a_miss_is_reported_as_one(self, backend, capsys):
        memory_cmd.remember("something unrelated")
        capsys.readouterr()
        memory_cmd.find("kubernetes")
        assert "Nothing remembered" in capsys.readouterr().out


class TestForgetting:
    def test_it_shows_what_would_go_before_doing_it(self, backend, capsys):
        """Forgetting matches generously by design, so a count alone does not
        tell you it caught the right ones."""
        memory_cmd.remember("Zeke lives in Phoenix")
        capsys.readouterr()

        assert memory_cmd.forget("lives in Phoenix") == 2
        captured = capsys.readouterr()
        assert "Zeke lives in Phoenix" in captured.out
        assert "would forget 1" in captured.err

    def test_force_actually_removes_it(self, backend, capsys):
        from andromeda_tools import MemoryStore

        memory_cmd.remember("Zeke lives in Phoenix")
        assert memory_cmd.forget("lives in Phoenix", force=True) == 0
        assert MemoryStore(config_module.home() / "memory", backend).load() == []

    def test_a_miss_changes_nothing(self, backend):
        memory_cmd.remember("something")
        assert memory_cmd.forget("kubernetes", force=True) == 1

    def test_a_scope_can_protect_standing_memories(self, backend):
        from andromeda_tools import MemoryStore

        memory_cmd.remember("the retry budget", scope="standing")
        memory_cmd.remember("the retry budget in this run")
        memory_cmd.forget("retry budget", scope="episode", force=True)
        remaining = MemoryStore(config_module.home() / "memory", backend).load()
        assert [item.scope for item in remaining] == ["standing"]

    def test_an_unknown_scope_is_a_usage_error(self, backend):
        assert memory_cmd.forget("x", scope="permanent") == 2


class TestExportAndStats:
    def test_export_writes_json_on_either_backend(self, backend, tmp_path, capsys):
        """The only way to see a sqlite-backed store as text."""
        memory_cmd.remember("the retry budget is three")
        destination = tmp_path / "out.json"
        assert memory_cmd.export(str(destination)) == 0
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert payload[0]["content"] == "the retry budget is three"

    def test_export_to_stdout_is_plain(self, backend, capsys):
        memory_cmd.remember("something")
        capsys.readouterr()
        memory_cmd.export()
        assert json.loads(capsys.readouterr().out)[0]["content"] == "something"

    def test_stats_names_the_backend_and_the_cap(self, backend, capsys):
        memory_cmd.remember("a rule", scope="standing")
        capsys.readouterr()
        assert memory_cmd.stats() == 0
        printed = capsys.readouterr().out
        assert backend in printed
        assert "of 40" in printed


class TestBackendSubstitution:
    def test_a_fallback_is_said_once_rather_than_hidden(self, capsys, monkeypatch):
        """Every listing would otherwise be silently from a different backend
        than the one configured."""
        from andromeda_tools import memory_backends

        config_module.set_value("memory_backend", "sqlite")
        monkeypatch.setattr(
            memory_backends.SqliteBackend, "available", lambda _self: False
        )
        memory_cmd.show_list()
        assert "unavailable" in capsys.readouterr().out
