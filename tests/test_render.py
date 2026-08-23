"""The terminal surface.

Two properties matter: structure survives into a terminal, and nothing but
plain text survives into a pipe.
"""

from __future__ import annotations

import re

from rich.console import Console

from andromeda_cli import render

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def draw(renderable, width: int = 70) -> str:
    """Render to a string, with escape codes stripped.

    Stripped because these tests are about content and structure. Syntax
    highlighting interleaves an escape sequence between characters, so
    `"x = 1" in out` is false against the raw bytes even though the terminal
    shows exactly that.
    """
    console = Console(theme=render.THEME, width=width, force_terminal=True)
    with console.capture() as captured:
        console.print(renderable)
    return ANSI.sub("", captured.get())


class TestCharts:
    def test_label_value_lines_become_bars(self):
        bars = render.parse_chart("a: 10\nb: 5\n")
        assert [(bar.label, bar.value) for bar in bars] == [("a", 10.0), ("b", 5.0)]

    def test_formatting_is_stripped_from_the_number_but_kept_for_display(self):
        bars = render.parse_chart("revenue: $1,200\nshare: 45%")
        assert bars[0].value == 1200.0 and bars[0].formatted == "$1,200"
        assert bars[1].value == 45.0 and bars[1].formatted == "45%"

    def test_a_label_containing_a_colon_still_parses(self):
        bars = render.parse_chart("lib/agent-runtime: 42")
        assert bars[0].label == "lib/agent-runtime" and bars[0].value == 42.0

    def test_unparseable_lines_are_skipped_not_fatal(self):
        """Four of five bars beats an error."""
        bars = render.parse_chart("a: 10\nnonsense\nb: not-a-number\nc: 3")
        assert [bar.label for bar in bars] == ["a", "c"]

    def test_the_largest_bar_fills_the_width(self):
        out = draw(render.render_chart(render.parse_chart("a: 100\nb: 1"), width=20))
        assert "█" * 20 in out

    def test_close_values_are_visibly_different(self):
        """Whole blocks alone make every value in the same 1/32 look identical."""
        lines = draw(render.render_chart(render.parse_chart("a: 100\nb: 98"), width=32))
        first, second = [line for line in lines.splitlines() if line.strip()][:2]
        assert first != second

    def test_a_zero_value_still_draws_something(self):
        assert "▏" in draw(render.render_chart(render.parse_chart("a: 0\nb: 10")))

    def test_an_empty_chart_renders_nothing(self):
        assert str(render.render_chart([])) == ""


class TestExpansion:
    def test_a_chart_fence_becomes_a_chart(self):
        out = draw(render.expand_charts("Before\n\n```chart\na: 10\nb: 5\n```\n\nAfter"))
        assert "█" in out
        assert "```" not in out
        assert "Before" in out and "After" in out

    def test_an_unparseable_fence_falls_back_to_the_code_block(self):
        out = draw(render.expand_charts("```chart\nnothing useful\n```"))
        assert "nothing useful" in out

    def test_plain_markdown_passes_through(self):
        assert "Heading" in draw(render.expand_charts("# Heading\n\nBody"))

    def test_multiple_charts_in_one_answer(self):
        out = draw(
            render.expand_charts("```chart\na: 1\n```\ntext\n```chart\nb: 2\n```")
        )
        assert out.count("█") >= 2


class TestMarkdown:
    def test_bold_is_rendered_not_printed(self):
        out = draw(render.expand_charts("This is **important** text"))
        assert "**" not in out
        assert "important" in out

    def test_a_table_becomes_a_table(self):
        out = draw(render.expand_charts("| a | b |\n|---|---|\n| 1 | 2 |"))
        assert "|---|" not in out
        assert "a" in out and "1" in out

    def test_a_code_fence_is_rendered(self):
        out = draw(render.expand_charts("```python\nx = 1\n```"))
        assert "```" not in out and "x = 1" in out


class TestStreaming:
    def test_a_pipe_gets_plain_text(self, capsys):
        with render.AnswerStream(live=False) as stream:
            stream.feed("**bold** and `code`")
        out = capsys.readouterr().out
        # Exactly what the model said, byte for byte.
        assert out == "**bold** and `code`"

    def test_the_buffer_accumulates(self):
        with render.AnswerStream(live=False) as stream:
            stream.feed("one ")
            stream.feed("two")
            assert stream.text == "one two"


class TestMeter:
    def test_it_fills_with_use(self):
        assert render.context_meter(0.0, width=8) == "░" * 8
        assert render.context_meter(1.0, width=8) == "█" * 8

    def test_it_clamps(self):
        assert render.context_meter(-1, width=4) == "░" * 4
        assert render.context_meter(9, width=4) == "█" * 4


class TestStreamingCharts:
    """A chart fence should not appear as a code block on its way to being a chart."""

    def test_an_unclosed_fence_is_held_back_while_streaming(self):
        partial = "Here are the numbers:\n\n```chart\nread_file: 12\n"
        out = draw(render.expand_charts(partial, streaming=True))
        assert "Here are the numbers" in out
        assert "read_file: 12" not in out

    def test_a_closed_fence_renders_as_a_chart_while_streaming(self):
        text = "```chart\na: 10\nb: 5\n```\n"
        assert "█" in draw(render.expand_charts(text, streaming=True))

    def test_an_earlier_chart_survives_a_later_open_fence(self):
        text = "```chart\na: 10\n```\nmore\n\n```chart\nb: 5\n"
        out = draw(render.expand_charts(text, streaming=True))
        assert "█" in out and "more" in out
        assert "b: 5" not in out

    def test_the_final_pass_keeps_a_genuinely_unclosed_fence(self):
        """At the end it is not a partial chart, it is content."""
        out = draw(render.expand_charts("```chart\na: 10\n"))
        assert "a: 10" in out
