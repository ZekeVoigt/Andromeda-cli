"""The command palette: one registry, and everything in it reachable."""

from __future__ import annotations

import pytest

from andromeda_cli import repl, vocabulary


def test_conversation_commands_come_first():
    """`/new` buried under an alphabetised merge with thirty verbs would be a
    worse list than the hardcoded one this replaces."""
    rows = vocabulary.commands()
    kinds = [row.kind for row in rows]
    assert kinds[0] == "conversation"
    assert kinds.index("verb") > kinds.count("conversation") - 1


def test_the_verbs_are_read_from_the_parser_not_a_list():
    """So a command added to the CLI is in the palette without anybody
    remembering to add it twice."""
    names = {row.name for row in vocabulary.commands() if row.kind == "verb"}
    assert "mcp" in names
    assert "cron" in names


def test_a_verb_row_advertises_its_subcommands():
    """"Connect and manage MCP servers" does not tell you `install` exists,
    and `install` is the word somebody is looking for."""
    row = next(r for r in vocabulary.commands() if r.name == "mcp")
    assert "install" in row.summary
    assert "catalog" in row.summary


def test_a_name_in_both_halves_is_listed_once():
    """`/skills` is a conversation command and a verb. The conversation one is
    what runs, so listing both would put an unreachable row in the palette."""
    rows = vocabulary.commands()
    assert len(rows) == len({row.name for row in rows})
    skills = next(row for row in rows if row.name == "skills")
    assert skills.kind == "conversation"
    assert not vocabulary.is_verb("skills")


def test_matching_puts_prefixes_above_mentions():
    rows = vocabulary.matching("/mc")
    assert rows[0].name == "mcp"


def test_matching_tolerates_the_slash_or_no_slash():
    assert vocabulary.matching("/new")[0].name == "new"
    assert vocabulary.matching("new")[0].name == "new"


def test_an_empty_prefix_is_the_whole_list():
    assert vocabulary.matching("/") == vocabulary.commands()


def test_nothing_matching_is_empty_rather_than_a_guess():
    assert vocabulary.matching("/zzzznotacommand") == []


def test_help_is_generated_so_it_cannot_be_out_of_date():
    text = vocabulary.help_text()
    for row in vocabulary.commands():
        assert f"/{row.name}" in text


def test_help_names_the_palette():
    assert "/" in vocabulary.help_text()
    assert "filter" in vocabulary.help_text()


# ---------------------------------------------------------------------------
# The completer
# ---------------------------------------------------------------------------


class _Document:
    def __init__(self, text: str) -> None:
        self.text_before_cursor = text


def _completions(text: str) -> list:
    return list(repl.SlashCompleter().get_completions(_Document(text), None))


def test_the_menu_opens_on_a_bare_slash():
    """The list *is* the feature — nobody presses Tab to find out whether
    there is something they have never heard of."""
    assert len(_completions("/")) == len(vocabulary.commands())


def test_typing_narrows_it():
    assert [c.display_text for c in _completions("/mc")] == ["/mcp"]


def test_each_row_carries_what_the_command_does():
    """A palette of twenty bare words is one you still have to go and look up."""
    first = _completions("/mcp")[0]
    assert first.display_meta_text
    assert "install" in first.display_meta_text


def test_a_message_that_merely_contains_a_slash_is_left_alone():
    """Popping a menu over somebody writing `and/or` makes the field feel
    broken."""
    assert _completions("what about and/or") == []
    assert _completions("/mcp install stripe") == []
    assert _completions("") == []


def test_the_completion_replaces_what_was_typed():
    """A `start_position` that is not the whole word appends rather than
    replaces, and you get `//mcp`."""
    for completion in _completions("/mc"):
        assert completion.start_position == -3


# ---------------------------------------------------------------------------
# Running a verb from inside a session
# ---------------------------------------------------------------------------


def test_a_verb_runs_in_process(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    assert vocabulary.run_verb("/mcp", "catalog stripe") == 0
    assert "stripe" in capsys.readouterr().out


def test_a_bad_argument_ends_the_command_not_the_session(tmp_path, monkeypatch):
    """argparse calls `sys.exit`, and that must not take the conversation
    somebody is in the middle of."""
    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    assert vocabulary.run_verb("/mcp", "--no-such-flag") != 0


def test_unbalanced_quotes_are_reported_rather_than_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    assert vocabulary.run_verb("/mcp", "catalog 'unclosed") == 1


def test_only_reachable_verbs_are_offered():
    """`backup` wants a shell and a path. Offering it here would be offering
    something that then asks you to go elsewhere anyway."""
    assert not vocabulary.is_verb("backup")
    assert not vocabulary.is_verb("restore")
    assert vocabulary.is_verb("mcp")


def test_a_conversation_command_is_never_treated_as_a_verb():
    """Or `/new` would start dispatching to the CLI instead of resetting."""
    for name in ("new", "exit", "help", "rewind"):
        assert not vocabulary.is_verb(name)
