"""Everything the CLI prints.

One module so the terminal's voice is consistent and so tests can assert on
rendering without importing a command.
"""

from __future__ import annotations

from rich.text import Text

from andromeda_agent.errors import AgentError

# Shares the render layer's console so styling and highlighting are decided in
# one place. Two consoles with different settings is how half a screen ends up
# auto-coloured and the other half not.
from .render import console, err_console  # noqa: E402

BANNER = r"""
   _              _                          _
  /_\  _ _  _ _| |_ _ ___ _ __  ___ __| |__ _
 / _ \| ' \/ _` |  _| '_/ _ \ '  \/ -_) _` / _` |
/_/ \_\_||_\__,_|\__|_| \___/_|_|_\___\__,_\__,_|
"""


def banner(*, model: str, lane: str) -> None:
    console.print(Text(BANNER.strip("\n"), style="bold cyan"))
    console.print(f"  [dim]{lane} · {model}[/dim]")
    console.print("  [dim]/help for commands, /exit to leave[/dim]\n")


def agent_error(exc: AgentError) -> None:
    err_console.print(f"[bold red]✗[/bold red] {exc}")
    if exc.hint:
        err_console.print(f"  [dim]{exc.hint}[/dim]")


def fail(message: str, hint: str = "") -> None:
    err_console.print(f"[bold red]✗[/bold red] {message}")
    if hint:
        err_console.print(f"  [dim]{hint}[/dim]")


def ok(message: str) -> None:
    console.print(f"[bold green]✓[/bold green] {message}")


def info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")
