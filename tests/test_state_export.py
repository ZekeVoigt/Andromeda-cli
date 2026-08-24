"""Exporting a session, and recapping one.

The escaping tests are the load-bearing ones. A transcript contains whatever
anybody pasted into it, and one of the things people paste is HTML — an export
opened in a browser is a local file with the privileges of any other.
"""

from __future__ import annotations

import json

import pytest

from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.state import export as export_module


def session_with(messages):
    session = store.Session()
    session.messages = list(messages)
    session.model = "test/model"
    session.workspace = "/tmp/w"
    return session


EXCHANGE = [
    {"role": "system", "content": "the skills manifest"},
    {"role": "user", "content": "read the config"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
        ],
    },
    {"role": "tool", "name": "read_file", "content": "line one\nline two"},
    {"role": "assistant", "content": "It sets the retry budget to three."},
]


class TestMarkdown:
    def test_it_carries_the_conversation_and_the_metadata(self):
        rendered = export_module.render(session_with(EXCHANGE), "markdown")
        assert "read the config" in rendered
        assert "retry budget to three" in rendered
        assert "test/model" in rendered

    def test_the_system_message_is_left_out(self):
        """It is the skills manifest and every standing memory — neither what
        happened nor something to hand to anyone else."""
        assert "skills manifest" not in export_module.render(
            session_with(EXCHANGE), "markdown"
        )

    def test_tool_calls_and_results_are_both_shown(self):
        rendered = export_module.render(session_with(EXCHANGE), "markdown")
        assert "→ read_file" in rendered and "← read_file" in rendered

    def test_a_huge_tool_result_is_clipped(self):
        session = session_with(
            [{"role": "tool", "name": "terminal", "content": "\n".join("x" * 500)}]
        )
        assert "more lines" in export_module.render(session, "markdown")

    def test_md_is_accepted_as_an_alias(self):
        assert export_module.normalize("md") == "markdown"

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError):
            export_module.render(session_with(EXCHANGE), "pdf")


class TestHtml:
    def test_pasted_markup_is_escaped(self):
        session = session_with(
            [{"role": "user", "content": "<script>alert(1)</script>"}]
        )
        rendered = export_module.render(session, "html")
        assert "<script>alert(1)</script>" not in rendered
        assert "&lt;script&gt;" in rendered

    def test_a_tool_result_is_escaped_too(self):
        session = session_with(
            [{"role": "tool", "name": "terminal", "content": "<img onerror=x>"}]
        )
        assert "<img onerror=x>" not in export_module.render(session, "html")

    def test_the_title_is_escaped(self):
        session = session_with([{"role": "user", "content": "</title><script>x"}])
        rendered = export_module.render(session, "html")
        assert "</title><script>" not in rendered

    def test_it_is_self_contained(self):
        rendered = export_module.render(session_with(EXCHANGE), "html")
        assert "<style>" in rendered
        assert "http://" not in rendered and "https://" not in rendered

    def test_it_paints_both_themes(self):
        rendered = export_module.render(session_with(EXCHANGE), "html")
        assert "prefers-color-scheme: dark" in rendered


class TestJsonl:
    def test_one_session_per_line(self):
        rendered = export_module.render_jsonl(
            [session_with(EXCHANGE), session_with(EXCHANGE)]
        )
        assert len(rendered.strip().splitlines()) == 2
        assert json.loads(rendered.splitlines()[0])["model"] == "test/model"

    def test_prompts_only_changes_the_unit_to_a_prompt(self):
        """Which is what makes it useful to pipe into review tooling — the
        shape somebody wants is rarely "the whole session, again"."""
        session = session_with(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "…"},
                {"role": "user", "content": "second"},
            ]
        )
        rendered = export_module.render_jsonl([session], prompts_only=True)
        rows = [json.loads(line) for line in rendered.strip().splitlines()]
        assert [row["prompt"] for row in rows] == ["first", "second"]

    def test_an_empty_export_is_empty_not_a_blank_line(self):
        assert export_module.render_jsonl([]) == ""


class TestRecap:
    def test_it_counts_turns_and_tool_calls(self):
        recap = state.build_recap(EXCHANGE)
        assert recap.turns == 1 and recap.tool_calls == 1

    def test_it_names_the_files_that_were_touched(self):
        assert state.build_recap(EXCHANGE).files == ["a.py"]

    def test_it_quotes_the_last_exchange(self):
        recap = state.build_recap(EXCHANGE)
        assert recap.last_prompt == "read the config"
        assert "retry budget to three" in recap.last_answer

    def test_it_counts_failed_tool_calls_separately(self):
        """The difference between "it did twelve things" and "it tried
        twelve things"."""
        recap = state.build_recap(
            [{"role": "tool", "name": "terminal", "content": "Error: no such file"}]
        )
        assert recap.errors == 1

    def test_a_result_that_merely_mentions_an_error_is_not_a_failure(self):
        recap = state.build_recap(
            [{"role": "tool", "name": "read_file", "content": "def handle_error():"}]
        )
        assert recap.errors == 0

    def test_an_empty_session_says_so(self):
        recap = state.build_recap([])
        assert recap.empty
        assert "Nothing has happened" in recap.lines()[0]

    def test_open_todos_are_carried_when_there_are_any(self):
        from andromeda_tools.todo import TodoList

        todos = TodoList()
        todos.replace(
            [
                {"task": "wire the index", "status": "in_progress"},
                {"task": "write the tests", "status": "done"},
            ]
        )
        recap = state.build_recap(EXCHANGE, todos)
        assert recap.open_todos == ["wire the index"]

    def test_a_broken_todo_list_never_breaks_the_recap(self):
        class Exploding:
            @property
            def items(self):
                raise RuntimeError("boom")

        assert state.build_recap(EXCHANGE, Exploding()).open_todos == []
