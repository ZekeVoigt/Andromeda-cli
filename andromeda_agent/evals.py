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

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        if self.error:
            return "error"
        return "pass" if self.passed else "fail"


CHECK_KINDS = (
    "file_exists",
    "file_contains",
    "answer_contains",
    "answer_matches",
    "tool_called",
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
    if not root.is_dir():
        return []
    scenarios = []
    for path in sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml")):
        scenarios.append(load_scenario(path))
    return scenarios


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

    if check.kind == "tool_called":
        wanted = check.value if isinstance(check.value, list) else [check.value]
        return all(str(w) in tools for w in wanted)

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
