"""Refusing the two config shapes that are never a real MCP server.

An MCP stdio entry is an arbitrary local command by design — that is the whole
point of the transport, and sandboxing it here would break every legitimate
custom server. So this does not try to judge what a server *does*. It refuses
two narrow shapes that have no honest reading, plus a hardcoded list of known
attacker artifacts.

**The exfiltration shape.** A shell interpreter whose inline script invokes
egress tooling. `command: bash`, `args: ["-c", "curl -X POST … < .env"]` is not
a server; it is a one-shot upload of your environment wearing a server's
clothes.

**The persistence shape.** A shell interpreter whose inline script writes to an
OS persistence surface — `authorized_keys`, `/etc/ssh`, PAM, sudoers, crontab,
a shell rc file. A campaign in June 2026 planted exactly this against several
agent harnesses: the payload appended an attacker SSH key, and the harness
re-executed the entry on every startup and every scheduled tick, reinstalling
the backdoor after each cleanup.

The check runs in **two** places, and it has to. At save time, so a config this
harness writes was screened. And at load time, so a config written by anything
else — a hand edit, a pre-planted file, a portable package — is screened before
it is ever spawned. Screening only at save would mean the file on disk was
trusted because of how it arrived, which is the assumption the campaign above
was built to exploit.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

SHELL_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "fish",
        "ksh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
)

_EGRESS = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
    r"|/dev/tcp/"
    r"|\bInvoke-WebRequest\b"
    r"|\bInvoke-RestMethod\b"
    r"|\bSystem\.Net\.WebClient\b",
    re.IGNORECASE,
)

# Egress alone is suspicious; egress *plus* one of these is the documented
# shape. Kept separate so the message can say which one it saw.
_EXFIL_HINT = re.compile(
    r"\.env\b|--data-binary|--data-raw|\b-X\s+POST\b|\bPOST\b|<\s*[^\s]+",
    re.IGNORECASE,
)

# Surfaces an MCP server has no reason to write to, ever.
_PERSISTENCE = re.compile(
    r"authorized_keys"
    r"|\.ssh/"
    r"|/etc/ssh\b"
    r"|/etc/pam\.d\b|pam_[\w-]+\.so"
    r"|/etc/sudoers"
    r"|/etc/cron|crontab\b"
    r"|/etc/rc\.local|/etc/systemd"
    r"|\.bashrc\b|\.bash_profile\b|\.profile\b|\.zshrc\b",
    re.IGNORECASE,
)

# Artifacts from the June 2026 campaign, hardcoded so a config planted by any
# route is refused on sight rather than only when its shape happens to match.
# The key is the attacker's; the addresses are where it authenticated from.
_INDICATORS = (
    "AAAAC3NzaC1lZDI1NTE5AAAAICBoh1oDC4DnsO1m5mJ4yfEKrQebaFh",
    "60.165.167.",
    "118.182.244.156",
    "61.178.123.196",
)


def _basename(command: Any) -> str:
    """The program being run, without its path or its arguments.

    `/usr/bin/env bash` and `bash` and `/bin/bash` are one thing for this
    purpose, and a check that only matched the bare word would be one `env`
    away from useless.
    """
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        parts = text.split()
    first = parts[0] if parts else text
    base = os.path.basename(first).lower()
    # `env VAR=x bash -c …` hides the interpreter one word in.
    if base == "env":
        for part in parts[1:]:
            if "=" in part.split("/")[-1] and not part.startswith("-"):
                continue
            if part.startswith("-"):
                continue
            return os.path.basename(part).lower()
    return base


def _script(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, (list, tuple)):
        return " ".join(str(item) for item in args)
    return str(args)


def _flatten(entry: dict[str, Any]) -> str:
    """Command, arguments and environment values as one string.

    Indicators are matched against all three: an attacker key delivered through
    `env` is the same key.
    """
    parts: list[str] = [str(entry.get("command") or ""), _script(entry.get("args"))]
    env = entry.get("env")
    if isinstance(env, dict):
        parts.extend(str(value) for value in env.values())
    parts.append(str(entry.get("url") or ""))
    return " ".join(parts)


def screen(name: str, entry: dict[str, Any]) -> list[str]:
    """What is wrong with this server entry. Empty means nothing is.

    Deliberately not a whitelist. `npx`, `uvx`, `python`, a compiled binary, a
    path into a checkout — all fine, all common, none of them examined. Only
    the shapes above are refused, and each refusal says which one it is so the
    person can see that the rule is narrow.
    """
    if not isinstance(entry, dict):
        return []

    flat = _flatten(entry)
    for indicator in _INDICATORS:
        if indicator in flat:
            # One is enough. The full list is not echoed back — a refusal that
            # prints every pattern it knows is a refusal that teaches evasion.
            return [
                f"`{name}` carries a known indicator of compromise "
                f"(`{indicator[:24]}…`) from the June 2026 agent-harness campaign"
            ]

    command = entry.get("command")
    if _basename(command) not in SHELL_INTERPRETERS:
        return []

    script = _script(entry.get("args"))
    if not script:
        return []

    issues: list[str] = []
    if _EGRESS.search(script):
        issue = f"`{name}` runs `{command}` with network egress in its arguments"
        if _EXFIL_HINT.search(script):
            issue += ", shaped like an upload rather than a fetch"
        issues.append(issue)

    if _PERSISTENCE.search(script):
        issues.append(
            f"`{name}` runs `{command}` to write to an OS persistence surface "
            f"(SSH keys, PAM, sudoers, cron or a shell rc file). That is a "
            f"backdoor being reinstalled on every start, not a server."
        )

    return issues


def suspicious(name: str, entry: dict[str, Any]) -> bool:
    return bool(screen(name, entry))
