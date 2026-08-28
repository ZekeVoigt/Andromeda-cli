"""Switching which transcript a running terminal writes to.

The rule this pins: only the transcript moves. The registry, the policy, the
provider and the workspace belong to the terminal, and a switch that rebuilt
them would be a second, subtly different way of starting a session.
"""

from __future__ import annotations

import pytest

from andromeda_agent import Policy
from andromeda_agent.loop import Conversation
from andromeda_cli import repl
from andromeda_cli import sessions as store
from andromeda_cli import state
from andromeda_cli.checkpoints import CheckpointStack
from andromeda_tools import Workspace, build_registry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider


def saved(text, checkpoints=None):
    session = store.Session()
    session.messages = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": f"about {text}"},
    ]
    session.checkpoints = checkpoints or []
    session.save()
    state.index_session(session)
    return session


@pytest.fixture
def conversation(tmp_path):
    workspace = Workspace(tmp_path)
    todos = TodoList()
    live = Conversation(
        provider=ScriptedProvider(script=["ok"]),  # type: ignore[arg-type]
        policy=Policy(mode="ask", enabled=frozenset({"read_file"})),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
    )
    record = store.Session()
    record.messages = [{"role": "user", "content": "the current session"}]
    record.save()
    live.messages = list(record.messages)
    live.binding = store.Binding(record)
    return live


class TestTheBinding:
    def test_switching_saves_the_session_being_left(self, conversation):
        """A session left half-written because somebody moved to another one
        is the transcript most likely to be the one they come back for."""
        leaving = conversation.binding.record
        target = saved("another topic")
        conversation.messages.append({"role": "assistant", "content": "unsaved reply"})

        conversation.binding.switch(target, conversation.messages)

        reloaded = store.load(leaving.id)
        assert reloaded.messages[-1]["content"] == "unsaved reply"

    def test_switching_to_the_same_session_is_a_no_op(self, conversation):
        current = conversation.binding.record
        assert conversation.binding.switch(current, []) is current
        assert current.messages

    def test_later_writes_land_in_the_new_transcript(self, conversation):
        target = saved("another topic")
        conversation.binding.switch(target, conversation.messages)
        conversation.binding.record.messages = [
            {"role": "user", "content": "written after the switch"}
        ]
        conversation.binding.record.save()
        assert "written after the switch" in store.load(target.id).messages[0]["content"]


class TestTheSlashCommand:
    def test_bare_resume_lists_the_other_sessions(self, conversation, capsys):
        other = saved("another topic")
        assert repl._slash("/resume", conversation, None) == "continue"
        assert other.id in capsys.readouterr().out

    def test_it_never_lists_the_session_you_are_in(self, conversation, capsys):
        saved("another topic")
        repl._slash("/resume", conversation, None)
        assert conversation.binding.record.id not in capsys.readouterr().out

    def test_a_number_picks_from_the_list(self, conversation):
        """A twelve-hex-digit id is not something anyone retypes correctly
        the first time."""
        target = saved("another topic")
        assert repl._slash("/resume 1", conversation, None) == "switched"
        assert conversation.binding.record.id == target.id

    def test_an_id_prefix_works_too(self, conversation):
        target = saved("another topic")
        assert repl._slash(f"/resume {target.id[:6]}", conversation, None) == "switched"
        assert conversation.binding.record.id == target.id

    def test_the_transcript_is_replaced_wholesale(self, conversation):
        """Including its original system message — rewriting that would
        silently change the rules the earlier turns were produced under."""
        target = saved("another topic")
        repl._slash("/resume 1", conversation, None)
        assert conversation.messages == target.messages

    def test_the_terminal_keeps_its_registry_and_policy(self, conversation):
        registry_before = conversation.registry
        policy_before = conversation.policy
        workspace_before = conversation.workspace
        saved("another topic")
        repl._slash("/resume 1", conversation, None)
        assert conversation.registry is registry_before
        assert conversation.policy is policy_before
        assert conversation.workspace is workspace_before

    def test_an_unknown_id_is_refused(self, conversation, capsys):
        saved("another topic")
        assert repl._slash("/resume deadbeef99", conversation, None) == "continue"
        assert "No session matching" in capsys.readouterr().err

    def test_resuming_the_current_session_says_so(self, conversation, capsys):
        current = conversation.binding.record
        saved("another topic")
        assert repl._slash(f"/resume {current.id}", conversation, None) == "continue"
        assert "Already in that session" in capsys.readouterr().out

    def test_a_surface_without_a_binding_refuses_rather_than_crashing(self, capsys):
        class Bare:
            messages: list = []

        assert repl._slash("/resume 1", Bare(), None) == "continue"
        assert "cannot switch sessions" in capsys.readouterr().err

    def test_with_no_other_sessions_it_says_so(self, conversation, capsys):
        assert repl._slash("/resume", conversation, None) == "continue"
        assert "No other sessions" in capsys.readouterr().out


class TestTheOtherNewVerbs:
    def test_recap_reports_what_happened(self, conversation, capsys):
        conversation.messages = [
            {"role": "user", "content": "read the config"},
            {"role": "assistant", "content": "it sets the retry budget"},
        ]
        assert repl._slash("/recap", conversation, None) == "continue"
        assert "you asked" in capsys.readouterr().out

    def test_sessions_without_a_query_says_what_it_needs(self, conversation, capsys):
        assert repl._slash("/sessions", conversation, None) == "continue"
        assert "/sessions <text>" in capsys.readouterr().err

    def test_sessions_searches_past_transcripts(self, conversation, capsys):
        target = saved("the retry budget is three")
        repl._slash("/sessions retry budget", conversation, None)
        assert target.id in capsys.readouterr().out

    def test_sessions_reports_a_miss(self, conversation, capsys):
        saved("something else")
        repl._slash("/sessions quantum tunnelling", conversation, None)
        assert "Nothing found" in capsys.readouterr().out


class TestSurfaceParity:
    def test_the_two_surfaces_cannot_disagree(self):
        """They read one registry, so this is now true by construction rather
        than by two people remembering to edit two strings."""
        from andromeda_tui import app

        assert repl.slash_help() == app.slash_help()

    def test_the_new_verbs_are_documented(self):
        for verb in ("/recap", "/sessions", "/resume"):
            assert verb in repl.slash_help()
