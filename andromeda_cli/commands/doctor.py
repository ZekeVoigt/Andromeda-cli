"""What is and is not working, in one screen."""

from __future__ import annotations

import platform
import shutil
import sys

from andromeda_tools import browser as browser_module
from andromeda_tools import skills as skills_module
from andromeda_tools import web

from .. import config as config_module
from .. import output
from .. import sessions as sessions_store
from ..commands import update as update_cmd


def _line(ok: bool, label: str, detail: str = "") -> None:
    mark = "[green]✓[/green]" if ok else "[yellow]·[/yellow]"
    output.console.print(f"  {mark} [cyan]{label.ljust(18)}[/cyan] [dim]{detail}[/dim]")


def run() -> int:
    from andromeda_cli import __version__

    output.console.print(f"\n  [bold]Andromeda CLI {__version__}[/bold]\n")

    _line(True, "python", f"{platform.python_version()} · {sys.executable}")
    _line(True, "platform", f"{platform.system()} {platform.machine()}")

    root = update_cmd.install_root()
    _line(root is not None, "checkout", str(root) if root else "not a git checkout")

    config = config_module.load()
    _line(True, "home", str(config_module.home()))
    _line(True, "provider", f"{config['provider']} · {config['model']}")
    _line(True, "approval", f"{config['approval_mode']} · ceiling {config['max_tier']}")

    from andromeda_agent.models import context_window, supports_reasoning

    reasons = supports_reasoning(config["model"])
    _line(
        reasons,
        "thinking",
        f"{config['thinking']}" if reasons else "not supported by this model",
    )
    _line(True, "context", f"{context_window(config['model']):,} tokens")

    # Reported even when it is not the configured surface: "why does --tui do
    # nothing" is exactly the question doctor exists to answer, and the answer
    # is usually an install that predates the dependency.
    import andromeda_tui

    tui_ready, tui_reason = andromeda_tui.available()
    _line(
        tui_ready,
        "interface",
        f"{config['interface']}" if tui_ready else f"{config['interface']} · {tui_reason}",
    )

    # Jobs and whether anything is going to run them, on two lines rather than
    # one. "3 jobs" reads as working, and it is exactly as true when nothing
    # has ticked for a week.
    from andromeda_agent.schedule import Schedule, heartbeat_age

    from .. import session as session_module
    from ..commands import cron as cron_cmd

    jobs = Schedule(session_module.schedule_path()).all()
    active = [job for job in jobs if job.state == "on"]
    _line(True, "jobs", f"{len(active)} scheduled · {len(jobs)} total")
    age = heartbeat_age(cron_cmd._heartbeat_path())
    if age is None:
        _line(
            not jobs,
            "scheduler",
            "never run — `andromeda cron install`" if jobs else "not running (no jobs)",
        )
    else:
        _line(age < cron_cmd.TICK_SECONDS * 4, "scheduler", f"ticked {cron_cmd._ago(age)}")

    credentials = config_module.load_credentials()
    _line(
        credentials.paired,
        "account",
        f"signed in · {credentials.user_id}"
        if credentials.paired
        else "not signed in — `andromeda auth login`",
    )

    key_name = str(config["direct_api_key_env"])
    import os

    _line(
        bool(os.environ.get(key_name, "").strip()),
        "byok key",
        f"${key_name} {'set' if os.environ.get(key_name) else 'not set'}",
    )

    provider = web.configured_provider()
    _line(provider is not None, "web search", provider or "no provider key set")

    playwright = browser_module.playwright_available()
    _line(playwright, "browser", "ready" if playwright else "andromeda browser install")

    found = skills_module.discover()
    unavailable = [s.name for s in found.values() if not s.available]
    detail = f"{len(found)} found"
    if unavailable:
        detail += f" · {len(unavailable)} need binaries: {', '.join(unavailable[:3])}"
    _line(bool(found), "skills", detail)

    sessions = sessions_store.recent(limit=1000)
    _line(True, "sessions", f"{len(sessions)} saved")

    for binary in ("git", "rg"):
        _line(bool(shutil.which(binary)), binary, shutil.which(binary) or "not installed")

    output.console.print()
    return 0
