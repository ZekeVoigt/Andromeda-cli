"""`andromeda lsp` — which language servers this machine has, and what is missing.

The command exists because of the standing decision in `andromeda_agent.lsp`:
nothing is ever installed. A feature that silently does nothing when a server
is absent is a feature people report as broken, so there has to be one place
that says *which* server would have run here, whether it is present, and the
exact command that would install it.

`status` answers it for the current directory: the languages actually in this
project, first. `servers` lists every server the harness knows about.
"""

from __future__ import annotations

from pathlib import Path

from andromeda_agent import lsp, project
from andromeda_agent.lsp.report import SEVERITY

from .. import config as config_module
from .. import output


def status(path: str = "") -> int:
    config = config_module.load()
    workspace = project.locate(path or Path.cwd())

    if not bool(config.get("lsp", True)):
        output.info("  Diagnostics are off — andromeda config set lsp true")
        return 0

    if workspace is None:
        output.info(
            "  Not inside a workspace, so no language server would be started here."
        )
        output.info("  Diagnostics run against a project root, never against $HOME.")
        return 0

    severities = lsp.parse_severities(config.get("lsp_severities"))
    output.info(f"  Workspace  {workspace.root}")
    output.info(f"  Reporting  {_severity_names(severities)}\n")

    applicable = lsp.relevant(workspace.root)
    if not applicable:
        output.info("  No file here is one this harness has a language server for.")
        return 0

    missing = [entry for entry in applicable if not entry.available]
    for entry in applicable:
        if entry.available:
            output.console.print(
                f"  [green]✓[/green] [cyan]{entry.server.id:<16}[/cyan] "
                f"[dim]{entry.server.label}[/dim]"
            )
            output.console.print(f"      [dim]{entry.binary}[/dim]")
        else:
            output.console.print(
                f"  [red]✗[/red] [cyan]{entry.server.id:<16}[/cyan] "
                f"[dim]{entry.server.label}[/dim]"
            )
            output.console.print(f"      [dim]install: {entry.server.install}[/dim]")

    if missing:
        output.console.print()
        plural = "" if len(missing) == 1 else "s"
        output.info(
            f"  {len(missing)} server{plural} would be used here and "
            f"{'is' if len(missing) == 1 else 'are'} not installed."
        )
        output.info("  Nothing is installed for you — run the command above yourself.")
    return 0


def servers() -> int:
    """Every server the harness knows, whether or not this project uses it."""
    for entry in lsp.survey():
        mark = "[green]✓[/green]" if entry.available else "[dim]·[/dim]"
        extensions = " ".join(sorted(entry.server.extensions))
        output.console.print(
            f"  {mark} [cyan]{entry.server.id:<18}[/cyan] [dim]{extensions}[/dim]"
        )
        if not entry.available:
            output.console.print(f"      [dim]{entry.server.install}[/dim]")
    return 0


def _severity_names(severities) -> str:
    return ", ".join(SEVERITY[number] for number in sorted(severities) if number in SEVERITY)
