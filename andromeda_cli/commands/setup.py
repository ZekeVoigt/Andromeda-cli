"""First-run setup.

Four screens, one decision each, everything skippable. That shape is a
reaction to what the alternative looks like: a wizard that opens with a
forty-five item provider list and a twenty-nine item messaging list is not
onboarding, it is a form, and the person filling it in has no way to know which
answers matter.

Four rules, each of them a thing that goes wrong otherwise.

**Sign in first.** The first screen is the account, before any preference, and
it happens in a browser rather than by copying a code between windows. It is
first because it is the step that decides what the rest of the product can do:
signed in, the model is reachable and a plan can be upgraded when the free one
runs out; not signed in, every later question is being asked of something that
cannot answer yet. Setup is also the only moment we can be sure the person is
sitting in front of both a terminal and a browser.

**One question on the screen at a time.** Each step clears the last. A wizard
that scrolls leaves question one visible while you are answering question two,
and two things on screen both look like the thing being asked.

**Escape is always a real answer.** Every screen can be skipped and none of
them strand you: skipping leaves the default in place and setup carries on. A
prompt that cannot be dismissed teaches people to answer without reading.

**Nothing is written until the end.** Config is saved once, after the summary,
so a wizard abandoned halfway leaves the previous configuration exactly as it
was rather than half-rewritten. Credentials are the exception and have to be:
they are written the moment the browser hands them back, because that is the
only moment they exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from andromeda_agent import soul

from .. import config as config_module
from .. import output
from .. import prompt as prompt_module
from ..prompt import interactive_input  # re-exported: tests and callers patch it here
from ..render import console, eyebrow

TOTAL_STEPS = 4


@dataclass
class Step:
    number: int
    eyebrow_text: str
    heading: str
    body: str


def _header(reader, step: Step) -> None:
    prompt_module.clear(reader)
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
    return prompt_module.choose(reader, options, default=default)


def _sign_in(reader, settings: dict) -> None:
    """Open the browser, wait for the redirect, and say what came back.

    Deliberately blocking. The alternative — launch the browser and carry on
    asking questions — puts a wizard and a sign-in on screen at the same time,
    and whichever one finishes second overwrites the other's output.
    """
    from . import auth as auth_cmd

    base_url = str(settings.get("base_url") or config_module.DEFAULTS["base_url"])

    def announce(url: str, opened: bool) -> None:
        console.print()
        if opened:
            console.print("  [muted]Your browser is open. Sign in or create an account there.[/muted]")
        else:
            console.print("  [muted]Open this to sign in or create an account:[/muted]")
        console.print(f"    [accent]{url}[/accent]")
        console.print()
        console.print("  [muted]Waiting… this window continues by itself.[/muted]")

    result = auth_cmd.browser_login(base_url=base_url, announce=announce)
    console.print()
    if result.ok:
        console.print("  [ok]✓[/ok]  [bold]Signed in.[/bold] [muted]This machine is paired.[/muted]")
        return

    console.print(f"  [warn]·[/warn]  [muted]{result.error}[/muted]")
    console.print("  [muted]Setup carries on. Finish this later with[/muted] [accent]andromeda auth login[/accent]")


def _step_account(reader, settings: dict) -> None:
    _header(reader, Step(1, "account", "Sign in to Andromeda.", """
        Andromeda runs on this machine, against your files, with your approval.
        It needs one way to reach a model.

        Signing in is free, and it is also where you upgrade later if you want
        more than the free plan gives you.
    """))

    already = config_module.load_credentials().paired
    if already:
        # Re-running setup must not sign a working machine out of its own
        # account just to get to the next question, so staying is the default
        # and signing in again is the deliberate second option.
        console.print("  [ok]✓[/ok]  [muted]This machine is already signed in.[/muted]")
        console.print()
        options = [
            ("Stay signed in", "Keep this machine on the account it already uses.", "keep"),
            ("Sign in as someone else", "Opens your browser and replaces the account.", "relay"),
            ("Bring your own key", "OpenRouter. You handle billing, no account needed.", "direct"),
        ]
        current = 0
    else:
        options = [
            ("Sign in with your browser", "Free account, nothing to copy or paste. Recommended.", "relay"),
            ("Bring your own key", "OpenRouter. You handle billing, no account needed.", "direct"),
        ]
        current = 0 if settings.get("provider", "relay") == "relay" else 1

    choice = _choose(reader, options, default=current)
    if choice is None:
        return
    chosen = options[choice][2]
    settings["provider"] = "relay" if chosen == "keep" else chosen

    if chosen == "relay":
        _sign_in(reader, settings)
    elif chosen == "direct" and not os.environ.get("OPENROUTER_API_KEY"):
        console.print()
        console.print("  [muted]Set[/muted] [accent]OPENROUTER_API_KEY[/accent] [muted]before your first run.[/muted]")


def _step_approval(reader, settings: dict) -> None:
    _header(reader, Step(2, "approval", "What it may do without asking.", """
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

    _header(reader, Step(3, "soul", "How it should work with you.", f"""
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
    prompt_module.wait_for_enter(reader)


def _step_summary(reader, settings: dict, gaps: list[tuple[str, str, str]]) -> None:
    _header(reader, Step(4, "ready", "What you have.", ""))
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
            ("signed in" if credentials.paired else "")
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

    prompt_module.clear(reader)
    console.print()
    console.print(f"  [eyebrow]{eyebrow('andromeda')}[/eyebrow]")
    console.print()
    console.print("  [bold]Your work has gravity.[/bold]")
    console.print()
    console.print("  [muted]Four questions and you're working. Skip any of them.[/muted]")
    console.print()
    prompt_module.wait_for_enter(reader)

    steps: list[Callable] = [_step_account, _step_approval, _step_soul]
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
