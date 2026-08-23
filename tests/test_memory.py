from __future__ import annotations

import json
import os
import stat

import pytest

from andromeda_tools.memory import CONSOLIDATE_AT, MAX_STANDING, MemoryStore, score


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


class TestScoring:
    def test_full_coverage_scores_one(self):
        assert score("clickable links", "Zeke prefers clickable links in output") == 1.0

    def test_no_overlap_scores_zero(self):
        assert score("kubernetes", "Zeke prefers clickable links") == 0.0

    def test_a_long_memory_is_not_punished_for_detail(self):
        """Coverage of the query, not symmetric overlap."""
        short = score("links", "links")
        long = score("links", "links " + " ".join(f"word{i}" for i in range(50)))
        assert short == long == 1.0

    def test_stopwords_do_not_inflate_a_match(self):
        assert score("the and of", "the and of") == 0.0


class TestStore:
    def test_stores_and_recalls(self, store):
        store.store("Zeke prefers file paths as clickable links")
        result = store.search("clickable links")
        assert result.ok and "clickable links" in result.content

    def test_recall_below_the_threshold_returns_nothing(self, store):
        store.store("Zeke prefers clickable links")
        assert "Nothing remembered" in store.search("kubernetes").content

    def test_an_empty_memory_is_refused(self, store):
        assert store.store("   ").ok is False

    def test_an_unknown_scope_is_refused(self, store):
        assert store.store("x", scope="permanent").ok is False

    def test_restating_a_fact_consolidates_rather_than_duplicates(self, store):
        """The tool description promises this, so it is pinned."""
        store.store("Zeke prefers clickable file links")
        result = store.store("Zeke prefers clickable file links")

        assert result.metadata["superseded"] == 1
        assert len(store.load()) == 1

    def test_a_different_fact_does_not_consolidate(self, store):
        store.store("Zeke prefers clickable file links")
        store.store("Zeke runs the harness on macOS")
        assert len(store.load()) == 2

    def test_replaces_removes_what_it_names(self, store):
        store.store("Zeke lives in Phoenix")
        result = store.store("Zeke lives in Denver", replaces="lives in Phoenix")

        assert result.metadata["superseded"] == 1
        contents = [memory.content for memory in store.load()]
        assert contents == ["Zeke lives in Denver"]

    def test_consolidation_is_scoped(self, store):
        """A standing fact and an episode about it are different records."""
        store.store("Zeke prefers clickable links", scope="standing")
        store.store("Zeke prefers clickable links", scope="episode")
        assert len(store.load()) == 2

    def test_standing_is_capped_by_age(self, store):
        for index in range(MAX_STANDING + 5):
            store.store(f"standing fact number {index}", scope="standing")

        standing = store.standing()
        assert len(standing) == MAX_STANDING
        # The oldest went, not the newest.
        assert any("number 44" in memory.content for memory in standing)
        assert not any(memory.content.endswith("number 0") for memory in standing)

    def test_episodes_are_not_capped(self, store):
        for index in range(MAX_STANDING + 5):
            store.store(f"episode number {index}")
        assert len(store.load()) == MAX_STANDING + 5

    def test_standing_returns_only_standing(self, store):
        store.store("a standing thing", scope="standing")
        store.store("an episodic thing", scope="episode")
        assert [memory.content for memory in store.standing()] == ["a standing thing"]


class TestForget:
    def test_forgets_what_matches(self, store):
        store.store("Zeke lives in Phoenix")
        result = store.forget("lives in Phoenix")
        assert result.metadata["removed"] == 1
        assert store.load() == []

    def test_a_miss_removes_nothing(self, store):
        store.store("Zeke lives in Phoenix")
        assert "Nothing remembered matched" in store.forget("kubernetes").content
        assert len(store.load()) == 1

    def test_scope_limits_what_is_forgotten(self, store):
        store.store("shared subject matter here", scope="standing")
        store.store("shared subject matter here", scope="episode")

        store.forget("shared subject matter", scope="episode")
        remaining = store.load()
        assert len(remaining) == 1 and remaining[0].scope == "standing"

    def test_an_empty_query_is_refused(self, store):
        assert store.forget("  ").ok is False


class TestPersistence:
    def test_the_store_is_written_owner_only(self, store):
        store.store("a fact")
        mode = stat.S_IMODE(os.stat(store.file).st_mode)
        assert mode == 0o600, f"memory file is {oct(mode)}"

    def test_a_corrupt_store_reads_as_empty_and_is_left_on_disk(self, store):
        store.root.mkdir(parents=True, exist_ok=True)
        store.file.write_text("{not json", encoding="utf-8")

        assert store.load() == []
        assert store.file.exists(), "a corrupt store must be recoverable by hand"

    def test_records_round_trip(self, store):
        store.store("a fact", scope="standing", category="preference", tags=["x", "y"])
        reloaded = MemoryStore(store.root).load()
        assert reloaded[0].category == "preference"
        assert reloaded[0].tags == ["x", "y"]
        assert reloaded[0].scope == "standing"

    def test_a_record_without_content_is_dropped_on_read(self, store):
        store.root.mkdir(parents=True, exist_ok=True)
        store.file.write_text(json.dumps([{"id": "1", "content": ""}]), encoding="utf-8")
        assert store.load() == []
