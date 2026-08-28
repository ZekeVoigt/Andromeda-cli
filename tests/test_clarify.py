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


class TestChoicesThatAreNotStrings:
    """A model passed an object where a choice string belonged and the prompt
    rendered its Python repr — `{'question': 'auth', 'choices': [...]}` — inside
    the box asking somebody to choose. Unreadable, and unanswerable."""

    @staticmethod
    def _asked(**kwargs):
        seen: list = []

        def asker(batch):
            seen.extend((q.text, q.choices) for q in batch)
            return ["x"] * len(batch)

        clarify.ask(asker, **kwargs)
        return seen

    def test_a_dict_never_reaches_the_screen_as_a_repr(self):
        seen = self._asked(question="Pick", choices=[{"unlabelled": 1}])
        assert all("{" not in choice for _text, choices in seen for choice in choices)

    def test_a_labelled_object_becomes_its_label(self):
        seen = self._asked(question="Pick", choices=[{"label": "staging"}, "prod"])
        assert seen[0][1] == ["staging", "prod"]

    def test_the_wrapped_batch_from_the_bug_report_is_unwrapped(self):
        """The prose question is at the top level and its options got wrapped
        in one object. Merged, not split — splitting asks the same thing twice."""
        seen = self._asked(
            question="How should the cloud job authenticate to Vercel's API?",
            choices=[
                {
                    "question": "auth",
                    "choices": ["Store a token", "Keep it local-only", "Something else"],
                }
            ],
        )
        assert len(seen) == 1
        text, choices = seen[0]
        assert text.startswith("How should the cloud job")
        assert choices == ["Store a token", "Keep it local-only", "Something else"]

    def test_plain_string_choices_are_untouched(self):
        assert self._asked(question="Pick", choices=["a", "b"])[0][1] == ["a", "b"]

    def test_numbers_survive_as_text(self):
        assert self._asked(question="How many?", choices=[1, 2])[0][1] == ["1", "2"]


class TestTextThatCannotMoveTheCursor:
    """The question and the choices are model output, and they end up on a tty.

    A `\r` inside a choice is not a character to the terminal, it is an
    instruction: the rest of that row is drawn from column zero, outside the
    box the prompt lives in and over whatever was already there. The row that
    moves is a row somebody is trying to pick.
    """

    def test_a_carriage_return_inside_a_choice_is_neutralised(self):
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""])[1],
            question="Which one?",
            choices=["webflow", "lin\rear"],
        )
        assert seen[0].choices == ["webflow", "lin ear"]

    def test_escape_sequences_are_dropped_rather_than_shown(self):
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""])[1],
            question="Pick \x1b[2Ja target",
            choices=["\x1b[31mprod\x1b[0m"],
        )
        assert seen[0].text == "Pick a target"
        assert seen[0].choices == ["prod"]

    def test_a_question_stays_one_line(self):
        """Every surface lays a question out as one line and wraps it itself."""
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""])[1],
            question="Which\n\ttarget?  staging\r\nor prod?",
        )
        assert seen[0].text == "Which target? staging or prod?"

    def test_a_labelled_choice_is_cleaned_too(self):
        seen = []
        clarify.ask(
            lambda q: (seen.extend(q), [""])[1],
            question="Which one?",
            choices=[{"label": "sta\tging"}],
        )
        assert seen[0].choices == ["sta ging"]
