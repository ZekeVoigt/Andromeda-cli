"""Compaction: making room without corrupting the transcript.

The invariant everything here protects: an assistant message carrying
`tool_calls` and the `tool` messages answering them are one unit. Split them
and the API rejects the whole request — every `tool_call_id` must have an
answer.
"""

from __future__ import annotations

import pytest

from andromeda_agent import compaction
from andromeda_agent.loop import Conversation
from andromeda_agent.approval import Policy
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider, call, turn_with

ALL = frozenset({"read_file", "list_dir", "search_files", "write_file", "patch", "terminal", "todo"})


def transcript(pairs: int, tool_chars: int = 5_000, text_chars: int = 0) -> list[dict]:
    """A system message then `pairs` complete call-and-answer units.

    `text_chars` inflates the *prose* rather than the tool output. That is the
    case pruning cannot help with, and therefore the only way to reach the
    summarisation stage.
    """
    messages: list[dict] = [{"role": "system", "content": "system"}]
    for index in range(pairs):
        messages.append({"role": "user", "content": f"ask {index} " + "w" * text_chars})
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "x" * tool_chars})
        messages.append(
            {"role": "assistant", "content": f"answer {index} " + "z" * text_chars}
        )
    return messages


def dangling_call_ids(messages: list[dict]) -> set[str]:
    """Tool call ids with no answering `tool` message after them."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    called = {
        c["id"]
        for m in messages
        for c in (m.get("tool_calls") or [])
    }
    return called - answered


class TestEstimation:
    def test_counts_content(self):
        assert compaction.estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100

    def test_counts_tool_call_arguments(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c", "function": {"name": "read_file", "arguments": "y" * 400}}
                ],
            }
        ]
        assert compaction.estimate_tokens(messages) > 100

    def test_a_null_content_does_not_crash(self):
        assert compaction.estimate_tokens([{"role": "assistant", "content": None}]) == 0

    def test_usage_fraction_handles_a_zero_window(self):
        assert compaction.usage_fraction([{"role": "user", "content": "x"}], 0) == 0.0


class TestMicroCompact:
    def test_old_tool_results_are_blanked(self):
        messages = transcript(pairs=5)
        out, pruned = compaction.micro_compact(messages)
        assert pruned == 3  # five, minus the two most recent
        assert compaction.estimate_tokens(out) < compaction.estimate_tokens(messages)

    def test_the_most_recent_results_survive(self):
        out, _ = compaction.micro_compact(transcript(pairs=5))
        tools = [m for m in out if m["role"] == "tool"]
        assert tools[-1]["content"] != compaction.PRUNED_PLACEHOLDER
        assert tools[-2]["content"] != compaction.PRUNED_PLACEHOLDER
        assert tools[-3]["content"] == compaction.PRUNED_PLACEHOLDER

    def test_the_messages_themselves_are_kept(self):
        """Removing them would strand the calls they answer."""
        messages = transcript(pairs=5)
        out, _ = compaction.micro_compact(messages)
        assert len(out) == len(messages)
        assert dangling_call_ids(out) == set()

    def test_short_results_are_left_alone(self):
        out, pruned = compaction.micro_compact(transcript(pairs=5, tool_chars=10))
        assert pruned == 0

    def test_pruning_twice_is_idempotent(self):
        once, first = compaction.micro_compact(transcript(pairs=5))
        twice, second = compaction.micro_compact(once)
        assert second == 0 and twice == once

    def test_nothing_to_prune_is_not_an_error(self):
        out, pruned = compaction.micro_compact([{"role": "user", "content": "hi"}])
        assert pruned == 0 and len(out) == 1


class TestSafeSplit:
    def test_it_never_starts_on_a_tool_message(self):
        body = transcript(pairs=6)[1:]
        for want in range(len(body)):
            index = compaction.safe_split(body, want)
            if index < len(body):
                assert body[index]["role"] != "tool", want

    def test_it_never_orphans_a_tool_call(self):
        body = transcript(pairs=6)[1:]
        for want in range(len(body)):
            kept = body[compaction.safe_split(body, want) :]
            assert dangling_call_ids(kept) == set(), want

    def test_it_moves_forward_not_backward(self):
        body = transcript(pairs=4)[1:]
        assert compaction.safe_split(body, 5) >= 5

    def test_a_split_past_the_end_keeps_nothing(self):
        body = transcript(pairs=2)[1:]
        assert compaction.safe_split(body, len(body) + 10) == len(body)


class TestPlan:
    def test_the_system_message_never_moves(self):
        system, older, recent = compaction.plan_summarisation(transcript(pairs=8), 10_000)
        assert len(system) == 1 and system[0]["role"] == "system"
        assert all(m["role"] != "system" for m in older + recent)

    def test_recent_turns_are_kept_verbatim(self):
        _, older, recent = compaction.plan_summarisation(transcript(pairs=8), 10_000)
        assert older and recent
        assert dangling_call_ids(recent) == set()

    def test_an_empty_body_plans_nothing(self):
        system, older, recent = compaction.plan_summarisation(
            [{"role": "system", "content": "s"}], 10_000
        )
        assert older == [] and recent == []


class TestBudget:
    def test_it_is_a_fifth_of_the_window(self):
        assert compaction.summary_budget(30_000) == 6_000

    def test_a_small_window_gets_the_floor(self):
        assert compaction.summary_budget(1_000) == compaction.MIN_SUMMARY_TOKENS

    def test_a_huge_window_gets_the_ceiling(self):
        assert compaction.summary_budget(1_000_000) == compaction.SUMMARY_TOKENS_CEILING


class TestInTheLoop:
    def build(self, tmp_path, script, window):
        workspace = Workspace(tmp_path)
        todos = TodoList()
        provider = ScriptedProvider(script=list(script))
        conversation = Conversation(
            provider=provider,
            policy=Policy(mode="auto", enabled=ALL),
            workspace=workspace,
            context_window=window,
            todos=todos,
            registry=build_registry(workspace, todos),
        )
        return conversation, provider

    def test_a_small_transcript_is_left_alone(self, tmp_path):
        conversation, _ = self.build(tmp_path, ["done"], window=128_000)
        seen = []
        from andromeda_agent import Callbacks

        conversation.send("hi", Callbacks(on_compaction=seen.append))
        assert seen == []

    def test_pruning_alone_is_preferred(self, tmp_path):
        from andromeda_agent import Callbacks

        conversation, provider = self.build(tmp_path, ["done"], window=8_000)
        conversation.messages = transcript(pairs=6)

        seen = []
        conversation.send("continue", Callbacks(on_compaction=seen.append))

        assert seen and seen[0].stage == "prune"
        assert seen[0].freed > 0
        # One model call: the turn itself. Pruning costs nothing.
        assert len(provider.seen) == 1

    def test_summarising_happens_when_pruning_is_not_enough(self, tmp_path):
        from andromeda_agent import Callbacks

        # The summary reply, then the turn's reply.
        conversation, provider = self.build(
            tmp_path, ["a summary of earlier work", "done"], window=2_000
        )
        # Prose-heavy: pruning tool results cannot get this under the line.
        conversation.messages = transcript(pairs=6, tool_chars=300, text_chars=2_000)

        seen = []
        conversation.send("continue", Callbacks(on_compaction=seen.append))

        assert seen and seen[-1].stage == "summarise"
        assert any(compaction.is_summary(m) for m in conversation.messages)
        assert conversation.messages[0]["role"] == "system"

    def test_the_compacted_transcript_is_still_well_formed(self, tmp_path):
        from andromeda_agent import Callbacks

        conversation, _ = self.build(tmp_path, ["summary", "done"], window=2_000)
        conversation.messages = transcript(pairs=6, tool_chars=300, text_chars=2_000)
        conversation.send("continue", Callbacks(on_compaction=lambda r: None))

        assert dangling_call_ids(conversation.messages) == set()

    def test_a_failed_summary_keeps_the_pruning_and_does_not_raise(self, tmp_path):
        from andromeda_agent import Callbacks
        from andromeda_agent.errors import AgentError

        conversation, provider = self.build(tmp_path, [], window=2_000)
        conversation.messages = transcript(pairs=6, tool_chars=3_000, text_chars=2_000)
        provider.raises = AgentError("summary failed")

        # The turn itself will also raise; what matters is that compaction did
        # not turn a recoverable state into a crash of its own.
        with pytest.raises(AgentError):
            conversation.send("continue", Callbacks())

        assert any(
            m.get("content") == compaction.PRUNED_PLACEHOLDER for m in conversation.messages
        )

    def test_context_used_reports_a_fraction(self, tmp_path):
        conversation, _ = self.build(tmp_path, ["done"], window=10_000)
        conversation.messages = transcript(pairs=2, tool_chars=4_000)
        assert 0 < conversation.context_used < 1


class TestRecallAfterCompaction:
    """Compaction is not deletion. Both stages leave the full text in the
    session index, and both say so — a model that believes the detail is gone
    either re-does the work or answers from a summary when it should look."""

    def test_the_prune_placeholder_points_at_the_search(self):
        pruned, count = compaction.micro_compact(transcript(pairs=4), keep_recent=1)
        assert count
        blanked = next(
            m for m in pruned if m.get("content") == compaction.PRUNED_PLACEHOLDER
        )
        assert "session_search" in blanked["content"]

    def test_a_summary_carries_the_recall_note(self):
        note = compaction.recall_note("abc123", 12)
        rendered = compaction.render_summary("the summary", note)["content"]
        assert "abc123" in rendered and "session_search" in rendered
        assert rendered.startswith(compaction.SUMMARY_PREFIX)

    def test_no_session_means_no_promise(self):
        """Promising a lookup that will fail is worse than not offering one."""
        assert compaction.recall_note("", 12) == ""
        assert compaction.recall_note("abc123", 0) == ""
        assert compaction.render_summary("just the summary")["content"].endswith(
            "just the summary"
        )

    def test_the_instruction_itself_stays_strict(self):
        """The note is what a later turn reads. Telling the model there is a
        safety net while it is *writing* produces a lazier summary."""
        assert "session_search" not in compaction.SUMMARY_INSTRUCTION
        assert "anything you leave out is gone" in compaction.SUMMARY_INSTRUCTION


class TestTheArchiveHook:
    def build(self, tmp_path, script, window, on_archive=None):
        workspace = Workspace(tmp_path)
        todos = TodoList()
        provider = ScriptedProvider(script=list(script))
        conversation = Conversation(
            provider=provider,
            policy=Policy(mode="auto", enabled=ALL),
            workspace=workspace,
            context_window=window,
            todos=todos,
            registry=build_registry(workspace, todos),
            on_archive=on_archive,
        )
        return conversation, provider

    def _compact(self, tmp_path, on_archive):
        from andromeda_agent import Callbacks

        conversation, _ = self.build(
            tmp_path, ["a summary of earlier work", "done"], 2_000, on_archive
        )
        conversation.messages = transcript(pairs=6, tool_chars=300, text_chars=2_000)
        conversation.send("continue", Callbacks())
        return conversation

    def test_it_is_called_with_the_range_about_to_be_folded_away(self, tmp_path):
        calls = []

        def on_archive(messages, first, last):
            calls.append((len(messages), first, last))
            return "kept"

        self._compact(tmp_path, on_archive)
        assert calls
        seen, first, last = calls[0]
        # The system message never moves, so the range starts after it.
        assert first == 1 and last < seen

    def test_it_sees_the_transcript_before_it_is_replaced(self, tmp_path):
        """Otherwise there would be nothing left to archive."""
        snapshots = []

        def on_archive(messages, _first, _last):
            snapshots.append(list(messages))
            return ""

        conversation = self._compact(tmp_path, on_archive)
        assert len(snapshots[0]) > len(conversation.messages)

    def test_what_it_returns_lands_in_the_summary(self, tmp_path):
        conversation = self._compact(
            tmp_path, lambda _m, _f, _l: "SEARCHABLE-MARKER"
        )
        summary = next(m for m in conversation.messages if compaction.is_summary(m))
        assert "SEARCHABLE-MARKER" in summary["content"]

    def test_a_failing_archive_never_breaks_the_compaction(self, tmp_path):
        """Nor does it leave the summary claiming something it cannot back."""

        def explode(_messages, _first, _last):
            raise RuntimeError("the index is unwritable")

        conversation = self._compact(tmp_path, explode)
        summary = next(m for m in conversation.messages if compaction.is_summary(m))
        assert "session_search" not in summary["content"]

    def test_no_hook_means_no_note(self, tmp_path):
        conversation = self._compact(tmp_path, None)
        summary = next(m for m in conversation.messages if compaction.is_summary(m))
        assert "session_search" not in summary["content"]
