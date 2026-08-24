"""First-run setup.

Four screens, one decision each, everything skippable. That shape is a
reaction to what the alternative looks like: a wizard that opens with a
forty-five item provider list and a twenty-nine item messaging list is not
onboarding, it is a form, and the person filling it in has no way to know which
answers matter. This build has a locked model and no messaging gateway, so the
only genuine decision is how it reaches a model — and even that has a default
that works.

Three rules, each of them a thing that goes wrong otherwise.

**Always say where you are.** `1 of 4` on every screen. Setup sends you to a
browser and back, and without a counter you return with no idea whether you are
nearly done or nearly at the start.

**Escape is always a real answer.** Every screen can be skipped and none of
them strand you: skipping leaves the default in place and setup carries on. A
prompt that cannot be dismissed teaches people to answer without reading.

**Nothing is written until the end.** Config is saved once, after the summary,
so a wizard abandoned halfway leaves the previous configuration exactly as it
was rather than half-rewritten.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from andromeda_agent import soul

from .. import config as config_module
from .. import output
from ..render import console, eyebrow

TOTAL_STEPS = 4


def interactive_input():
    """A reader bound to the terminal, not to stdin.

    The installer runs as `curl … | bash`, which makes the script's stdin the
    *pipe carrying the script itself*. A wizard that reads stdin there gets
    either EOF immediately or, worse, the remaining bytes of its own source as
    answers. Re-opening `/dev/tty` reaches the actual terminal regardless of
    what stdin was redirected to, and it is the reason the wizard can be
    launched from inside a piped installer at all.

    Returns None when there is no controlling terminal — cron, CI, Docker
    without `-t` — and every caller treats that as "do not prompt" rather than
    blocking forever on a read that will never return.
    """
    if sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r", encoding="utf-8")  # noqa: SIM115 - lifetime is the session
    except OSError:
        return None


@dataclass
class Step:
    number: int
    eyebrow_text: str
    heading: str
    body: str


def _header(step: Step) -> None:
    console.print()
    console.print(
        f"  [eyebrow]{eyebrow('andromeda / setup')}[/eyebrow]"
        f"[muted]{' ' * 6}{step.number} of {TOTAL_STEPS}[/muted]"
    )
    console.print()
    console.print(f"  [bold]{step.heading}[/bold]")
    if step.body:
        console.print()
        for line in step.body.strip().splitlines():
            console.print(f"  [muted]{line.strip()}[/muted]")
    console.print()


def _choose(reader, options: list[tuple[str, str, str]], default: int = 0) -> int | None:
    """A numbered chooser rather than an arrow-key list.

    Deliberately not a cursor UI. This runs inside a piped installer where the
    terminal is shared with a shell script, raw mode is not reliably available,
    and a broken cursor UI is unrecoverable — you cannot see what you are
    selecting. Numbers work on every terminal, over ssh, and in a transcript
    somebody pastes into a bug report.
    """
    for index, (label, detail, _value) in enumerate(options, start=1):
        marker = "●" if index - 1 == default else "○"
        console.print(f"    [accent]{marker}[/accent]  [bold]{index}[/bold]  {label}")
        if detail:
            console.print(f"           [muted]{detail}[/muted]")
    console.print()
    console.print("  [muted]number to choose · enter for the default · s to skip[/muted]")
    console.print()

    if reader is None:
        return default
    try:
        # Read from `reader`, never `input()`. `input()` reads `sys.stdin`, and
        # under `curl … | bash` that is the pipe carrying the installer script —
        # so the answer to the first question would be the next line of shell
        # source. The whole reason `interactive_input()` re-opens /dev/tty is to
        # get a handle on the actual terminal; calling `input()` throws that
        # away and silently reintroduces the bug it exists to prevent.
        console.file.write("  › ")
        console.file.flush()
        line = reader.readline()
        if not line:  # EOF
            return None
        raw = line.strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        return None
    if raw in {"s", "skip"}:
        return None
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    return default


def _step_provider(reader, settings: dict) -> None:
    _header(Step(1, "provider", "How it reaches a model.", """
        Andromeda runs on this machine, against your files, with your approval.
        It needs one way to reach a model.
    """))

    options = [
        ("Pair this machine", "Use your Andromeda account. Nothing else to set up.", "relay"),
        ("Bring your own key", "OpenRouter. You handle billing.", "direct"),
    ]
    current = 0 if settings.get("provider", "relay") == "relay" else 1
    choice = _choose(reader, options, default=current)
    if choice is None:
        return
    settings["provider"] = options[choice][2]

    if options[choice][2] == "relay":
        console.print()
        console.print("  [muted]After setup, run[/muted] [accent]andromeda auth login[/accent]")
        console.print("  [muted]Your code is in Settings → Paired machines.[/muted]")
    elif not os.environ.get("OPENROUTER_API_KEY"):
        console.print()
        console.print("  [muted]Set[/muted] [accent]OPENROUTER_API_KEY[/accent] [muted]before your first run.[/muted]")


def _step_approval(reader, settings: dict) -> None:
    _header(Step(2, "approval", "What it may do without asking.", """
        Andromeda reads and writes real files and runs real commands.
        This is the ceiling; you can change it any time.
    """))

    options = [
        ("Ask first", "Stops before anything that changes your machine. Recommended.", "ask"),
        ("Read only", "Never writes, never runs a command.", "deny"),
        ("Don't ask", "Runs everything without stopping. Know what this means.", "auto"),
    ]
    order = {"ask": 0, "deny": 1, "auto": 2}
    choice = _choose(reader, options, default=order.get(settings.get("approval_mode", "ask"), 0))
    if choice is None:
        return
    settings["approval_mode"] = options[choice][2]


def _step_soul(reader, settings: dict) -> None:
    home = config_module.home()
    created = soul.scaffold(home)

    _header(Step(3, "soul", "How it should work with you.", f"""
        {soul.FILENAME} holds your standing instructions — how you want
        Andromeda to work and how to talk to you. It is read every session
        and this program never writes to it.
    """))
    console.print(f"    [accent]{soul.path(home)}[/accent]")
    console.print()
    console.print(
        f"  [muted]{'Created with a starting template.' if created else 'Already there — left alone.'}"
        "  Edit it whenever.[/muted]"
    )
    console.print()


def _step_summary(reader, settings: dict, gaps: list[tuple[str, str, str]]) -> None:
    _header(Step(4, "ready", "What you have.", ""))
    for label, state, fix in gaps:
        if state:
            console.print(f"    [ok]✓[/ok]  {label}  [muted]{state}[/muted]")
        else:
            # Every gap names the exact thing that closes it. "Not configured"
            # tells someone they have a problem and leaves them to find the
            # fix; the command is the whole value of the line.
            console.print(f"    [muted]·[/muted]  {label}")
            console.print(f"           [muted]{fix}[/muted]")
    console.print()


def capability_report(settings: dict) -> list[tuple[str, str, str]]:
    """Each capability, its state, and the exact command that fills the gap."""
    from andromeda_tools import skills as skills_module

    credentials = config_module.load_credentials()
    provider = settings.get("provider", "relay")

    found = skills_module.resolve_skills_dir()
    skill_count = 0
    if found:
        skill_count = sum(1 for p in found.iterdir() if (p / "SKILL.md").exists())

    browser_ready = False
    try:
        import playwright  # noqa: F401

        browser_ready = True
    except ImportError:
        pass

    web_key = any(os.environ.get(name) for name in ("BRAVE_API_KEY", "TAVILY_API_KEY"))

    return [
        (
            "Model access",
            ("paired" if credentials.paired else "")
            if provider == "relay"
            else ("your own key" if os.environ.get("OPENROUTER_API_KEY") else ""),
            "andromeda auth login" if provider == "relay"
            else "export OPENROUTER_API_KEY=…",
        ),
        ("Files, search and shell", "ready", ""),
        ("Skills", f"{skill_count} available" if skill_count else "", "none found on this machine"),
        (soul.FILENAME, "ready", ""),
        ("Browser", "ready" if browser_ready else "", "andromeda browser install"),
        ("Web search", "ready" if web_key else "", "set BRAVE_API_KEY or TAVILY_API_KEY"),
    ]


def run(quick: bool = False) -> int:
    """The wizard. Returns a process exit code."""
    reader = interactive_input()
    settings = config_module.load()
    original = dict(settings)

    if reader is None and not quick:
        # No terminal to ask on. Say what to run rather than silently choosing
        # for someone, and do not fail the install that called us.
        output.info("No terminal available for setup.")
        output.info("Run `andromeda setup` when you have one.")
        return 0

    console.print()
    console.print(f"  [eyebrow]{eyebrow('andromeda')}[/eyebrow]")
    console.print()
    console.print("  [bold]Your work has gravity.[/bold]")
    console.print()
    console.print("  [muted]Four questions and you're working. Skip any of them.[/muted]")

    steps: list[Callable] = [_step_provider, _step_approval, _step_soul]
    for step in steps:
        step(reader, settings)

    # Written through `set_value` rather than dumped wholesale: it validates
    # each key, and a wizard is exactly where an invalid value would otherwise
    # be written confidently and fail on the next launch. Only changed keys are
    # touched, so skipping a step leaves the previous value alone.
    for key in ("provider", "approval_mode"):
        if key in settings and settings[key] != original.get(key):
            try:
                config_module.set_value(key, str(settings[key]))
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                output.fail(f"Could not save {key}: {exc}")

    _step_summary(reader, settings, capability_report(settings))

    console.print(f"  [muted]Start with[/muted] [accent]andromeda[/accent]")
    console.print(f"  [muted]Change any of this with[/muted] [accent]andromeda setup[/accent]")
    console.print()
    return 0
