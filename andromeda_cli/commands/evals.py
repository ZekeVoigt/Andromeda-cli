"""Running the behavioural evaluations."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from andromeda_agent import Callbacks, build_provider
from andromeda_agent.evals import Outcome, discover, report_json, run_scenario

from .. import config as config_module
from .. import output
from ..session import build_conversation


def default_root() -> Path:
    """`evals/` beside the checkout, then beside the working directory.

    Looked up rather than configured: the scenarios travel with the code they
    describe, and a path in a config file is a path that goes stale.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "evals"
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "evals"


def _live_runner(prompt: str, settings: dict, workspace: Path) -> tuple[str, list[str]]:
    provider = build_provider(settings)
    conversation, _record = build_conversation(
        settings, provider, interactive=False, workspace_root=str(workspace)
    )
    used: list[str] = []
    answer = conversation.send(
        prompt, Callbacks(on_tool_start=lambda spec, _args: used.append(spec.name))
    )
    return answer, used


def _line(outcome: Outcome) -> None:
    marks = {
        "pass": "[green]pass[/green]",
        "fail": "[red]fail[/red]",
        "error": "[red]err [/red]",
        "skip": "[dim]skip[/dim]",
    }
    output.console.print(
        f"  {marks[outcome.status]}  {outcome.scenario.name.ljust(34)}"
        f"  [dim]{outcome.seconds:.1f}s[/dim]"
    )
    if outcome.skipped:
        output.console.print(f"        [dim]{outcome.skipped}[/dim]")
    for failure in outcome.failures:
        output.console.print(f"        [red]{failure}[/red]")
    if outcome.error:
        output.console.print(f"        [red]{outcome.error}[/red]")


def run(pattern: str = "", as_json: bool = False, root: str | None = None) -> int:
    directory = Path(root).expanduser() if root else default_root()
    try:
        scenarios = discover(directory)
    except ValueError as exc:
        output.fail(str(exc))
        return 2

    if pattern:
        scenarios = [s for s in scenarios if pattern.lower() in s.name.lower()]

    if not scenarios:
        output.fail(f"No scenarios found in {directory}.")
        return 2

    if not as_json:
        output.info(f"Running {len(scenarios)} scenario(s) against the live model.\n")

    config = config_module.load()
    started = time.time()
    outcomes = [run_scenario(s, config, _live_runner) for s in scenarios]

    if as_json:
        # Straight to stdout. Rich hard-wraps at the terminal width, which
        # inserts newlines inside JSON strings and produces something no parser
        # will accept — the same "a tty is not a pipe" rule as everywhere else.
        sys.stdout.write(report_json(outcomes) + "\n")
        sys.stdout.flush()
    else:
        for outcome in outcomes:
            _line(outcome)

        passed = sum(1 for o in outcomes if o.status == "pass")
        skipped = sum(1 for o in outcomes if o.status == "skip")
        broken = len(outcomes) - passed - skipped
        summary = f"\n  {passed}/{len(outcomes) - skipped} passed"
        if skipped:
            summary += f" · {skipped} skipped"
        summary += f" · {time.time() - started:.0f}s"
        output.console.print(summary)

    return 1 if any(o.status in {"fail", "error"} for o in outcomes) else 0


def show_list(root: str | None = None) -> int:
    directory = Path(root).expanduser() if root else default_root()
    try:
        scenarios = discover(directory)
    except ValueError as exc:
        output.fail(str(exc))
        return 2

    if not scenarios:
        output.info(f"No scenarios in {directory}.")
        return 0

    for scenario in scenarios:
        output.console.print(
            f"  [cyan]{scenario.name.ljust(34)}[/cyan] "
            f"[dim]{len(scenario.checks)} checks · approval {scenario.approval}[/dim]"
        )
    output.info(f"\n  {directory}")
    return 0
