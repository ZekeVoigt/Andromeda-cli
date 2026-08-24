"""The wiring between a conversation, its transcript, skills and memory."""

from __future__ import annotations

from andromeda_cli import config as config_module
from andromeda_cli import sessions as store
from andromeda_cli.session import build_conversation
from andromeda_tools import MemoryStore
from andromeda_tools import skills as skills_module
from support import ScriptedProvider


def build(tmp_path, script, **overrides):
    config = config_module.load()
    config.update({"approval_mode": "auto", **overrides})
    provider = ScriptedProvider(script=list(script))
    return build_conversation(
        config, provider, interactive=True, workspace_root=str(tmp_path)
    )


def test_a_finished_exchange_is_saved(tmp_path):
    conversation, record = build(tmp_path, ["hello"])
    conversation.send("hi")

    assert record.path.exists()
    reloaded = store.load(record.id)
    assert reloaded is not None
    assert reloaded.messages[-1]["content"] == "hello"


def test_the_saved_session_records_the_model_and_workspace(tmp_path):
    conversation, record = build(tmp_path, ["hello"])
    conversation.send("hi")

    reloaded = store.load(record.id)
    assert reloaded.model == "test/model"
    assert reloaded.workspace == str(tmp_path.resolve())


def test_each_exchange_overwrites_rather_than_appends(tmp_path):
    conversation, record = build(tmp_path, ["one", "two"])
    conversation.send("first")
    conversation.send("second")

    reloaded = store.load(record.id)
    assert reloaded.turns == 2
    assert len(store.recent()) == 1


def test_resuming_replays_the_transcript_verbatim(tmp_path):
    conversation, record = build(tmp_path, ["hello"])
    conversation.send("hi")
    original = list(conversation.messages)

    config = config_module.load()
    config["approval_mode"] = "auto"
    resumed, same_record = build_conversation(
        config,
        ScriptedProvider(script=["again"]),
        interactive=True,
        workspace_root=str(tmp_path),
        session=store.load(record.id),
    )

    # Including the original system message: rewriting it would change the
    # rules the earlier turns were produced under.
    assert resumed.messages == original
    assert same_record.id == record.id


def test_resuming_continues_the_same_file(tmp_path):
    conversation, record = build(tmp_path, ["hello"])
    conversation.send("hi")

    config = config_module.load()
    config["approval_mode"] = "auto"
    resumed, _ = build_conversation(
        config,
        ScriptedProvider(script=["second answer"]),
        interactive=True,
        workspace_root=str(tmp_path),
        session=store.load(record.id),
    )
    resumed.send("more")

    assert len(store.recent()) == 1
    assert store.load(record.id).turns == 2


def test_the_skills_manifest_reaches_the_system_prompt(tmp_path, monkeypatch):
    root = tmp_path / "skills" / "weather"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        '---\nname: weather\ndescription: "Forecasts."\n---\n\nBODY TEXT\n', encoding="utf-8"
    )
    monkeypatch.setenv(skills_module.ENV_SKILLS_DIR, str(tmp_path / "skills"))

    conversation, _ = build(tmp_path, ["ok"])
    system = conversation.messages[0]["content"]

    assert "weather — Forecasts." in system
    # The body stays out until skill_load asks for it.
    assert "BODY TEXT" not in system


def test_standing_memories_reach_the_system_prompt_and_episodes_do_not(tmp_path):
    memory = MemoryStore(config_module.home() / "memory")
    memory.store("Zeke prefers clickable links", scope="standing")
    memory.store("The build failed on Tuesday", scope="episode")

    conversation, _ = build(tmp_path, ["ok"])
    system = conversation.messages[0]["content"]

    assert "clickable links" in system
    assert "Tuesday" not in system


def test_skill_load_and_memory_tools_are_offered(tmp_path):
    conversation, _ = build(tmp_path, ["ok"])
    names = {spec.name for spec in conversation.available}
    assert {"skill_load", "memory_search", "memory_store", "memory_forget"} <= names


def test_a_new_conversation_keeps_skills_and_memory_bound(tmp_path):
    """`reset` rebuilt the registry; without the hook it dropped these."""
    conversation, _ = build(tmp_path, ["ok"])
    conversation.reset()

    names = set(conversation.registry)
    assert {"skill_load", "memory_search", "memory_store"} <= names


def test_a_failing_save_does_not_lose_the_turn(tmp_path, monkeypatch):
    conversation, record = build(tmp_path, ["hello"])

    def explode(_messages):
        raise OSError("disk full")

    conversation.on_persist = explode
    assert conversation.send("hi") == "hello"


def test_the_agent_knows_where_its_own_state_lives(tmp_path):
    """Asked often enough that a guess is worse than a fact."""
    conversation, _ = build(tmp_path, ["ok"])
    system = conversation.messages[0]["content"]

    assert str(config_module.config_path()) in system
    assert str(config_module.home() / "sessions") in system


def test_checkpoints_survive_a_saved_session(tmp_path):
    """The run you most want to undo is often the one you came back to."""
    from andromeda_cli import sessions as store
    from andromeda_cli.checkpoints import CheckpointStack

    stack = CheckpointStack()
    stack.take([{"role": "user", "content": "first question"}], "first question")

    session = store.Session()
    session.messages = [{"role": "user", "content": "first question"}]
    session.checkpoints = stack.to_json()
    session.save()

    reloaded = store.load(session.id)
    restored = CheckpointStack.from_json(reloaded.checkpoints)
    assert [c.label for c in restored.all()] == ["first question"]


class TestTheIndexKeepsUpWithTheConversation:
    """Indexed on the same schedule the transcript is written, so a session is
    searchable the moment it exists rather than after some later sweep."""

    def test_a_finished_exchange_is_searchable_immediately(self, tmp_path):
        from andromeda_cli import state

        conversation, record = build(tmp_path, ["the retry budget is three"])
        conversation.send("what about the retry budget")

        assert [hit.session_id for hit in state.search("retry budget")] == [
            record.id,
            record.id,
        ]

    def test_a_second_turn_appends_rather_than_rebuilding(self, tmp_path):
        from andromeda_cli.state import db as db_module
        from andromeda_cli.state import index as index_module

        conversation, record = build(tmp_path, ["one", "two"])
        conversation.send("first")
        conversation.send("second")

        with db_module.connect() as conn:
            assert index_module.write_session(conn, record) == "unchanged"

    def test_a_failing_index_never_fails_the_turn(self, tmp_path, monkeypatch):
        """Failing to index is a failure to search later. It must never
        become a failure to answer now."""
        import sqlite3

        from andromeda_cli.state import db as db_module

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db_module, "connect", explode)
        conversation, record = build(tmp_path, ["hello"])
        assert conversation.send("hi") == "hello"
        assert store.load(record.id) is not None

    def test_the_agent_is_told_where_its_own_state_lives(self, tmp_path):
        """Asked often enough — "where are my sessions", "what have you
        remembered" — that a guess is worse than a fact."""
        conversation, _record = build(tmp_path, ["hello"])
        blocks = "\n".join(conversation.context_blocks)
        assert "sessions" in blocks and "index" in blocks

    def test_the_configured_memory_backend_is_the_one_that_gets_used(self, tmp_path):
        conversation, _record = build(tmp_path, ["hello"], memory_backend="sqlite")
        blocks = "\n".join(conversation.context_blocks)
        assert "state.db" in blocks


class TestCompactionKeepsTheDiscardedTurns:
    """The end-to-end version of the archive hook: a real session, a real
    compaction, and the folded-away turns still findable afterwards."""

    def compact(self, tmp_path):
        conversation, record = build(
            tmp_path,
            ["a summary of earlier work", "done"],
            context_window=2_000,
        )
        # Prose-heavy, so pruning tool results cannot get under the line.
        conversation.messages = [
            {"role": "system", "content": "the system message"},
            *[
                {
                    "role": "user" if n % 2 == 0 else "assistant",
                    "content": f"turn {n} about the retry budget " + "x" * 1_500,
                }
                for n in range(6)
            ],
        ]
        conversation.send("carry on")
        return conversation, record

    def test_the_folded_away_turns_are_still_searchable(self, tmp_path):
        from andromeda_cli import state

        _conversation, record = self.compact(tmp_path)

        hits = state.search("retry budget", limit=20)
        assert hits, "compaction discarded the turns from the index as well"
        assert any(hit.archived for hit in hits)
        assert all(hit.session_id == record.id for hit in hits)

    def test_the_summary_names_the_session_they_are_in(self, tmp_path):
        from andromeda_agent import compaction as compaction_module

        conversation, record = self.compact(tmp_path)
        summary = next(
            m for m in conversation.messages if compaction_module.is_summary(m)
        )
        assert record.id in summary["content"]

    def test_the_anchor_in_that_summary_actually_resolves(self, tmp_path):
        """The promise is only worth making if the lookup works."""
        from andromeda_cli import state

        _conversation, record = self.compact(tmp_path)
        archived = [hit for hit in state.search("retry budget") if hit.archived]
        assert archived
        view = state.anchored(record.id, archived[0].anchor)
        assert view["window"]

    def test_the_live_transcript_is_shorter_than_what_is_indexed(self, tmp_path):
        from andromeda_cli import state

        conversation, record = self.compact(tmp_path)
        indexed = state.transcript(record.id)
        assert len(indexed) > len(conversation.messages)
        assert state.archived_count(record.id) > 0
