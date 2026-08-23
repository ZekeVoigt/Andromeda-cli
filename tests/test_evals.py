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
