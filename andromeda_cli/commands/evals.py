"""Running the behavioural evaluations."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from andromeda_agent import Callbacks, build_provider
from andromeda_agent import evals as evals_module
from andromeda_agent.evals import Outcome, discover, report_json

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
    mark = "[yellow]flaky[/yellow]" if outcome.flaky else marks[outcome.status]
    rate = (
        f"  [dim]{outcome.passes}/{outcome.attempts}[/dim]"
        if outcome.attempts > 1
        else ""
    )
    output.console.print(
        f"  {mark}  {outcome.scenario.name.ljust(34)}"
        f"{rate}  [dim]{outcome.seconds:.1f}s[/dim]"
    )
    if outcome.skipped:
        output.console.print(f"        [dim]{outcome.skipped}[/dim]")
    for failure in outcome.failures:
        output.console.print(f"        [red]{failure}[/red]")
    if outcome.error:
        output.console.print(f"        [red]{outcome.error}[/red]")


def run(
    pattern: str = "",
    as_json: bool = False,
    root: str | None = None,
    repeat: int = 1,
    jobs: int = 1,
) -> int:
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

    repeat = max(1, repeat)
    jobs = max(1, jobs)

    if not as_json:
        detail = f" {repeat}x each" if repeat > 1 else ""
        lanes = f", {jobs} at a time" if jobs > 1 else ""
        output.info(
            f"Running {len(scenarios)} scenario(s){detail} against the live "
            f"model{lanes}.\n"
        )

    config = config_module.load()
    started = time.time()
    outcomes = evals_module.run_suite(
        scenarios,
        config,
        _live_runner,
        repeat=repeat,
        jobs=jobs,
        # Printed as they finish when the run is serial. In parallel they are
        # collected in scenario order instead, because interleaved lines from
        # several scenarios at once are unreadable.
        on_result=_line if (not as_json and jobs == 1) else None,
    )

    saved = evals_module.save_run(
        config_module.home(), outcomes, model=str(config.get("model", ""))
    )

    if as_json:
        # Straight to stdout. Rich hard-wraps at the terminal width, which
        # inserts newlines inside JSON strings and produces something no parser
        # will accept — the same "a tty is not a pipe" rule as everywhere else.
        sys.stdout.write(report_json(outcomes) + "\n")
        sys.stdout.flush()
    else:
        if jobs > 1:
            for outcome in outcomes:
                _line(outcome)

        passed = sum(1 for o in outcomes if o.status == "pass")
        skipped = sum(1 for o in outcomes if o.status == "skip")
        flaky = sum(1 for o in outcomes if o.flaky)
        summary = f"\n  {passed}/{len(outcomes) - skipped} passed"
        if flaky:
            summary += f" · {flaky} flaky"
        if skipped:
            summary += f" · {skipped} skipped"
        summary += f" · {time.time() - started:.0f}s"
        output.console.print(summary)
        if saved is not None:
            output.info("  andromeda eval report   to compare with the last run")

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


def report(root: str | None = None) -> int:
    """What moved between the last two runs.

    The question an eval suite is actually for. A pass count on its own says
    nothing — the model changes underneath a prompt nobody edited, and the only
    way to notice is against what it did before.
    """
    runs = evals_module.past_runs(config_module.home())

    if not runs:
        output.info("  no runs recorded yet — andromeda eval")
        return 0
    if len(runs) == 1:
        output.info(f"  one run recorded ({runs[0]['at']}) — nothing to compare it to")
        return 0

    after, before = runs[0], runs[1]
    output.info(f"  {before['at']} → {after['at']}")
    if before.get("model") != after.get("model"):
        # The most likely explanation for everything below it.
        output.info(f"  model changed: {before.get('model')} → {after.get('model')}")
    output.console.print()

    moved = evals_module.compare(before, after)
    labels = [
        ("broke", "red", "broke"),
        ("shakier", "yellow", "less reliable"),
        ("fixed", "green", "fixed"),
        ("steadier", "green", "more reliable"),
        ("added", "cyan", "new"),
        ("removed", "dim", "gone"),
    ]

    anything = False
    for key, colour, label in labels:
        for name in moved[key]:
            anything = True
            output.console.print(f"  [{colour}]{label.ljust(14)}[/{colour}] {name}")

    if not anything:
        output.ok("Nothing moved.")
    return 0


def show_runs() -> int:
    runs = evals_module.past_runs(config_module.home())
    if not runs:
        output.info("  no runs recorded yet")
        return 0
    for run_data in runs:
        results = run_data.get("results", {})
        passed = sum(1 for item in results.values() if item.get("status") == "pass")
        output.console.print(
            f"  [cyan]{run_data['at']}[/cyan] [dim]{passed}/{len(results)} passed"
            f" · {run_data.get('model', '')}[/dim]"
        )
    return 0
