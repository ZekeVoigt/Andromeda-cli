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

def banner(*, model: str, lane: str, extra: str = "", animate: bool = True) -> None:
    """The first thing anyone sees.

    The study replaces the ASCII wordmark it used to print. A wordmark says the
    name, which is already in the command they just typed; the study says what
    the product is. It is the landing page's `ProportionStudy`, and carrying it
    into the terminal is the cheapest way for the two surfaces to feel like one
    product.

    Degrades all the way down: animated on a capable tty, static where live
    redraws are not available, and a single plain line where the terminal
    cannot encode braille or the output is redirected. A banner must never be
    the reason a session fails to start.
    """
    from . import art
    from .render import eyebrow

    drew = False
    if art.supported():
        console.print()
        if animate:
            art.scan(console, width=console.width)
        else:
            for text, style in art.study(console.width):
                console.print(text, style=style or None)
        drew = bool(art.figure(console.width))

    if not drew:
        console.print()
        console.print(f"  [eyebrow]{eyebrow('Andromeda')}[/eyebrow]")

    console.print()
    console.print(f"  [eyebrow]{eyebrow('the personal agent')}[/eyebrow]")
    console.print()
    line = f"  [muted]{lane} · {model}[/muted]"
    if extra:
        line += f"  [muted]·[/muted]  [muted]{extra}[/muted]"
    console.print(line)
    console.print("  [muted]/help for commands, /exit to leave[/muted]\n")


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
