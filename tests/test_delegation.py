"""Delegation: the brief, the belt, and the limits on a lane."""

from __future__ import annotations

import pytest

from andromeda_agent.delegation import Delegation, build_brief, make_delegate_tool
from andromeda_agent.specialists import SPECIALISTS
from andromeda_cli import config as config_module
from andromeda_cli.session import build_conversation
from support import ScriptedProvider, call, turn_with


class TestBrief:
    def test_states_the_task_and_the_belt(self):
        brief = build_brief("scout", "Find the config file")
        assert "Find the config file" in brief
        assert SPECIALISTS["scout"].purpose in brief

    def test_states_the_step_budget(self):
        brief = build_brief("writer", "Draft it")
        assert str(SPECIALISTS["writer"].max_turns) in brief

    def test_says_the_lane_cannot_see_the_conversation(self):
        assert "cannot see the conversation" in build_brief("scout", "x")

    def test_context_is_included_when_given(self):
        brief = build_brief("scout", "x", context="The file is at src/a.py")
        assert "src/a.py" in brief

    def test_criteria_require_an_explicit_answer(self):
        """The hosted runtime accepts criteria and never grades them."""
        brief = build_brief("scout", "x", success_criteria=["names the file", "cites a line"])
        assert "names the file" in brief
        assert "state for each check whether it is met" in brief

    def test_empty_criteria_add_no_section(self):
        assert "Checks your answer" not in build_brief("scout", "x", success_criteria=[])

    def test_expected_output_is_included(self):
        brief = build_brief("scout", "x", expected_output="a JSON array")
        assert "a JSON array" in brief


class TestDelegateTool:
    def _tool(self, outcome=None, recorder=None):
        def run_lane(**kwargs):
            if recorder is not None:
                recorder.append(kwargs)
            return outcome or Delegation("scout", "Scout", "the report", 3)

        return make_delegate_tool(run_lane)

    def test_returns_the_lane_report(self):
        result = self._tool().run(task="find it")
        assert result.ok and "the report" in result.content

    def test_an_empty_task_is_refused(self):
        assert self._tool().run(task="   ").ok is False

    def test_an_unknown_specialist_lists_the_real_ones(self):
        result = self._tool().run(task="x", specialist="wizard")
        assert result.ok is False and "scout" in result.content

    def test_it_defaults_to_the_specialist_that_changes_nothing(self):
        seen = []
        self._tool(recorder=seen).run(task="x")
        assert seen[0]["specialist"] == "scout"

    def test_arguments_reach_the_lane(self):
        seen = []
        self._tool(recorder=seen).run(
            task="t", context="c", successCriteria=["a"], expectedOutput="e", label="L"
        )
        assert seen[0]["context"] == "c"
        assert seen[0]["success_criteria"] == ["a"]
        assert seen[0]["expected_output"] == "e"

    def test_a_failing_lane_is_a_result_not_a_raise(self):
        def explode(**_):
            raise RuntimeError("lane died")

        assert make_delegate_tool(explode).run(task="x").ok is False

    def test_a_long_report_is_truncated(self):
        long = Delegation("scout", "Scout", "x" * 20_000, 4)
        result = self._tool(outcome=long).run(task="x")
        assert result.metadata["truncated"] is True

    def test_the_summary_names_the_lane_and_the_task(self):
        spec = self._tool()
        summary = spec.summary({"specialist": "writer", "task": "draft the memo"})
        assert "writer" in summary and "draft the memo" in summary


class TestLaneExecution:
    """The synchronous path, end to end through the real session builder.

    `background: False` throughout, deliberately: these assert on ordering
    through a shared scripted provider, and a script read from three threads at
    once is not a test of anything. The concurrent path is covered by
    `test_lanes.py` and by `TestBackgroundLanes` below.

    The parent and its lanes share one provider instance and pull from one
    script in order: the parent's turn, then the lane's turns, then the parent
    again. `run_lane` closes over the provider given at build time, so the
    provider must be passed in — reassigning `conversation.provider` afterwards
    does not reach a lane.
    """

    def build(self, tmp_path, script, **overrides):
        config = config_module.load()
        config.update({"approval_mode": "auto", **overrides})
        provider = ScriptedProvider(script=list(script))
        conversation, record = build_conversation(
            config, provider, interactive=True, workspace_root=str(tmp_path)
        )
        return conversation, provider

    def test_a_lane_runs_and_reports_back(self, tmp_path):
        (tmp_path / "answer.txt").write_text("42", encoding="utf-8")

        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "read answer.txt", "specialist": "scout"})),
                turn_with(call("read_file", {"path": "answer.txt"}, "lane1")),
                "The file says 42.",
                "It says 42.",
            ],
        )

        assert conversation.send("what does answer.txt say") == "It says 42."

        # The lane ran on the brief as its system prompt, not the parent's.
        lane_prompts = [seen[0]["content"] for seen in provider.seen]
        assert any("Scout lane" in prompt for prompt in lane_prompts)
        assert any("read answer.txt" in prompt for prompt in lane_prompts)

        # And its report reached the parent.
        tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
        assert "The file says 42." in tool_message["content"]

    def test_a_lane_is_not_given_the_delegate_tool(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "go", "specialist": "scout"})),
                "nothing to delegate to",
                "done",
            ],
        )
        conversation.send("go")

        # provider.seen_tools[1] is the lane's advertised toolbelt.
        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert "delegate" not in lane_tools
        assert "read_file" in lane_tools

    def test_a_scout_lane_is_offered_nothing_that_changes_anything(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "go", "specialist": "scout"})),
                "report",
                "done",
            ],
        )
        conversation.send("go")

        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert lane_tools & {"write_file", "patch", "terminal", "memory_store"} == set()
        assert "read_file" in lane_tools

    def test_a_writer_lane_is_offered_no_network(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "draft", "specialist": "writer"})),
                "drafted",
                "done",
            ],
        )
        conversation.send("draft it")

        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert "web_fetch" not in lane_tools and "web_search" not in lane_tools
        assert "read_file" in lane_tools

    def test_a_verifier_lane_cannot_store_what_it_concludes(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "check", "specialist": "verifier"})),
                "checked",
                "done",
            ],
        )
        conversation.send("check it")

        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert "memory_store" not in lane_tools
        assert "memory_search" in lane_tools

    def test_allowed_tools_cannot_grant_what_the_parent_lacks(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(
                    call(
                        "delegate",
                        {
                            "background": False, "task": "go",
                            "specialist": "scout",
                            "allowedTools": ["terminal", "read_file"],
                        },
                    )
                ),
                "report",
                "done",
            ],
            enabled_tools=["read_file", "delegate"],
        )
        conversation.send("go")

        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert "terminal" not in lane_tools
        assert lane_tools == {"read_file"}

    def test_denied_tools_are_applied_after_allowed(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(
                    call(
                        "delegate",
                        {
                            "background": False, "task": "go",
                            "specialist": "scout",
                            "allowedTools": ["read_file", "list_dir"],
                            "deniedTools": ["list_dir"],
                        },
                    )
                ),
                "report",
                "done",
            ],
        )
        conversation.send("go")

        lane_tools = {tool["function"]["name"] for tool in provider.seen_tools[1] or []}
        assert lane_tools == {"read_file"}

    def test_a_lane_stops_at_its_specialists_step_budget(self, tmp_path):
        """A lane that could run forever must stop where its belt says."""
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        budget = SPECIALISTS["writer"].max_turns

        script = [turn_with(call("delegate", {"background": False, "task": "draft", "specialist": "writer"}))]
        # Nothing but tool calls, so the only way this lane ends is the ceiling.
        script += [
            turn_with(call("read_file", {"path": "a.txt"}, f"l{i}"))
            for i in range(budget + 20)
        ]

        conversation, provider = self.build(tmp_path, script)
        conversation.send("draft")

        report = [m for m in conversation.messages if m["role"] == "tool"][0]["content"]
        assert f"Stopped after {budget} steps" in report

    def test_the_parents_own_budget_is_unchanged_by_a_lane(self, tmp_path):
        from andromeda_agent.loop import MAX_STEPS

        conversation, _ = self.build(tmp_path, ["done"])
        assert conversation.max_steps == MAX_STEPS


    def test_the_brief_lists_the_lanes_real_toolbelt(self, tmp_path):
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "go", "specialist": "writer"})),
                "report",
                "done",
            ],
        )
        conversation.send("go")

        brief = provider.seen[1][0]["content"]
        assert "- read_file" in brief
        # And nothing the belt denies.
        assert "- terminal" not in brief and "- write_file" not in brief

    def test_the_reported_step_count_is_model_turns_not_user_messages(self, tmp_path):
        """A lane sends one user message and may take many steps answering it."""
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        conversation, provider = self.build(
            tmp_path,
            [
                turn_with(call("delegate", {"background": False, "task": "look", "specialist": "scout"})),
                turn_with(call("read_file", {"path": "a.txt"}, "l1")),
                turn_with(call("read_file", {"path": "a.txt"}, "l2")),
                "found it",
                "done",
            ],
        )
        conversation.send("look")

        report = [m for m in conversation.messages if m["role"] == "tool"][0]["content"]
        assert "3 steps" in report


class TestLaneEvidence:
    """A lane's report must not be able to masquerade as work it did not do.

    Observed live: a Writer lane with no shell wrote `<shell><command>printf
    ...</command></shell>` into its prose, and the parent reported that a
    command had run. The belt had correctly prevented the action; the report
    still misled.
    """

    def _tool(self, outcome):
        return make_delegate_tool(lambda **_: outcome)

    def test_the_header_states_what_was_actually_called(self):
        outcome = Delegation("scout", "Scout", "report", 2, tools_used=["read_file", "list_dir"])
        result = self._tool(outcome).run(task="x")
        assert "called list_dir, read_file" in result.content

    def test_repeats_are_counted_not_listed(self):
        outcome = Delegation("scout", "Scout", "r", 3, tools_used=["read_file"] * 3)
        assert "read_file×3" in self._tool(outcome).run(task="x").content

    def test_a_lane_that_called_nothing_says_so(self):
        outcome = Delegation("writer", "Writer", "I ran `printf hello > x`", 1, tools_used=[])
        result = self._tool(outcome).run(task="x")
        assert "called no tools" in result.content

    def test_the_brief_names_the_toolbelt(self):
        brief = build_brief("scout", "x", toolbelt=["read_file", "list_dir"])
        assert "- read_file" in brief and "- list_dir" in brief
        assert "That is the complete list" in brief

    def test_the_brief_forbids_pseudo_tool_calls(self):
        # The phrase wraps across a line in the template, so match a fragment
        # that cannot straddle the break.
        assert "anything shaped like a tool" in build_brief(
            "scout", "x", toolbelt=["read_file"]
        )

    def test_an_empty_toolbelt_is_stated_plainly(self):
        assert "you can only reason and report" in build_brief("writer", "x", toolbelt=[])
