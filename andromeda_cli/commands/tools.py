"""Inspecting and switching tools."""

from __future__ import annotations

from andromeda_agent import Policy
from andromeda_agent.delegation import make_delegate_tool, make_lane_tools
from andromeda_agent.lanes import LaneRegistry
from andromeda_agent.schedule import Schedule
from andromeda_tools import BrowserSession, MemoryStore, Workspace, build_registry
from andromeda_tools import browser as browser_module
from andromeda_tools import mcp as mcp_module
from andromeda_tools.processes import ProcessRegistry
from andromeda_tools import skills as skills_module
from andromeda_tools.todo import TodoList

from .. import config as config_module
from .. import output
from .. import session as session_module


def _registry():
    """Built with the same bindings a real session gets.

    Without the memory store and the skills, this listing quietly omits four
    tools the agent actually has — and a `tools` command that disagrees with
    the session is worse than none.
    """
    workspace = Workspace()

    def unused(**_: object):
        raise AssertionError("the listing never runs a lane")

    return build_registry(
        workspace,
        TodoList(),
        skills_module.discover(workspace.root),
        MemoryStore(config_module.home() / "memory"),
        delegate=make_delegate_tool(unused),
        lane_tools=make_lane_tools(LaneRegistry()),
        browser=browser_module.build_session(),
        processes=ProcessRegistry(),
        mcp_servers=_connected_mcp(),
        # A placeholder, so the listing shows the tool the session will have.
        # It is never called from here.
        vision=object(),
        # The real schedule, not a placeholder: reading it is harmless and the
        # listing should describe the tool the session actually gets. This test
        # — `test_the_tools_listing_matches_a_real_session` — is what catches a
        # binding added to one and not the other.
        schedule=Schedule(session_module.schedule_path()),
        # An interactive session gets `connect_app`, so the listing has to show
        # it. A lane does not, which is why this is a parameter rather than
        # something the registry decides for itself.
        connect_home=config_module.home(),
    )


def _connected_mcp():
    servers = mcp_module.build_servers(config_module.home())
    for server in servers:
        server.connect()
    return servers


def show() -> int:
    config = config_module.load()
    registry = _registry()
    policy = Policy(
        mode=config["approval_mode"],
        enabled=frozenset(config["enabled_tools"]),
        max_tier=config["max_tier"],
    )

    width = max(len(name) for name in registry)
    for name, spec in sorted(registry.items()):
        decision = policy.decide(spec)
        mark, note = {
            "allowed": ("[green]on[/green] ", "runs without asking"),
            "needs_approval": ("[green]on[/green] ", "asks first"),
            "denied": ("[dim]off[/dim]", "off"),
        }[decision]
        output.console.print(
            f"  {mark}  [cyan]{name.ljust(width)}[/cyan]  "
            f"[dim]{spec.risk_tier.ljust(12)} {note}[/dim]"
        )

    output.info(f"\n  approval: {policy.mode} · ceiling: {policy.max_tier}")
    output.info("  andromeda tools enable|disable <name>")
    return 0


def _write(names: list[str]) -> None:
    config_module.set_value("enabled_tools", ",".join(sorted(names)))


def enable(name: str) -> int:
    registry = _registry()
    if name not in registry:
        output.fail(f"No tool named {name!r}.", f"Known: {', '.join(sorted(registry))}")
        return 2

    enabled = set(config_module.load()["enabled_tools"])
    if name in enabled:
        output.info(f"{name} is already enabled.")
        return 0
    enabled.add(name)
    _write(sorted(enabled))
    output.ok(f"{name} enabled.")
    return 0


def disable(name: str) -> int:
    registry = _registry()
    if name not in registry:
        output.fail(f"No tool named {name!r}.", f"Known: {', '.join(sorted(registry))}")
        return 2

    enabled = set(config_module.load()["enabled_tools"])
    if name not in enabled:
        output.info(f"{name} is already disabled.")
        return 0
    enabled.discard(name)
    _write(sorted(enabled))
    output.ok(f"{name} disabled.")
    return 0
