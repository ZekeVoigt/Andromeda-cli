"""Pluggable memory storage, and the one thing that is not pluggable.

`minScore` means "this fraction of the query's meaningful terms appear in the
memory". A backend that swapped in its own ranking would keep the parameter
name while changing what a given number does, so a threshold tuned on one
install would mean something else on another. These tests pin that the two
backends agree on scores and disagree only on where the bytes live.
"""

from __future__ import annotations

import json

import pytest

from andromeda_tools import memory_backends
from andromeda_tools.memory import MemoryStore


@pytest.fixture(params=["json", "sqlite"])
def store(request, tmp_path):
    return MemoryStore(tmp_path / "memory", request.param)


class TestBothBackends:
    def test_a_memory_round_trips(self, store):
        store.store("Zeke prefers file paths as clickable links")
        assert "clickable links" in store.search("clickable links").content

    def test_scopes_are_kept_apart(self, store):
        store.store("a standing thing", scope="standing")
        store.store("an episodic thing", scope="episode")
        assert [item.content for item in store.standing()] == ["a standing thing"]

    def test_restating_a_fact_consolidates_it(self, store):
        store.store("Zeke prefers clickable file links")
        result = store.store("Zeke prefers clickable file links")
        assert result.metadata["superseded"] == 1
        assert len(store.load()) == 1

    def test_replaces_removes_what_it_makes_untrue(self, store):
        store.store("Zeke lives in Phoenix")
        store.store("Zeke lives in Denver", replaces="lives in Phoenix")
        assert [item.content for item in store.load()] == ["Zeke lives in Denver"]

    def test_forgetting_removes_matching_memories(self, store):
        store.store("Zeke lives in Phoenix")
        assert store.forget("lives in Phoenix").metadata["removed"] == 1
        assert store.load() == []

    def test_tags_survive_a_round_trip(self, store):
        """The sqlite backend joins them into one column, and a separator a
        tag could itself contain would silently turn one tag into two."""
        store.store("something", tags=["a,b", "c"])
        assert store.load()[0].tags == ["a,b", "c"]

    def test_a_miss_says_nothing_was_remembered(self, store):
        store.store("Zeke prefers clickable links")
        assert "Nothing remembered" in store.search("kubernetes").content


class TestScoringIsNotPluggable:
    def test_both_backends_rank_the_same_query_identically(self, tmp_path):
        facts = [
            "the retry budget is three attempts",
            "colima lost dns again",
            "retry the deploy tomorrow",
        ]
        results = []
        for name in ("json", "sqlite"):
            store = MemoryStore(tmp_path / f"memory-{name}", name)
            for fact in facts:
                store.store(fact)
            results.append(store.search("retry budget", limit=5).content)
        assert results[0] == results[1]

    def test_min_score_means_the_same_on_both(self, tmp_path):
        for name in ("json", "sqlite"):
            store = MemoryStore(tmp_path / f"memory-{name}", name)
            store.store("the retry budget is three attempts")
            assert "Nothing remembered" in store.search(
                "retry budget kubernetes helm", min_score=0.9
            ).content


class TestCandidateNarrowing:
    def test_the_sqlite_backend_narrows_but_never_omits_a_match(self, tmp_path):
        """A search that silently sees fewer memories is worse than a slow
        one, so narrowing may only ever remove non-matches."""
        store = MemoryStore(tmp_path / "memory", "sqlite")
        for index in range(50):
            store.store(f"fact number {index} about widgets")
        store.store("the retry budget is three")
        candidates = store.backend.candidates("retry budget")
        assert any("retry budget" in item.content for item in candidates)
        assert len(candidates) < 51

    def test_a_partial_term_match_is_still_a_candidate(self, tmp_path):
        """The bug this pins: FTS5 ANDs adjacent terms, which is stricter than
        the 0.3 default `minScore`, so an ANDed candidate query would never
        offer a memory covering one term of two — and the two backends would
        then recall different things from the same store."""
        store = MemoryStore(tmp_path / "memory", "sqlite")
        store.store("retry the deploy tomorrow")
        assert store.backend.candidates("retry budget")
        assert "retry the deploy" in store.search("retry budget").content

    def test_a_failed_narrowing_falls_back_to_everything(self, tmp_path, monkeypatch):
        import sqlite3

        store = MemoryStore(tmp_path / "memory", "sqlite")
        store.store("the retry budget is three")

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("malformed MATCH")

        monkeypatch.setattr(store.backend, "_connect", explode)
        assert store.backend.candidates("retry budget") == []


class TestSelection:
    def test_the_default_is_the_readable_file(self, tmp_path):
        store = MemoryStore(tmp_path / "memory")
        assert store.backend.name == "json"
        assert store.file.name == "memories.json"

    def test_an_unknown_name_falls_back_and_says_so(self, tmp_path):
        """A typo in a setting must not take away the agent's memory — the
        same rule `cron_provider` follows."""
        store = MemoryStore(tmp_path / "memory", "postgres")
        assert store.backend.name == "json"
        assert "unknown memory backend" in store.backend_note

    def test_an_unavailable_backend_falls_back_and_says_why(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            memory_backends.SqliteBackend, "available", lambda _self: False
        )
        backend, note = memory_backends.build("sqlite", tmp_path)
        assert backend.name == "json"
        assert "unavailable" in note

    def test_the_file_property_names_where_the_memories_really_are(self, tmp_path):
        """The agent tells the user this path when asked where its memory is."""
        assert MemoryStore(tmp_path, "sqlite").file.name == "state.db"

    def test_a_backend_instance_can_be_passed_directly(self, tmp_path):
        backend = memory_backends.JsonBackend(tmp_path)
        assert MemoryStore(tmp_path, backend).backend is backend

    def test_the_setting_is_validated(self):
        from andromeda_cli import config as config_module

        with pytest.raises(config_module.ConfigError):
            config_module.validate("memory_backend", "postgres")


class TestDurability:
    def test_a_corrupt_json_store_reads_as_empty_and_stays_on_disk(self, tmp_path):
        store = MemoryStore(tmp_path / "memory", "json")
        store.store("something")
        store.file.write_text("{not json", encoding="utf-8")
        assert store.load() == []
        assert store.file.exists(), "a corrupt store must be recoverable by hand"

    def test_the_json_store_is_owner_only(self, tmp_path):
        import os
        import stat

        store = MemoryStore(tmp_path / "memory", "json")
        store.store("a private fact")
        assert stat.S_IMODE(os.stat(store.file).st_mode) == 0o600
