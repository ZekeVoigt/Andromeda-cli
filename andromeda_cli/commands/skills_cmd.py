"""`andromeda skills` — what is installed, what the scan found, what you allow.

`scan` is the one that matters. It prints what a skill contains that a scanner
thinks is worth knowing about, with the file and line, so the decision is made
by a person reading the actual text rather than by a verdict word.

`trust` records that decision against the skill's **content hash**: edit the
skill and it is behind the gate again, because what was accepted was the text
that was read.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from andromeda_tools import skill_scan
from andromeda_tools import skills as skills_module

from .. import config as config_module
from .. import output

SEVERITY_COLOUR = {
    "critical": "red",
    "high": "yellow",
    "medium": "cyan",
    "low": "dim",
}


def _discover(workspace: str = "") -> dict[str, skills_module.Skill]:
    return skills_module.discover(Path(workspace) if workspace else None)


def _results(workspace: str = "") -> tuple[dict, dict]:
    found = _discover(workspace)
    return found, skill_scan.screen(
        found, config_module.home(), skills_module.bundled_skills_dir()
    )


def show_list(workspace: str = "") -> int:
    found, results = _results(workspace)
    if not found:
        output.info("  no skills found")
        output.info(f"  put one in {config_module.home() / 'skills'}/<name>/SKILL.md")
        return 0

    plural = "" if len(found) == 1 else "s"
    output.info(f"  {len(found)} skill{plural}\n")

    for name in sorted(found):
        skill = found[name]
        result = results[name]
        allowed = skill_scan.is_allowed(result)
        mark = "[green]✓[/green]" if allowed else "[red]✗[/red]"
        output.console.print(
            f"  {mark} [cyan]{name}[/cyan] [dim]{result.trust}[/dim]"
        )
        detail = skill.description[:90] or "(no description)"
        output.console.print(f"      [dim]{detail}[/dim]")
        if not allowed:
            output.console.print(f"      [red]withheld — {result.summary()}[/red]")
        elif result.findings:
            # Allowed, but not silent about it: a medium finding is worth
            # knowing even when it decides nothing.
            output.console.print(f"      [dim]scan: {result.summary()}[/dim]")
        if not skill.available:
            output.console.print(
                f"      [yellow]needs {', '.join(skill.missing_bins)}[/yellow]"
            )
    return 0


def scan(name: str = "", workspace: str = "") -> int:
    found, results = _results(workspace)

    if name:
        if name not in found:
            known = ", ".join(sorted(found)) or "none"
            output.fail(f"No skill named {name!r}.", f"Found: {known}")
            return 2
        targets = [name]
    else:
        targets = sorted(found)

    if not targets:
        output.info("  no skills to scan")
        return 0

    blocked = 0
    for target in targets:
        result = results[target]
        _report(target, result)
        if not skill_scan.is_allowed(result):
            blocked += 1
        output.console.print()

    if blocked:
        plural = "" if blocked == 1 else "s"
        output.info(
            f"  {blocked} skill{plural} withheld · "
            f"andromeda skills trust <name> to use one anyway"
        )
        return 1
    return 0


def _report(name: str, result: skill_scan.ScanResult) -> None:
    verdict = result.verdict.upper()
    colour = {"safe": "green", "caution": "yellow", "dangerous": "red"}[result.verdict]
    output.console.print(
        f"  [cyan]{name}[/cyan] [dim]{result.trust}[/dim] "
        f"[{colour}]{verdict}[/{colour}]"
    )

    if result.trust == "builtin":
        output.console.print("      [dim]shipped with this install — not scanned[/dim]")
        return

    if not result.findings:
        output.console.print("      [dim]nothing found[/dim]")
        return

    ordered = sorted(
        result.findings,
        key=lambda item: (
            skill_scan.SEVERITY_ORDER.get(item.severity, 4),
            item.file,
            item.line,
        ),
    )
    for finding in ordered:
        tint = SEVERITY_COLOUR.get(finding.severity, "dim")
        where = f"{finding.file}:{finding.line}" if finding.line else finding.file
        output.console.print(
            f"      [{tint}]{finding.severity.ljust(8)}[/{tint}] "
            f"[dim]{where}[/dim]  {finding.description}"
        )
        if finding.match:
            # A `Text`, not a markup string: this is the skill's own content,
            # and a line containing `[dim]` or a bracketed path would otherwise
            # be parsed as styling — in the one command whose job is to show
            # exactly what the file says.
            output.console.print(Text(f"        {finding.match[:100]}", style="dim"))

    decision = "allowed" if skill_scan.is_allowed(result) else "withheld"
    output.console.print(f"      [dim]→ {decision}[/dim]")


def trust(name: str, workspace: str = "") -> int:
    found, results = _results(workspace)
    if name not in found:
        known = ", ".join(sorted(found)) or "none"
        output.fail(f"No skill named {name!r}.", f"Found: {known}")
        return 2

    result = results[name]
    if result.trust == "trusted-by-you":
        output.info(f"  {name} is already trusted at its current content")
        return 0
    if skill_scan.is_allowed(result):
        # Recorded anyway. The scan that allows it today may not tomorrow, and
        # an explicit decision should survive a change to the rules.
        output.info(f"  {name} was not being withheld — recording your decision anyway")

    skill_scan.approve(
        config_module.home(), result, Path(found[name].path).parent
    )
    output.ok(f"Trusting {name} at its current content.")
    output.info("  Editing the skill withdraws this — the hash is what was approved.")
    return 0


def untrust(name: str) -> int:
    removed = skill_scan.withdraw(config_module.home(), name)
    if not removed:
        output.info(f"  nothing recorded for {name}")
        return 0
    plural = "" if removed == 1 else "s"
    output.ok(f"Withdrew {removed} decision{plural} for {name}.")
    return 0
