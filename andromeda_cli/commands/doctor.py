"""What is and is not working, in one screen.

`--cloud` asks a different question: not "is this install healthy" but **"could
this container actually do the job".** They are not the same check and the
difference is the whole point of having it. A hosted runner is booted by a
request, does its work, and stops — nobody is at a terminal to notice that
`rg` is missing or that the volume is full, and the symptom of either is a job
that fails identically forever until the failure auto-pause catches it five
runs later.

So the cloud checks are the ones that can only be answered *inside* the
container, and every one of them is a real failure somebody has had:

  binaries      a job shells out to `git` or `rg`, it works on your Mac, and it
                fails the moment the runner tries — invisible exactly while you
                are looking at it
  ANDROMEDA_HOME  pointing at the image layer instead of the mounted volume
                means every monitor baseline and notepad is discarded on the
                next boot, so a watcher re-reports what it already reported,
                forever, and costs a model turn each time
  writability   a read-only mount fails at the first save, which is after the
                model call
  free space    a partial write to `state.db` is worse than a skipped run
  no model key  a container holding a provider key is a key on hardware the
                user does not control
"""

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


def run(cloud: bool = False) -> int:
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

    # A paused install is the most confusing thing this program can be if it
    # does not say so: jobs stop firing and everything else looks healthy.
    from andromeda_agent import pause as pause_module

    held = pause_module.describe(config_module.home())
    if held:
        _line(False, "paused", held)

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

    # The index and the memory backend, because both fail quietly: a search
    # that finds nothing and a recall that returns nothing look exactly like
    # "there was nothing there".
    from .. import profiles
    from .. import state

    capability = state.capabilities()
    if capability["error"]:
        _line(False, "index", f"unreadable — {capability['error']}")
    else:
        counted = state.counts()
        stale = state.stale_count()
        search_route = "fts5" if capability["fts5"] else "substring scan"
        if capability["trigram"]:
            search_route += " + trigram"
        _line(
            stale == 0,
            "index",
            f"{counted['sessions']} sessions · {counted['messages']} messages · "
            + (search_route if stale == 0 else f"{stale} stale — `andromeda sessions reindex`"),
        )

    from andromeda_tools import MemoryStore

    memory = MemoryStore(config_module.home() / "memory", config["memory_backend"])
    _line(
        not memory.backend_note,
        "memory",
        f"{len(memory.load())} stored · {memory.backend.name}"
        + (f" · {memory.backend_note}" if memory.backend_note else ""),
    )

    # Plugins, because a broken one is the only thing on this screen that is
    # third-party code running in this process. A plugin that fails to load is
    # reported at startup and then scrolls away; this is where it stays.
    from andromeda_agent import plugin_store, plugins as plugins_module

    if plugins_module.plugins_disabled():
        _line(True, "plugins", f"off — {plugins_module.ENV_DISABLE} is set")
    else:
        discovered = plugins_module.discover()
        enabled = {key for key in discovered if plugin_store.is_enabled(key)}
        broken = sorted(
            plugin_id
            for plugin_id, entry in plugins_module.manager().loaded.items()
            if entry.error
        )
        if discovered:
            detail = f"{len(enabled)} enabled of {len(discovered)}"
            if broken:
                detail += f" · {', '.join(broken)} failed to load"
            _line(not broken, "plugins", detail)

    named = [item for item in profiles.listing() if not item.is_default]
    if named:
        _line(
            True,
            "profiles",
            f"{profiles.selected()} · {len(named)} other(s) on this machine",
        )

    for binary in ("git", "rg"):
        _line(bool(shutil.which(binary)), binary, shutil.which(binary) or "not installed")

    # Not a failure — everything works without it. But a scheduled job's
    # notification can only be *clicked into* its conversation when this is
    # present: macOS's own `display notification` cannot carry an action at
    # all, so without it the notification is read-only and the session id has
    # to be typed. Worth one line, since nobody would think to look for it.
    if sys.platform == "darwin":
        notifier = shutil.which("terminal-notifier")
        _line(
            bool(notifier),
            "notifications",
            notifier or "clickable job notifications need `brew install terminal-notifier`",
        )

    # Which MCP servers a hosted job can reach. Silence here was the whole
    # failure: a cloud job reached none of them and reported it as having no
    # tools, which is indistinguishable from the server being broken.
    try:
        from andromeda_agent import mcp_cloud
        from andromeda_tools import mcp_config

        local = mcp_config.servers(config_module.home())
        if local:
            travels = [n for n, c in local.items() if not mcp_cloud.travellable(n, c)]
            _line(
                True,
                "mcp → cloud",
                f"{len(travels)} of {len(local)} can travel"
                + ("" if not travels else " · `andromeda mcp push` to send them"),
            )
    except Exception:  # noqa: BLE001 - a diagnostic must not be the thing that breaks
        pass

    failures = _cloud_checks(config) if cloud else 0

    output.console.print()
    # An exit code, unlike every other check here. `doctor` is read by a person;
    # `doctor --cloud` is read by a container's healthcheck, and a healthcheck
    # that always exits 0 is a healthcheck that never fails.
    return 1 if failures else 0


def _cloud_checks(config: dict) -> int:
    """Everything that can only be answered from inside the runner.

    Returns how many failed, so the caller can turn it into an exit code.
    """
    import os
    import shutil as shutil_module

    output.console.print("\n  [bold]as a hosted runner[/bold]\n")
    failures = 0

    def check(ok: bool, label: str, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        _line(ok, label, detail)

    # The binaries a tool shells out to. Named explicitly rather than probed
    # from the registry: the point is to fail the build when the image and the
    # toolbelt drift apart, and a list derived from the toolbelt would drift
    # with it silently.
    for binary in ("git", "rg"):
        found = shutil_module.which(binary)
        check(bool(found), f"{binary} present", found or "MISSING — jobs using it will fail")

    # Where the durable disk is mounted. `/data` is what the image declares;
    # a deployment that mounts elsewhere says so here rather than failing a
    # check it cannot pass. It is the operator's statement of fact, not a
    # setting the agent or a job can reach.
    volume = os.environ.get("ANDROMEDA_CLOUD_VOLUME", "/data")
    home = config_module.home()
    on_volume = str(home) == volume or str(home).startswith(volume.rstrip("/") + "/")
    check(
        on_volume,
        "home on volume",
        f"{home}"
        + (
            ""
            if on_volume
            else f" — not under {volume}; state is lost on every boot"
        ),
    )

    writable = os.access(home, os.W_OK)
    check(writable, "home writable", "yes" if writable else "NO — the first save will fail")

    try:
        usage = shutil_module.disk_usage(home)
        free_mb = usage.free // (1024 * 1024)
    except OSError:
        free_mb = 0
    # 256MB is not a tuned number, it is a floor: `state.db`, a session
    # transcript and a run's output together are well under it, and anything
    # below it means the next write is the one that half-lands.
    check(free_mb >= 256, "free space", f"{free_mb} MB" + ("" if free_mb >= 256 else " — too low to write safely"))

    # A runner must reach the model through the relay, on the account's credit,
    # with billing authority server-side. A provider key in the environment is
    # not a convenience here; it is a credential on hardware the user does not
    # hold.
    leaked = [
        name
        for name in os.environ
        if name.endswith("_API_KEY") or name in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    ]
    check(
        not leaked,
        "no model key",
        "none in the environment" if not leaked else f"FOUND {', '.join(sorted(leaked))}",
    )

    # Not a failure, and deliberately so. Skills are user content and live on
    # the volume (`<home>/skills`), not in the image — a runner is *supposed*
    # to start with none. It is reported because the failure it precedes is
    # confusing: a job that names a skill the runner does not have fails on a
    # missing file, which reads as a broken job rather than as un-synced
    # content.
    from andromeda_tools import skills as skills_mod

    found = skills_mod.discover()
    _line(
        True,
        "skills",
        f"{len(found)} on the volume ({home}/skills)",
    )

    relay = config.get("provider") == "relay"
    check(relay, "provider", f"{config.get('provider')}" + ("" if relay else " — a runner must use the relay lane"))

    return failures
