"""Rewinding."""

from __future__ import annotations

from andromeda_cli.checkpoints import MAX_CHECKPOINTS, CheckpointStack


def transcript(*turns: str) -> list[dict]:
    messages = [{"role": "system", "content": "s"}]
    for turn in turns:
        messages.append({"role": "user", "content": turn})
        messages.append({"role": "assistant", "content": f"re: {turn}"})
    return messages


class TestTaking:
    def test_a_checkpoint_copies_the_transcript(self):
        stack = CheckpointStack()
        messages = transcript("one")
        checkpoint = stack.take(messages, "one")

        messages.append({"role": "user", "content": "two"})
        assert len(checkpoint.messages) == 3

    def test_messages_are_copied_individually(self):
        """A shallow list copy still shares the dicts the transcript mutates."""
        stack = CheckpointStack()
        messages = transcript("one")
        checkpoint = stack.take(messages, "one")

        messages[1]["content"] = "changed"
        assert checkpoint.messages[1]["content"] == "one"

    def test_the_label_is_the_prompt_trimmed(self):
        stack = CheckpointStack()
        checkpoint = stack.take([], "a  really\n  long   prompt " + "x" * 200)
        assert "\n" not in checkpoint.label
        assert len(checkpoint.label) <= 60

    def test_indexes_increase(self):
        stack = CheckpointStack()
        assert [stack.take([], str(i)).index for i in range(3)] == [1, 2, 3]

    def test_the_stack_is_bounded(self):
        stack = CheckpointStack()
        for index in range(MAX_CHECKPOINTS + 10):
            stack.take(transcript(str(index)), str(index))
        assert len(stack) == MAX_CHECKPOINTS

    def test_the_oldest_are_dropped_not_the_newest(self):
        stack = CheckpointStack(limit=3)
        for index in range(5):
            stack.take([], f"turn {index}")
        assert [c.label for c in stack.all()] == ["turn 2", "turn 3", "turn 4"]


class TestResolving:
    def test_none_means_the_most_recent(self):
        stack = CheckpointStack()
        stack.take([], "first")
        latest = stack.take([], "second")
        assert stack.resolve() is latest

    def test_an_index_finds_that_checkpoint(self):
        stack = CheckpointStack()
        first = stack.take([], "first")
        stack.take([], "second")
        assert stack.resolve(first.index) is first

    def test_an_unknown_index_resolves_to_nothing(self):
        stack = CheckpointStack()
        stack.take([], "first")
        assert stack.resolve(99) is None

    def test_an_empty_stack_resolves_to_nothing(self):
        assert CheckpointStack().resolve() is None


class TestRewinding:
    def test_it_restores_the_transcript(self):
        stack = CheckpointStack()
        first = stack.take(transcript("one"), "one")
        stack.take(transcript("one", "two"), "two")

        restored = stack.rewind_to(first)
        assert [m.get("content") for m in restored if m["role"] == "user"] == ["one"]

    def test_later_checkpoints_are_discarded(self):
        """Keeping them would let a second rewind jump forward into a
        transcript that no longer describes what happened."""
        stack = CheckpointStack()
        first = stack.take(transcript("one"), "one")
        stack.take(transcript("one", "two"), "two")
        stack.take(transcript("one", "two", "three"), "three")

        stack.rewind_to(first)
        assert [c.label for c in stack.all()] == ["one"]

    def test_the_restored_transcript_is_detached(self):
        stack = CheckpointStack()
        checkpoint = stack.take(transcript("one"), "one")
        restored = stack.rewind_to(checkpoint)
        restored[1]["content"] = "mutated"
        assert checkpoint.messages[1]["content"] == "one"

    def test_turns_counts_user_messages(self):
        stack = CheckpointStack()
        assert stack.take(transcript("one", "two"), "x").turns == 2


class TestPersistence:
    def test_a_stack_round_trips(self):
        stack = CheckpointStack()
        stack.take(transcript("one"), "one")
        stack.take(transcript("one", "two"), "two")

        restored = CheckpointStack.from_json(stack.to_json())
        assert [c.label for c in restored.all()] == ["one", "two"]
        assert restored.resolve().turns == 2

    def test_numbering_continues_above_what_was_restored(self):
        """Or a resumed session's indexes collide with the ones on screen."""
        stack = CheckpointStack()
        stack.take([], "one")
        stack.take([], "two")

        restored = CheckpointStack.from_json(stack.to_json())
        assert restored.take([], "three").index == 3

    def test_rewinding_works_after_a_restore(self):
        stack = CheckpointStack()
        first = stack.take(transcript("one"), "one")
        stack.take(transcript("one", "two"), "two")

        restored = CheckpointStack.from_json(stack.to_json())
        target = restored.resolve(first.index)
        messages = restored.rewind_to(target)
        assert [m["content"] for m in messages if m["role"] == "user"] == ["one"]

    def test_nonsense_restores_as_an_empty_stack(self):
        assert len(CheckpointStack.from_json("not a list")) == 0
        assert len(CheckpointStack.from_json(None)) == 0
        assert len(CheckpointStack.from_json([1, "two", {}])) == 0

    def test_a_restored_stack_is_still_bounded(self):
        stack = CheckpointStack()
        for index in range(30):
            stack.take([], str(index))
        restored = CheckpointStack.from_json(stack.to_json(), limit=3)
        assert len(restored) == 3
