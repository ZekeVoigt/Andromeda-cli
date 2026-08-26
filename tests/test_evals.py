"""The evals harness.

Tested without a model: the harness's job is to build a workspace, run
something, and judge the result. What it runs is injected, so these are fast and
deterministic — the live suite is `andromeda eval`, which is the thing that
costs money and measures the real agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from andromeda_agent import evals


def write(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def runner_returning(answer: str = "", tools: list[str] | None = None):
    def run(prompt, settings, workspace):
        return answer, list(tools or [])

    return run


class TestLoading:
    def test_a_minimal_scenario_loads(self, tmp_path):
        path = write(
            tmp_path,
            "s.yaml",
            'prompt: do the thing\nexpect:\n  - answer_contains: "yes"\n',
        )
        scenario = evals.load_scenario(path)
        assert scenario.prompt == "do the thing"
        assert scenario.name == "s"
        assert len(scenario.checks) == 1

    def test_a_scenario_without_a_prompt_is_refused(self, tmp_path):
        path = write(tmp_path, "s.yaml", "expect:\n  - answer_contains: x\n")
        with pytest.raises(ValueError, match="needs a prompt"):
            evals.load_scenario(path)

    def test_a_scenario_without_expectations_is_refused(self, tmp_path):
        """It would pass always, making the suite look bigger than it is."""
        path = write(tmp_path, "s.yaml", "prompt: x\n")
        with pytest.raises(ValueError, match="at least one expectation"):
            evals.load_scenario(path)

    def test_an_unknown_check_names_the_known_ones(self, tmp_path):
        path = write(tmp_path, "s.yaml", "prompt: x\nexpect:\n  - vibes: good\n")
        with pytest.raises(ValueError, match="file_exists"):
            evals.load_scenario(path)

    def test_a_negated_check_is_recognised(self, tmp_path):
        path = write(tmp_path, "s.yaml", "prompt: x\nexpect:\n  - not_file_exists: a.txt\n")
        check = evals.load_scenario(path).checks[0]
        assert check.kind == "file_exists" and check.negate is True

    def test_discovery_finds_every_scenario(self, tmp_path):
        write(tmp_path, "a.yaml", "prompt: x\nexpect:\n  - answer_contains: y\n")
        write(tmp_path, "b.yml", "prompt: x\nexpect:\n  - answer_contains: y\n")
        assert len(evals.discover(tmp_path)) == 2

    def test_discovery_of_a_missing_directory_is_empty(self, tmp_path):
        assert evals.discover(tmp_path / "nope") == []


class TestChecks:
    def _scenario(self, tmp_path, expect: str):
        path = write(tmp_path, "s.yaml", f"prompt: x\nexpect:\n{expect}")
        return evals.load_scenario(path)

    def test_file_exists(self, tmp_path):
        scenario = self._scenario(tmp_path, "  - file_exists: made.txt\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "", []) == [
            "must file_exists 'made.txt'"
        ]

        (workspace / "made.txt").write_text("x", encoding="utf-8")
        assert evals.evaluate(scenario, workspace, "", []) == []

    def test_not_file_exists(self, tmp_path):
        scenario = self._scenario(tmp_path, "  - not_file_exists: forbidden.txt\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "", []) == []

        (workspace / "forbidden.txt").write_text("x", encoding="utf-8")
        assert evals.evaluate(scenario, workspace, "", []) != []

    def test_file_contains_is_case_insensitive(self, tmp_path):
        scenario = self._scenario(
            tmp_path, "  - file_contains:\n      out.txt: DONE\n"
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "out.txt").write_text("all done here", encoding="utf-8")
        assert evals.evaluate(scenario, workspace, "", []) == []

    def test_file_contains_fails_when_the_file_is_missing(self, tmp_path):
        scenario = self._scenario(tmp_path, "  - file_contains:\n      out.txt: DONE\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "", []) != []

    def test_answer_contains_accepts_a_list(self, tmp_path):
        scenario = self._scenario(tmp_path, "  - answer_contains: [alpha, beta]\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "Alpha and BETA", []) == []
        assert evals.evaluate(scenario, workspace, "only alpha", []) != []

    def test_answer_matches_is_a_regex(self, tmp_path):
        scenario = self._scenario(tmp_path, '  - answer_matches: "\\\\d+ lines"\n')
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "there are 42 lines", []) == []

    def test_tool_called(self, tmp_path):
        scenario = self._scenario(tmp_path, "  - tool_called: read_file\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert evals.evaluate(scenario, workspace, "", ["read_file"]) == []
        assert evals.evaluate(scenario, workspace, "", ["terminal"]) != []

    def test_every_failing_check_is_reported(self, tmp_path):
        """One run should tell you everything that is wrong."""
        scenario = self._scenario(
            tmp_path,
            "  - file_exists: a.txt\n  - answer_contains: nope\n  - tool_called: terminal\n",
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert len(evals.evaluate(scenario, workspace, "", [])) == 3


class TestRunning:
    def _scenario(self, tmp_path, body: str):
        return evals.load_scenario(write(tmp_path, "s.yaml", body))

    def test_fixture_files_are_created(self, tmp_path):
        scenario = self._scenario(
            tmp_path,
            "prompt: x\nfiles:\n  data.txt: |\n    one\n    two\n"
            "expect:\n  - file_contains:\n      data.txt: one\n",
        )
        outcome = evals.run_scenario(scenario, {}, runner_returning())
        assert outcome.passed is True

    def test_the_workspace_is_removed_afterwards(self, tmp_path):
        seen = {}

        def runner(prompt, settings, workspace):
            seen["path"] = workspace
            return "", []

        scenario = self._scenario(
            tmp_path, "prompt: x\nexpect:\n  - answer_contains: ''\n"
        )
        evals.run_scenario(scenario, {}, runner)
        assert not seen["path"].exists()

    def test_the_scenarios_approval_mode_reaches_the_runner(self, tmp_path):
        seen = {}

        def runner(prompt, settings, workspace):
            seen.update(settings)
            return "", []

        scenario = self._scenario(
            tmp_path, "prompt: x\napproval: ask\nexpect:\n  - answer_contains: ''\n"
        )
        evals.run_scenario(scenario, {"approval_mode": "auto"}, runner)
        assert seen["approval_mode"] == "ask"

    def test_a_broken_scenario_does_not_break_the_suite(self, tmp_path):
        def explode(prompt, settings, workspace):
            raise RuntimeError("model down")

        scenario = self._scenario(
            tmp_path, "prompt: x\nexpect:\n  - answer_contains: y\n"
        )
        outcome = evals.run_scenario(scenario, {}, explode)
        assert outcome.status == "error" and "model down" in outcome.error

    def test_a_missing_requirement_skips_rather_than_fails(self, tmp_path):
        """A suite that is red for environmental reasons stops being read."""
        scenario = self._scenario(
            tmp_path,
            "prompt: x\nrequires: [bin:definitely-not-installed-xyz]\n"
            "expect:\n  - answer_contains: y\n",
        )
        outcome = evals.run_scenario(scenario, {}, runner_returning())
        assert outcome.status == "skip"
        assert "definitely-not-installed-xyz" in outcome.skipped

    def test_a_present_requirement_does_not_skip(self, tmp_path):
        scenario = self._scenario(
            tmp_path,
            "prompt: x\nrequires: [bin:sh]\nexpect:\n  - answer_contains: ''\n",
        )
        assert evals.run_scenario(scenario, {}, runner_returning()).status != "skip"

    def test_an_env_requirement_is_checked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SOME_EVAL_KEY", raising=False)
        scenario = self._scenario(
            tmp_path,
            "prompt: x\nrequires: [env:SOME_EVAL_KEY]\nexpect:\n  - answer_contains: y\n",
        )
        assert evals.run_scenario(scenario, {}, runner_returning()).status == "skip"


class TestReport:
    def test_a_bare_yaml_boolean_is_refused_with_an_explanation(self, tmp_path):
        """`answer_contains: yes` would silently check for the string "true"."""
        path = write(tmp_path, "s.yaml", "prompt: x\nexpect:\n  - answer_contains: yes\n")
        with pytest.raises(ValueError, match="quote it"):
            evals.load_scenario(path)

    def test_a_boolean_inside_a_list_is_caught_too(self, tmp_path):
        path = write(tmp_path, "s.yaml", "prompt: x\nexpect:\n  - answer_contains: [ok, no]\n")
        with pytest.raises(ValueError, match="quote it"):
            evals.load_scenario(path)

    def test_the_json_report_parses_and_counts(self, tmp_path):
        scenario = evals.load_scenario(
            write(tmp_path, "s.yaml", 'prompt: x\nexpect:\n  - answer_contains: "yes"\n')
        )
        outcomes = [
            evals.run_scenario(scenario, {}, runner_returning("yes")),
            evals.run_scenario(scenario, {}, runner_returning("no")),
        ]
        report = json.loads(evals.report_json(outcomes))
        assert report["total"] == 2 and report["passed"] == 1 and report["failed"] == 1

    def test_the_report_carries_the_answer(self, tmp_path):
        """Without it a failing eval can only be diagnosed by reproducing it."""
        scenario = evals.load_scenario(
            write(tmp_path, "s.yaml", "prompt: x\nexpect:\n  - answer_contains: nope\n")
        )
        outcome = evals.run_scenario(scenario, {}, runner_returning("what it said"))
        report = json.loads(evals.report_json([outcome]))
        assert report["scenarios"][0]["answer"] == "what it said"


class TestShippedScenarios:
    """The scenarios that ship with the repo must at least be well formed."""

    def _root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "evals"

    def test_they_all_parse(self):
        root = self._root()
        if not root.is_dir():
            pytest.skip("running outside the monorepo checkout")
        scenarios = evals.discover(root)
        assert len(scenarios) >= 4

    def test_every_one_has_a_name_and_checks(self):
        root = self._root()
        if not root.is_dir():
            pytest.skip("running outside the monorepo checkout")
        for scenario in evals.discover(root):
            assert scenario.name and scenario.checks, scenario.path

    def test_approval_modes_are_real(self):
        root = self._root()
        if not root.is_dir():
            pytest.skip("running outside the monorepo checkout")
        from andromeda_agent.schedule import APPROVAL_MODES

        for scenario in evals.discover(root):
            assert scenario.approval in APPROVAL_MODES, scenario.path


class TestRepetition:
    """An agent is stochastic. One run of a stochastic system is an anecdote."""

    def scenario(self, tmp_path):
        (tmp_path / "s.yaml").write_text(
            "name: sometimes\nprompt: go\nexpect:\n  - answer_contains: yes-please\n",
            encoding="utf-8",
        )
        return evals.discover(tmp_path)[0]

    def test_a_single_run_has_one_attempt(self, tmp_path):
        outcome = evals.run_trials(
            self.scenario(tmp_path), {}, lambda p, s, w: ("yes-please", []), repeat=1
        )
        assert outcome.attempts == 1
        assert outcome.pass_rate == 1.0
        assert outcome.flaky is False

    def test_a_repeated_run_reports_a_pass_rate(self, tmp_path):
        answers = iter(["yes-please", "no", "yes-please", "no"])
        outcome = evals.run_trials(
            self.scenario(tmp_path),
            {},
            lambda p, s, w: (next(answers), []),
            repeat=4,
        )
        assert outcome.attempts == 4
        assert outcome.passes == 2
        assert outcome.pass_rate == 0.5

    def test_passing_sometimes_is_called_flaky(self, tmp_path):
        answers = iter(["yes-please", "no"])
        outcome = evals.run_trials(
            self.scenario(tmp_path), {}, lambda p, s, w: (next(answers), []), repeat=2
        )
        assert outcome.flaky is True

    def test_always_passing_is_not_flaky(self, tmp_path):
        outcome = evals.run_trials(
            self.scenario(tmp_path), {}, lambda p, s, w: ("yes-please", []), repeat=3
        )
        assert outcome.flaky is False
        assert outcome.status == "pass"

    def test_the_reported_outcome_is_a_failure_when_there_was_one(self, tmp_path):
        """The report should show what went wrong, not the run that happened to
        work."""
        answers = iter(["yes-please", "nope"])
        outcome = evals.run_trials(
            self.scenario(tmp_path), {}, lambda p, s, w: (next(answers), []), repeat=2
        )
        assert outcome.status == "fail"
        assert outcome.failures

    def test_a_skip_is_not_repeated(self, tmp_path):
        (tmp_path / "s.yaml").write_text(
            "name: needs\nprompt: go\nrequires: [bin:definitely-not-installed]\n"
            "expect:\n  - answer_contains: x\n",
            encoding="utf-8",
        )
        calls: list[int] = []
        outcome = evals.run_trials(
            evals.discover(tmp_path)[0],
            {},
            lambda p, s, w: (calls.append(1) or "x", []),
            repeat=5,
        )
        assert outcome.status == "skip"
        assert calls == []


class TestTheSuite:
    def scenarios(self, tmp_path, count=3):
        for index in range(count):
            (tmp_path / f"s{index}.yaml").write_text(
                f"name: scenario {index}\nprompt: go\n"
                f"expect:\n  - answer_contains: fine\n",
                encoding="utf-8",
            )
        return evals.discover(tmp_path)

    def test_every_scenario_runs(self, tmp_path):
        outcomes = evals.run_suite(
            self.scenarios(tmp_path), {}, lambda p, s, w: ("fine", [])
        )
        assert len(outcomes) == 3
        assert all(o.status == "pass" for o in outcomes)

    def test_results_keep_scenario_order_when_parallel(self, tmp_path):
        """A report whose order changes between runs cannot be diffed."""
        import time

        def uneven(prompt, settings, workspace):
            time.sleep(0.05)
            return "fine", []

        outcomes = evals.run_suite(
            self.scenarios(tmp_path, 4), {}, uneven, jobs=4
        )
        assert [o.scenario.name for o in outcomes] == [
            "scenario 0",
            "scenario 1",
            "scenario 2",
            "scenario 3",
        ]

    def test_parallel_is_faster(self, tmp_path):
        import time

        def slow(prompt, settings, workspace):
            time.sleep(0.1)
            return "fine", []

        started = time.time()
        evals.run_suite(self.scenarios(tmp_path, 5), {}, slow, jobs=5)
        assert time.time() - started < 0.4


class TestRunHistory:
    def outcome(self, name, status="pass", passes=1, attempts=1):
        scenario = evals.Scenario(name=name, prompt="go", path=Path(name))
        out = evals.Outcome(scenario=scenario, passed=status == "pass")
        if attempts > 1:
            out.trials = [
                evals.Outcome(scenario=scenario, passed=index < passes)
                for index in range(attempts)
            ]
        return out

    def test_a_run_is_saved_and_read_back(self, tmp_path):
        evals.save_run(tmp_path, [self.outcome("one")], model="test/model")
        runs = evals.past_runs(tmp_path)
        assert len(runs) == 1
        assert runs[0]["model"] == "test/model"
        assert runs[0]["results"]["one"]["status"] == "pass"

    def test_runs_come_back_newest_first(self, tmp_path):
        import time

        evals.save_run(tmp_path, [self.outcome("one")])
        time.sleep(1.05)
        evals.save_run(tmp_path, [self.outcome("two")])
        runs = evals.past_runs(tmp_path)
        assert list(runs[0]["results"]) == ["two"]

    def test_no_runs_is_an_empty_list(self, tmp_path):
        assert evals.past_runs(tmp_path) == []

    def test_a_corrupt_run_file_is_skipped(self, tmp_path):
        evals.runs_dir(tmp_path).mkdir(parents=True)
        (evals.runs_dir(tmp_path) / "20260101-000000.json").write_text("{not json")
        assert evals.past_runs(tmp_path) == []

    def test_a_scenario_that_broke_is_reported(self):
        before = {"results": {"a": {"status": "pass", "passes": 1, "attempts": 1}}}
        after = {"results": {"a": {"status": "fail", "passes": 0, "attempts": 1}}}
        assert evals.compare(before, after)["broke"] == ["a"]

    def test_a_scenario_that_was_fixed_is_reported(self):
        before = {"results": {"a": {"status": "fail", "passes": 0, "attempts": 1}}}
        after = {"results": {"a": {"status": "pass", "passes": 1, "attempts": 1}}}
        assert evals.compare(before, after)["fixed"] == ["a"]

    def test_a_scenario_getting_shakier_is_reported(self):
        """5/5 to 3/5 has not started failing, and it is the earliest thing
        worth knowing."""
        before = {"results": {"a": {"status": "pass", "passes": 5, "attempts": 5}}}
        after = {"results": {"a": {"status": "pass", "passes": 3, "attempts": 5}}}
        assert "a (100% → 60%)" in evals.compare(before, after)["shakier"]

    def test_a_scenario_getting_steadier_is_reported(self):
        before = {"results": {"a": {"status": "pass", "passes": 3, "attempts": 5}}}
        after = {"results": {"a": {"status": "pass", "passes": 5, "attempts": 5}}}
        assert evals.compare(before, after)["steadier"]

    def test_new_and_removed_scenarios_are_reported(self):
        before = {"results": {"gone": {"status": "pass"}}}
        after = {"results": {"new": {"status": "pass"}}}
        moved = evals.compare(before, after)
        assert moved["added"] == ["new"]
        assert moved["removed"] == ["gone"]

    def test_nothing_moving_is_all_empty(self):
        same = {"results": {"a": {"status": "pass", "passes": 1, "attempts": 1}}}
        assert all(not names for names in evals.compare(same, same).values())


class TestTheNewChecks:
    def outcome_for(self, tmp_path, yaml_body, answer="", tools=()):
        (tmp_path / "s.yaml").write_text(yaml_body, encoding="utf-8")
        scenario = evals.discover(tmp_path)[0]
        return evals.evaluate(scenario, tmp_path, answer, list(tools))

    def test_tools_in_order_is_a_subsequence(self, tmp_path):
        body = "name: x\nprompt: go\nexpect:\n  - tools_in_order: [read_file, write_file]\n"
        assert self.outcome_for(
            tmp_path, body, tools=["read_file", "list_dir", "write_file"]
        ) == []

    def test_tools_in_the_wrong_order_fail(self, tmp_path):
        body = "name: x\nprompt: go\nexpect:\n  - tools_in_order: [read_file, write_file]\n"
        assert self.outcome_for(tmp_path, body, tools=["write_file", "read_file"])

    def test_steps_under_counts_tool_calls(self, tmp_path):
        body = "name: x\nprompt: go\nexpect:\n  - steps_under: 3\n"
        assert self.outcome_for(tmp_path, body, tools=["a", "b"]) == []
        assert self.outcome_for(tmp_path, body, tools=["a", "b", "c"])

    def test_file_matches_is_a_regex(self, tmp_path):
        (tmp_path / "out.txt").write_text("version 2.1.4\n", encoding="utf-8")
        body = 'name: x\nprompt: go\nexpect:\n  - file_matches:\n      out.txt: "version \\\\d+\\\\.\\\\d+"\n'
        assert self.outcome_for(tmp_path, body) == []

    def test_file_matches_fails_when_the_file_is_missing(self, tmp_path):
        body = 'name: x\nprompt: go\nexpect:\n  - file_matches:\n      nope.txt: "x"\n'
        assert self.outcome_for(tmp_path, body)
