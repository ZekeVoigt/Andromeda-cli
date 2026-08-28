"""Behavioural evaluations.

Unit tests answer "does this function do what I wrote". An eval answers "does
the agent still do the right thing" — which is a different question, because the
agent's behaviour depends on a model that changes underneath you, a prompt you
edit, and a tool description you reword. None of those break a unit test.

A scenario is a workspace, a prompt, and a set of checks. Checks are written
against **observable outcomes** — files that exist, tools that were called,
text that appears — never against the model's exact words, because asserting on
phrasing produces a suite that fails on a synonym and passes on a lie.

Runs cost money. That is the point: the thing being measured is the real agent
against the real model, and a mocked eval measures the mock.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIMEOUT = 180


@dataclass
class Check:
    """One assertion about what happened."""

    kind: str
    value: Any
    negate: bool = False

    def describe(self) -> str:
        prefix = "must not" if self.negate else "must"
        return f"{prefix} {self.kind} {self.value!r}"


@dataclass
class Scenario:
    name: str
    prompt: str
    path: Path
    files: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    approval: str = "auto"
    thinking: str = "off"
    timeout: int = DEFAULT_TIMEOUT
    # Scenarios that need something the machine may not have.
    requires: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


@dataclass
class Outcome:
    scenario: Scenario
    passed: bool = False
    skipped: str = ""
    failures: list[str] = field(default_factory=list)
    answer: str = ""
    tools_used: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""
    # Every attempt, when the scenario was run more than once. Empty for a
    # single run, where the outcome *is* the trial.
    trials: list["Outcome"] = field(default_factory=list, repr=False)

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        if self.error:
            return "error"
        return "pass" if self.passed else "fail"

    @property
    def attempts(self) -> int:
        return len(self.trials) or 1

    @property
    def passes(self) -> int:
        if not self.trials:
            return 1 if self.status == "pass" else 0
        return sum(1 for trial in self.trials if trial.status == "pass")

    @property
    def pass_rate(self) -> float:
        return self.passes / self.attempts if self.attempts else 0.0

    @property
    def flaky(self) -> bool:
        """Passed sometimes. The most useful thing a repeated eval reports —
        an intermittent behaviour is a real finding, and a single run reports
        it as either fine or broken depending on the day."""
        return bool(self.trials) and 0 < self.passes < self.attempts


CHECK_KINDS = (
    "file_exists",
    "file_contains",
    "file_matches",
    "answer_contains",
    "answer_matches",
    "tool_called",
    "tools_in_order",
    "steps_under",
)


def load_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: a scenario must be a mapping.")

    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"{path}: a scenario needs a prompt.")

    checks: list[Check] = []
    for entry in raw.get("expect") or []:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each expectation must be a mapping.")
        for kind, value in entry.items():
            negate = kind.startswith("not_")
            bare = kind[4:] if negate else kind
            if bare not in CHECK_KINDS:
                raise ValueError(
                    f"{path}: unknown check {kind!r}. "
                    f"Known: {', '.join(CHECK_KINDS)} (prefix with not_ to invert)."
                )
            _reject_yaml_booleans(path, kind, value)
            checks.append(Check(kind=bare, value=value, negate=negate))

    if not checks:
        # A scenario with nothing to check passes always, which is worse than
        # not having it: it makes the suite look bigger than it is.
        raise ValueError(f"{path}: a scenario needs at least one expectation.")

    return Scenario(
        name=str(raw.get("name") or path.stem),
        prompt=prompt,
        path=path,
        files={str(k): str(v) for k, v in (raw.get("files") or {}).items()},
        checks=checks,
        approval=str(raw.get("approval") or "auto"),
        thinking=str(raw.get("thinking") or "off"),
        timeout=int(raw.get("timeout") or DEFAULT_TIMEOUT),
        requires=[str(r) for r in (raw.get("requires") or [])],
    )


# YAML 1.1 reads bare `yes`, `no`, `on`, `off`, `true` and `false` as booleans.
# `answer_contains: yes` therefore checks for the string "true", which is not
# what anyone means and fails in a way that looks like the agent misbehaved
# rather than like the scenario is wrong.
def _reject_yaml_booleans(path: Path, kind: str, value: Any) -> None:
    values = value if isinstance(value, list) else [value]
    if isinstance(value, dict):
        values = list(value.values())
    for item in values:
        if isinstance(item, bool):
            raise ValueError(
                f"{path}: `{kind}` was read as the boolean {item}. YAML treats "
                "bare yes/no/on/off/true/false that way — quote it, as "
                f'`{kind}: "yes"`.'
            )


def discover(root: Path) -> list[Scenario]:
    """Every scenario: the files under `root`, then the ones plugins added.

    Files first, so a plugin cannot shadow a scenario the user wrote — an eval
    that silently stopped testing what its name says is worse than a missing
    one, because the suite still goes green.
    """
    scenarios: list[Scenario] = []
    if root.is_dir():
        for path in sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml")):
            scenarios.append(load_scenario(path))

    seen = {item.name for item in scenarios}
    for scenario in _plugin_scenarios():
        if scenario.name not in seen:
            scenarios.append(scenario)
            seen.add(scenario.name)
    return scenarios


def _plugin_scenarios() -> list[Scenario]:
    """Scenarios a plugin registered.

    Ungated: an eval runs only when somebody types the command, and one that
    is wrong fails its own check rather than anybody else's.
    """
    try:
        from . import plugins as plugins_module

        return [
            scenario
            for scenario in plugins_module.evals()
            if isinstance(scenario, Scenario)
        ]
    except Exception:  # noqa: BLE001 - evals must not depend on plugins loading
        return []

def missing_requirements(scenario: Scenario) -> list[str]:
    """Requirements this machine cannot satisfy.

    A scenario that needs `rg` on a machine without it should be reported as
    skipped, not failed — a red suite that is red for environmental reasons is
    a suite people stop reading.
    """
    missing = []
    for requirement in scenario.requires:
        if requirement.startswith("bin:"):
            if not shutil.which(requirement[4:]):
                missing.append(requirement[4:])
        elif requirement.startswith("env:"):
            import os

            if not os.environ.get(requirement[4:], "").strip():
                missing.append(f"${requirement[4:]}")
    return missing


def materialise(scenario: Scenario, root: Path) -> None:
    for name, body in scenario.files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def evaluate(scenario: Scenario, root: Path, answer: str, tools: list[str]) -> list[str]:
    """Every failing check, so one run tells you everything that is wrong."""
    failures: list[str] = []

    for check in scenario.checks:
        held = _holds(check, root, answer, tools)
        if held == (not check.negate):
            continue
        failures.append(check.describe())
    return failures


def _holds(check: Check, root: Path, answer: str, tools: list[str]) -> bool:
    if check.kind == "file_exists":
        return (root / str(check.value)).exists()

    if check.kind == "file_contains":
        if not isinstance(check.value, dict):
            return False
        for name, needle in check.value.items():
            target = root / str(name)
            if not target.exists():
                return False
            if str(needle).lower() not in target.read_text(
                encoding="utf-8", errors="replace"
            ).lower():
                return False
        return True

    if check.kind == "answer_contains":
        needles = check.value if isinstance(check.value, list) else [check.value]
        return all(str(n).lower() in answer.lower() for n in needles)

    if check.kind == "answer_matches":
        return re.search(str(check.value), answer, re.IGNORECASE | re.DOTALL) is not None

    if check.kind == "file_matches":
        if not isinstance(check.value, dict):
            return False
        for name, pattern in check.value.items():
            target = root / str(name)
            if not target.exists():
                return False
            body = target.read_text(encoding="utf-8", errors="replace")
            if re.search(str(pattern), body, re.IGNORECASE | re.DOTALL) is None:
                return False
        return True

    if check.kind == "tool_called":
        wanted = check.value if isinstance(check.value, list) else [check.value]
        return all(str(w) in tools for w in wanted)

    if check.kind == "tools_in_order":
        # Subsequence, not equality: "it read the file before it wrote it" is
        # the property worth asserting, and demanding the exact call list makes
        # a scenario fail because the agent also checked something sensible.
        wanted = check.value if isinstance(check.value, list) else [check.value]
        remaining = list(tools)
        for name in wanted:
            if str(name) not in remaining:
                return False
            remaining = remaining[remaining.index(str(name)) + 1 :]
        return True

    if check.kind == "steps_under":
        try:
            ceiling = int(check.value)
        except (TypeError, ValueError):
            return False
        return len(tools) < ceiling

    return False


def run_scenario(scenario: Scenario, config: dict[str, Any], runner) -> Outcome:
    """Run one scenario in a throwaway workspace.

    `runner(prompt, settings, workspace) -> (answer, tools)` is injected so the
    harness can be exercised without a model, while the real one drives the real
    agent.
    """
    outcome = Outcome(scenario=scenario)

    missing = missing_requirements(scenario)
    if missing:
        outcome.skipped = f"needs {', '.join(missing)}"
        return outcome

    started = time.time()
    workspace = Path(tempfile.mkdtemp(prefix=f"andromeda-eval-{scenario.slug}-"))
    try:
        materialise(scenario, workspace)
        settings = {
            **config,
            "approval_mode": scenario.approval,
            "thinking": scenario.thinking,
        }
        answer, tools = runner(scenario.prompt, settings, workspace)
        outcome.answer = answer
        outcome.tools_used = tools
        outcome.failures = evaluate(scenario, workspace, answer, tools)
        outcome.passed = not outcome.failures
    except Exception as exc:  # noqa: BLE001 - one broken scenario is not a broken suite
        outcome.error = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        outcome.seconds = time.time() - started
        shutil.rmtree(workspace, ignore_errors=True)

    return outcome


def run_trials(
    scenario: Scenario, config: dict[str, Any], runner, repeat: int = 1
) -> Outcome:
    """Run one scenario `repeat` times and report the aggregate.

    An agent is stochastic. One run of a stochastic system is an anecdote, and
    a suite built out of anecdotes moves for reasons nobody can attribute. The
    aggregate is reported as a **pass rate**, and a scenario that passed some
    of the time is called flaky rather than rounded to either answer.

    The reported outcome is the first failure when there is one, so the report
    shows what went wrong rather than the run that happened to work.
    """
    if repeat <= 1:
        return run_scenario(scenario, config, runner)

    trials = [run_scenario(scenario, config, runner) for _ in range(repeat)]

    if trials[0].skipped:
        # A skip is a property of the machine, not of the attempt.
        return trials[0]

    representative = next(
        (trial for trial in trials if trial.status != "pass"), trials[0]
    )
    representative.trials = trials
    representative.seconds = sum(trial.seconds for trial in trials)
    return representative


def run_suite(
    scenarios: list[Scenario],
    config: dict[str, Any],
    runner,
    *,
    repeat: int = 1,
    jobs: int = 1,
    on_result=None,
) -> list[Outcome]:
    """Run every scenario, optionally several at a time.

    Threads rather than processes: a run is almost entirely waiting on a model,
    and each scenario already has its own throwaway workspace, so there is
    nothing shared to protect. Results come back in the order the scenarios
    were given regardless of the order they finished, because a report whose
    order changes between runs cannot be diffed.
    """
    if jobs <= 1:
        results = []
        for scenario in scenarios:
            outcome = run_trials(scenario, config, runner, repeat)
            if on_result is not None:
                on_result(outcome)
            results.append(outcome)
        return results

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(run_trials, scenario, config, runner, repeat)
            for scenario in scenarios
        ]
        results = []
        for future in futures:
            outcome = future.result()
            if on_result is not None:
                on_result(outcome)
            results.append(outcome)
    return results


# ---------------------------------------------------------------------------
# Keeping the runs, so two of them can be compared
# ---------------------------------------------------------------------------

RUNS_DIRNAME = "eval-runs"


def runs_dir(home: Path) -> Path:
    return Path(home) / RUNS_DIRNAME


def save_run(home: Path, outcomes: list[Outcome], model: str = "") -> Path | None:
    """Write one run down, named for when it happened.

    Kept because the interesting question is never "did it pass" but "did it
    pass *last week*" — a model changes underneath a prompt nobody edited, and
    without a previous run there is nothing to notice that against.
    """
    directory = runs_dir(home)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = directory / f"{stamp}.json"
    payload = {
        "at": stamp,
        "model": model,
        "results": {
            outcome.scenario.name: {
                "status": outcome.status,
                "passes": outcome.passes,
                "attempts": outcome.attempts,
                "seconds": round(outcome.seconds, 1),
                "failures": outcome.failures,
            }
            for outcome in outcomes
        },
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return None
    return path


def past_runs(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Saved runs, newest first."""
    directory = runs_dir(home)
    if not directory.is_dir():
        return []
    runs = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("results"), dict):
            data["path"] = str(path)
            runs.append(data)
    return runs


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    """What moved between two runs.

    Four buckets, and `shakier`/`steadier` are the reason the pass rate is
    stored rather than a boolean: a scenario going from 5/5 to 3/5 has not
    started failing, and it is the earliest thing worth knowing.
    """
    old = before.get("results", {})
    new = after.get("results", {})

    moved: dict[str, list[str]] = {
        "broke": [],
        "fixed": [],
        "shakier": [],
        "steadier": [],
        "added": [],
        "removed": [],
    }

    for name, current in sorted(new.items()):
        previous = old.get(name)
        if previous is None:
            moved["added"].append(name)
            continue

        was = _rate(previous)
        now = _rate(current)

        if previous.get("status") == "pass" and current.get("status") != "pass":
            moved["broke"].append(name)
        elif previous.get("status") != "pass" and current.get("status") == "pass":
            moved["fixed"].append(name)
        elif now < was:
            moved["shakier"].append(f"{name} ({was:.0%} → {now:.0%})")
        elif now > was:
            moved["steadier"].append(f"{name} ({was:.0%} → {now:.0%})")

    for name in sorted(old):
        if name not in new:
            moved["removed"].append(name)

    return moved


def _rate(entry: dict[str, Any]) -> float:
    attempts = int(entry.get("attempts") or 0)
    if attempts <= 0:
        return 1.0 if entry.get("status") == "pass" else 0.0
    return int(entry.get("passes") or 0) / attempts


def report_json(outcomes: list[Outcome]) -> str:
    return json.dumps(
        {
            "total": len(outcomes),
            "passed": sum(1 for o in outcomes if o.status == "pass"),
            "failed": sum(1 for o in outcomes if o.status == "fail"),
            "errored": sum(1 for o in outcomes if o.status == "error"),
            "skipped": sum(1 for o in outcomes if o.status == "skip"),
            "scenarios": [
                {
                    "name": o.scenario.name,
                    "status": o.status,
                    "attempts": o.attempts,
                    "passes": o.passes,
                    "flaky": o.flaky,
                    "seconds": round(o.seconds, 1),
                    "failures": o.failures,
                    "error": o.error,
                    "skipped": o.skipped,
                    "toolsUsed": o.tools_used,
                    # Included so a failing eval can be diagnosed from its
                    # report. Without it the only way to see what the agent
                    # actually said is to reproduce the run by hand.
                    "answer": o.answer[:4000],
                }
                for o in outcomes
            ],
        },
        indent=2,
    )
