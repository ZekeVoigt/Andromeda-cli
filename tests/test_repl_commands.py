"""The REPL's glue.

Slash commands are the thinnest, least-covered code in the CLI and the easiest
to break with a rename — `_slash` reaches into the conversation, the checkpoint
stack, the process registry and the specialist table by attribute. Driving it
directly covers that without a pseudo-terminal, which was slow, needed a live
provider to get past startup, and tested the harness more than the code.
"""

from __future__ import annotations

import pytest

from andromeda_agent import Policy
from andromeda_agent.loop import Conversation
from andromeda_cli import repl
from andromeda_cli.checkpoints import CheckpointStack
from andromeda_tools import Workspace, build_registry
from andromeda_tools.processes import ProcessRegistry
from andromeda_tools.todo import TodoList
from support import ScriptedProvider


@pytest.fixture
def conversation(tmp_path):
    workspace = Workspace(tmp_path)
    todos = TodoList()
    processes = ProcessRegistry()
    live = Conversation(
        provider=ScriptedProvider(script=["ok"]),  # type: ignore[arg-type]
        policy=Policy(mode="ask", enabled=frozenset({"read_file", "terminal"})),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos, processes=processes),
    )
    live.process_registry = processes
    yield live
    processes.shutdown_all()


@pytest.fixture
def checkpoints():
    return CheckpointStack()


def run(command, conversation, checkpoints=None) -> str:
    return repl._slash(command, conversation, checkpoints)


class TestDispatch:
    def test_exit_and_quit_both_leave(self, conversation):
        assert run("/exit", conversation) == "exit"
        assert run("/quit", conversation) == "exit"

    def test_an_unknown_command_is_reported_and_continues(self, conversation, capsys):
        assert run("/teleport", conversation) == "continue"
        assert "Unknown command" in capsys.readouterr().err

    def test_help_lists_every_command_it_dispatches(self, conversation, capsys):
        """A command that exists but is undocumented is a command nobody finds."""
        run("/help", conversation)
        printed = capsys.readouterr().out
        for command in (
            "/new", "/rewind", "/history", "/ps",
            "/tools", "/skills", "/lanes", "/model", "/cwd", "/exit",
        ):
            assert command in printed, command


class TestInspection:
    def test_tools_lists_what_the_session_holds(self, conversation, capsys):
        run("/tools", conversation)
        printed = capsys.readouterr().out
        assert "read_file" in printed and "terminal" in printed

    def test_tools_says_which_ones_ask_first(self, conversation, capsys):
        run("/tools", conversation)
        assert "asks first" in capsys.readouterr().out

    def test_lanes_lists_the_specialists(self, conversation, capsys):
        run("/lanes", conversation)
        printed = capsys.readouterr().out
        for belt in ("scout", "writer", "verifier", "browser"):
            assert belt in printed, belt

    def test_cwd_prints_the_workspace_root(self, conversation, capsys, tmp_path):
        run("/cwd", conversation)
        assert str(tmp_path.resolve()) in capsys.readouterr().out

    def test_model_names_the_lane_and_the_model(self, conversation, capsys):
        run("/model", conversation)
        assert "test/model" in capsys.readouterr().out


class TestProcesses:
    def test_ps_with_nothing_running(self, conversation, capsys):
        run("/ps", conversation)
        assert "No background processes" in capsys.readouterr().out

    def test_ps_lists_a_started_process(self, conversation, capsys):
        conversation.process_registry.start(conversation.workspace, "sleep 5")
        run("/ps", conversation)
        assert "sleep 5" in capsys.readouterr().out


class TestRewind:
    def test_rewind_with_no_checkpoints(self, conversation, checkpoints, capsys):
        run("/rewind", conversation, checkpoints)
        assert "Nothing to rewind" in capsys.readouterr().out

    def test_history_with_no_checkpoints(self, conversation, checkpoints, capsys):
        run("/history", conversation, checkpoints)
        assert "No checkpoints yet" in capsys.readouterr().out

    def test_rewind_restores_the_transcript(self, conversation, checkpoints):
        before = list(conversation.messages)
        checkpoints.take(conversation.messages, "first question")

        conversation.messages.append({"role": "user", "content": "second"})
        conversation.messages.append({"role": "assistant", "content": "answer"})

        run("/rewind", conversation, checkpoints)
        assert conversation.messages == before

    def test_rewind_takes_the_most_recent_by_default(self, conversation, checkpoints):
        checkpoints.take(conversation.messages, "one")
        conversation.messages.append({"role": "user", "content": "two"})
        checkpoints.take(conversation.messages, "two")
        conversation.messages.append({"role": "user", "content": "three"})

        run("/rewind", conversation, checkpoints)
        assert [m.get("content") for m in conversation.messages if m["role"] == "user"] == ["two"]

    def test_rewind_to_a_numbered_checkpoint(self, conversation, checkpoints):
        first = checkpoints.take(conversation.messages, "one")
        conversation.messages.append({"role": "user", "content": "two"})
        checkpoints.take(conversation.messages, "two")
        conversation.messages.append({"role": "user", "content": "three"})

        run(f"/rewind {first.index}", conversation, checkpoints)
        assert all(m["role"] != "user" for m in conversation.messages)

    def test_rewind_to_an_unknown_checkpoint_is_reported(self, conversation, checkpoints, capsys):
        checkpoints.take(conversation.messages, "one")
        run("/rewind 99", conversation, checkpoints)
        assert "No checkpoint" in capsys.readouterr().err

    def test_history_lists_the_checkpoints(self, conversation, checkpoints, capsys):
        checkpoints.take(conversation.messages, "why is the build failing")
        run("/history", conversation, checkpoints)
        assert "why is the build failing" in capsys.readouterr().out


class TestNew:
    def test_new_clears_the_transcript(self, conversation):
        conversation.messages.append({"role": "user", "content": "something"})
        run("/new", conversation)
        assert len(conversation.messages) == 1
        assert conversation.messages[0]["role"] == "system"


class TestCreditsAndUsage:
    """The two questions that used to have one confusing answer.

    A user reported `/credits` reading "$0.10 out of $0.10" while they were
    plainly spending it. Three things caused that and all three are pinned
    here: the figure was rounded to the cent at a scale where a turn costs
    fractions of one, the headers describe the balance from *before* the reply
    they arrive on, and nothing anywhere reported what had actually been used.
    """

    def test_credits_shows_enough_places_for_the_balance_to_move(
        self, conversation, capsys
    ):
        from andromeda_agent import credits as credits_module

        conversation.provider.balance = credits_module.Balance(
            remaining_micros=99_700, grant_micros=100_000, access="active"
        )

        run("/credits", conversation)
        printed = capsys.readouterr().out

        assert "$0.0997" in printed
        assert "$0.10 of $0.10" not in printed

    def test_an_untouched_grant_still_reads_as_whole_dollars_and_cents(
        self, conversation, capsys
    ):
        """A window that has just renewed should say "$0.10 of $0.10"."""
        from andromeda_agent import credits as credits_module

        conversation.provider.balance = credits_module.Balance(
            remaining_micros=100_000, grant_micros=100_000, access="active"
        )

        run("/credits", conversation)

        assert "$0.10 of $0.10" in capsys.readouterr().out

    def test_credits_says_the_figure_lags_a_turn(self, conversation, capsys):
        from andromeda_agent import credits as credits_module

        conversation.provider.balance = credits_module.Balance(
            remaining_micros=61_240, grant_micros=100_000, access="active"
        )

        run("/credits", conversation)
        printed = capsys.readouterr().out

        assert "previous turn" in printed
        assert "/usage" in printed

    def test_usage_reports_nothing_before_the_first_reply(self, conversation, capsys):
        run("/usage", conversation)

        assert "Nothing counted yet" in capsys.readouterr().out

    def test_usage_reports_what_this_session_spent(self, conversation, capsys):
        conversation.usage.record("test/model", input=1200, output=340)

        run("/usage", conversation)
        printed = capsys.readouterr().out

        assert "this session" in printed
        assert "1.5k" in printed  # 1,540 tokens

    def test_usage_is_documented(self, conversation, capsys):
        run("/help", conversation)

        assert "/usage" in capsys.readouterr().out
