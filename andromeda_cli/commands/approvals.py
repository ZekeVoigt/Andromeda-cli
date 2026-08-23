"""Inspecting and clearing learned approvals."""

from __future__ import annotations

import time

from andromeda_agent.allowlist import Allowlist

from .. import config as config_module
from .. import output


def _allowlist() -> Allowlist:
    return Allowlist(config_module.home() / "approvals.json")


def show() -> int:
    allowlist = _allowlist()
    entries = allowlist.all()
    if not entries:
        output.info("Nothing learned yet.")
        output.info("  Answer ! at an approval prompt to stop being asked about a tool.")
        return 0

    width = max(len(entry.tool_name) for entry in entries)
    for entry in entries:
        verb = "allow" if entry.trust_level == "always_allow" else "deny"
        style = "green" if verb == "allow" else "red"
        age = time.time() - (entry.last_approved_at or entry.updated_at)
        output.console.print(
            f"  [{style}]{verb.ljust(5)}[/{style}]  [cyan]{entry.tool_name.ljust(width)}[/cyan]"
            f"  [dim]{entry.risk_tier}  ·  {entry.approval_count} approvals"
            f"  ·  {int(age / 86400)}d ago[/dim]"
        )
    output.info(f"\n  andromeda approvals forget <tool> · andromeda approvals clear")
    return 0


def forget(tool: str) -> int:
    if _allowlist().forget(tool):
        output.ok(f"{tool} will be asked about again.")
        return 0
    output.info(f"Nothing learned about {tool!r}.")
    return 1


def clear() -> int:
    count = _allowlist().clear()
    output.ok(f"Cleared {count} learned approval(s).")
    return 0
