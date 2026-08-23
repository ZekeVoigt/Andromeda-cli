"""Asking rather than guessing."""

from __future__ import annotations

from andromeda_tools import clarify


class TestRefusal:
    def test_with_nobody_to_ask_it_refuses(self):
        """A default here would be exactly the guess this tool replaces."""
        result = clarify.ask(None, question="Which target?")
        assert result.ok is False
        assert "nobody to ask" in result.content
        assert "State your assumption" in result.content

    def test_an_empty_question_is_refused(self):
        assert clarify.ask(lambda q: [""], question="  ").ok is False

    def test_an_empty_batch_is_refused(self):
        assert clarify.ask(lambda q: [], questions=[{"nope": 1}]).ok is False


class TestAsking:
    def test_a_single_question_round_trips(self):
        seen = []

        def asker(questions):
            seen.extend(questions)
            return ["staging"]

        result = clarify.ask(asker, question="Which target?", choices=["staging", "prod"])
        assert result.ok
        assert "staging" in result.content
        assert seen[0].choices == ["staging", "prod"]

    def test_choices_are_capped(self):
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""])[1],
            question="Pick",
            choices=["a", "b", "c", "d", "e", "f"],
        )
        assert len(seen[0].choices) == clarify.MAX_CHOICES

    def test_blank_choices_are_dropped(self):
        seen = []
        clarify.ask(lambda q: (seen.extend(q), [""])[1], question="Pick", choices=["a", "  ", "b"])
        assert seen[0].choices == ["a", "b"]

    def test_a_batch_asks_each_question(self):
        seen = []

        def asker(questions):
            seen.extend(questions)
            return ["one", "two"]

        result = clarify.ask(
            asker,
            questions=[
                {"id": "a", "question": "First?"},
                {"id": "b", "question": "Second?", "choices": ["x", "y"]},
            ],
        )
        assert [q.text for q in seen] == ["First?", "Second?"]
        assert "one" in result.content and "two" in result.content

    def test_a_batch_is_capped(self):
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""] * len(q))[1],
            questions=[{"question": f"Q{i}"} for i in range(10)],
        )
        assert len(seen) == clarify.MAX_QUESTIONS

    def test_an_unanswered_question_reads_as_no_answer(self):
        result = clarify.ask(lambda q: [""], question="Which?")
        assert "(no answer)" in result.content

    def test_a_dismissed_prompt_is_reported_not_raised(self):
        def asker(questions):
            raise KeyboardInterrupt

        result = clarify.ask(asker, question="Which?")
        assert result.ok is False and "dismissed" in result.content

    def test_the_answers_are_in_the_metadata(self):
        result = clarify.ask(lambda q: ["picked"], question="Which?")
        assert result.metadata["answers"] == ["picked"]


class TestSchema:
    def test_options_belong_in_choices_is_stated(self):
        """The rule models get wrong most often."""
        assert "never enumerate them inside the question" in clarify.DESCRIPTION

    def test_it_says_not_to_use_it_for_dangerous_commands(self):
        assert "approval gate already does that" in clarify.DESCRIPTION

    def test_the_schema_caps_choices_and_questions(self):
        properties = clarify.PARAMETERS["properties"]
        assert properties["choices"]["maxItems"] == clarify.MAX_CHOICES
        assert properties["questions"]["maxItems"] == clarify.MAX_QUESTIONS

    def test_nothing_is_required(self):
        """Either `question` or `questions` is valid, so neither can be required."""
        assert clarify.PARAMETERS["required"] == []
