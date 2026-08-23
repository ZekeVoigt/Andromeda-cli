"""Making the scheduler survive a logout.

`andromeda cron daemon` runs in a terminal, which is fine for watching it and
useless for the thing it exists to do. This writes the supervisor's file and
hands the process over.

The daemon does not fork, and this is why: `launchd` and `systemd` both want to
own the process they supervise, and a program that daemonises itself underneath
them is a program with two ideas about whether it is running — the supervisor
restarts a process that already exited, or gives up on one that is still alive.
The foreground loop is the correct shape *because* something else supervises it.

Two things worth knowing before editing:

- **The interpreter is `sys.executable`, and the path is absolute.** A user
  agent starts with almost none of a login shell's environment: no `PATH` from
  `.zshrc`, no `PYENV_ROOT`, nothing a `uv` shim resolves through. `andromeda`
  on `PATH` works in a terminal and is not there at boot.
- **`KeepAlive` / `Restart=always`, with a throttle.** A scheduler that exits
  on an unhandled error and never comes back is worse than one that was never
  installed, because the jobs still look scheduled.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .. import config as config_module
from .. import output

LABEL = "com.andromeda.cli.cron"
UNIT = "andromeda-cron"

# Long enough that a crash loop cannot spin, short enough that a transient
# failure heals without anyone noticing.
THROTTLE_SECONDS = 30

LAUNCHD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>andromeda_cli</string>
    <string>cron</string>
    <string>daemon</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>EnvironmentVariables</key>
  <dict>
{environment}  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>{throttle}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

SYSTEMD_TEMPLATE = """[Unit]
Description=Andromeda CLI scheduler
After=network-online.target

[Service]
Type=simple
ExecStart={python} -m andromeda_cli cron daemon
WorkingDirectory={cwd}
{environment}Restart=always
RestartSec={throttle}
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""

# Passed through to the service because the daemon cannot ask for them. A BYOK
# install has no other way to reach a provider at boot, and `ANDROMEDA_HOME`
# decides which install's jobs run at all. Nothing else is forwarded — a
# service file is world-readable on most systems, so this list is deliberately
# short and deliberately not "the environment".
#
# `PATH` is the one that is not about Andromeda at all, and it is here because
# leaving it out breaks the commonest thing a job does. A user agent starts with
# a minimal `PATH` — on macOS roughly `/usr/bin:/bin:/usr/sbin:/sbin` — so a
# script calling `gh`, `rg`, `node` or anything else installed by Homebrew or a
# version manager works when you run the job by hand and fails the moment the
# scheduler runs it. That is the worst shape of bug available: it works while
# you are watching.
FORWARDED = (
    "PATH",
    "LANG",
    "ANDROMEDA_HOME",
    "ANDROMEDA_PROVIDER",
    "OPENROUTER_API_KEY",
)


def _systemd_value(value: str) -> str:
    """Quote for a systemd `Environment="K=V"` line.

    A `PATH` entry with a quote or a backslash in it is unusual and not
    impossible, and an unescaped one produces a unit file systemd refuses —
    after the install command has already reported success.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _log_path() -> Path:
    return config_module.home() / "cron" / "scheduler.log"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{UNIT}.service"


def _environment() -> dict[str, str]:
    """A snapshot, taken now, of the few variables a job needs.

    A snapshot and not a reference: the service file holds literal values, so
    `PATH` is whatever the shell that ran `cron install` had. Install from a
    normal login shell, and re-run `cron install` after changing your `PATH` —
    the service will not pick it up on its own. `service_paths()` is what
    `andromeda cron service` prints so the difference is visible rather than
    mysterious.
    """
    return {
        key: os.environ[key] for key in FORWARDED if os.environ.get(key, "").strip()
    }


def _report_path(environment: dict[str, str]) -> None:
    """Say what the service will be able to find, and when it will not.

    Checked rather than assumed: the failure this prevents is a job that runs
    fine by hand and cannot find `gh` at 6am, which looks like the job being
    broken rather than the environment being different.
    """
    captured = environment.get("PATH", "")
    if not captured:
        output.console.print(
            "  [yellow]No PATH was captured — the service will only find "
            "binaries in the system default locations.[/yellow]"
        )
        return

    missing = [name for name in ("git", "gh") if not shutil.which(name)]
    output.info(f"  PATH captured from this shell ({len(captured.split(':'))} entries)")
    if missing:
        output.info(
            f"  {', '.join(missing)} not on this PATH — a job that calls "
            "them will not find them either."
        )


def _warn_about_secrets(environment: dict[str, str]) -> None:
    secrets = [key for key in environment if key.endswith("_API_KEY")]
    if secrets:
        # Said plainly rather than silently done. Writing a key into a file to
        # make a background service work is a reasonable trade and it is still
        # a copy of a credential in a new place, which the person should know
        # about before it exists rather than discover later.
        output.console.print(
            f"  [yellow]{', '.join(secrets)} is written into the service file so "
            "the scheduler can reach the model at boot.[/yellow]"
        )
        output.info("  Use the hosted lane (`andromeda auth login`) to avoid that.")


def install() -> int:
    system = platform.system()
    if system == "Darwin":
        return _install_launchd()
    if system == "Linux":
        return _install_systemd()
    output.fail(
        f"No service integration for {system}.",
        "Run `andromeda cron daemon` in a terminal, or use your own supervisor.",
    )
    return 2


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # 0600: it may hold a provider key, and a LaunchAgents directory is not
    # private by default.
    path.chmod(0o600)


def _install_launchd() -> int:
    environment = _environment()
    body = LAUNCHD_TEMPLATE.format(
        label=LABEL,
        python=sys.executable,
        cwd=str(Path.home()),
        throttle=THROTTLE_SECONDS,
        log=_log_path(),
        environment="".join(
            f"    <key>{key}</key><string>{escape(value)}</string>\n"
            for key, value in environment.items()
        ),
    )
    path = _plist_path()
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    _write(path, body)

    # `bootout` first, ignoring failure: reinstalling over a loaded agent
    # silently keeps the old one running with the old arguments, which is how
    # someone spends an hour wondering why their edit did nothing.
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    loaded = subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True
    )
    if loaded.returncode != 0:
        output.fail(
            "launchctl refused to load the agent.",
            (loaded.stderr or "").strip()[:200] or f"Try: launchctl bootstrap {domain} {path}",
        )
        return 1

    output.ok("Scheduler installed. It starts at login and restarts if it dies.")
    output.info(f"  {path}")
    output.info(f"  log  {_log_path()}")
    _report_path(environment)
    _warn_about_secrets(environment)
    return 0


def _install_systemd() -> int:
    if not shutil.which("systemctl"):
        output.fail(
            "systemctl is not available.",
            "Run `andromeda cron daemon` under whatever supervises this machine.",
        )
        return 2

    environment = _environment()
    body = SYSTEMD_TEMPLATE.format(
        python=sys.executable,
        cwd=str(Path.home()),
        throttle=THROTTLE_SECONDS,
        log=_log_path(),
        environment="".join(
            f'Environment="{key}={_systemd_value(value)}"\n'
            for key, value in environment.items()
        ),
    )
    path = _unit_path()
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    _write(path, body)

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    enabled = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{UNIT}.service"],
        capture_output=True,
        text=True,
    )
    if enabled.returncode != 0:
        output.fail(
            "systemctl refused to enable the unit.",
            (enabled.stderr or "").strip()[:200],
        )
        return 1

    output.ok("Scheduler installed and started.")
    output.info(f"  {path}")
    output.info(f"  log  {_log_path()}")
    # Without lingering, a user unit stops at logout — which for a scheduler is
    # the difference between "runs overnight" and "runs while I am typing".
    output.info(f"  loginctl enable-linger {os.environ.get('USER', '$USER')}  # to survive logout")
    _report_path(environment)
    _warn_about_secrets(environment)
    return 0


def uninstall() -> int:
    system = platform.system()
    removed = False

    if system == "Darwin":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True
        )
        path = _plist_path()
        if path.exists():
            path.unlink()
            removed = True
    elif system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{UNIT}.service"],
            capture_output=True,
        )
        path = _unit_path()
        if path.exists():
            path.unlink()
            removed = True
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    else:
        output.fail(f"No service integration for {system}.")
        return 2

    # The jobs are left alone, deliberately. Uninstalling the supervisor is
    # "stop running these for now", not "delete my automations" — and deleting
    # someone's jobs as a side effect of a command about a service file would
    # be the worst kind of surprise.
    output.ok("Scheduler service removed." if removed else "No scheduler service installed.")
    output.info("  Your jobs are untouched — `andromeda cron list`.")
    return 0


def status() -> int:
    system = platform.system()
    path = _plist_path() if system == "Darwin" else _unit_path()
    output.info(f"  service   {path}")
    output.info(f"  installed {'yes' if path.exists() else 'no'}")
    output.info(f"  log       {_log_path()}")

    if path.exists():
        stored = _stored_path(path)
        if stored is not None:
            live = os.environ.get("PATH", "")
            output.info(f"  PATH      {len(stored.split(':'))} entries")
            if stored != live:
                # Not an error — the service was installed from a different
                # shell, which is normal. Worth saying, because "my job cannot
                # find a tool I just installed" is answered by re-running
                # `cron install` and by nothing else.
                output.info("            differs from this shell · re-run `cron install` to refresh")

    if system == "Darwin" and path.exists():
        listed = subprocess.run(
            ["launchctl", "list", LABEL], capture_output=True, text=True
        )
        output.info(f"  loaded    {'yes' if listed.returncode == 0 else 'no'}")
    elif system == "Linux" and path.exists():
        active = subprocess.run(
            ["systemctl", "--user", "is-active", f"{UNIT}.service"],
            capture_output=True,
            text=True,
        )
        output.info(f"  active    {(active.stdout or '').strip() or 'unknown'}")
    return 0


def _stored_path(path: Path) -> str | None:
    """The PATH baked into the installed service file, or None.

    Read back out rather than remembered, because the file is the truth: it may
    have been written by a different shell, on a different day, before the tool
    somebody is now wondering about was installed.
    """
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix == ".plist":
        match = re.search(
            r"<key>PATH</key>\s*<string>([^<]*)</string>", body
        )
        return match.group(1) if match else None
    match = re.search(r'^Environment="PATH=([^"]*)"', body, re.MULTILINE)
    return match.group(1) if match else None
