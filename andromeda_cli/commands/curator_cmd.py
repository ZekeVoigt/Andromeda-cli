"""`andromeda curator` — the state of the skill library, and tidying it.

`status` is the one to read: what is tracked, what is going quiet, what has
been put away. `sweep` is arithmetic and safe. `review` costs a model call and
proposes changes to what skills *say*, which a person then applies — nothing
here rewrites a skill on its own.
"""

from __future__ import annotations

from pathlib import Path

from andromeda_agent import curator as curator_module
from andromeda_tools import skill_usage
from andromeda_tools import skills as skills_module

from .. import config as config_module
from .. import output

STATE_COLOUR = {
    skill_usage.ACTIVE: "green",
    skill_usage.STALE: "yellow",
    skill_usage.ARCHIVED: "dim",
}


def _discover(workspace: str = "") -> dict[str, skills_module.Skill]:
    return skills_module.discover(Path(workspace) if workspace else None)


def _settings() -> curator_module.Settings:
    return curator_module.Settings.from_config(config_module.load())


def status(workspace: str = "") -> int:
    home = config_module.home()
    settings = _settings()
    rows = skill_usage.report(_discover(workspace), home, include_uncurated=True)

    if not rows:
        output.info("  no skills found")
        return 0

    curatable = [row for row in rows if row["curatable"]]
    others = [row for row in rows if not row["curatable"]]

    state = curator_module.load_state(home)
    when = state.get("last_run_at") or "never"
    output.info(f"  last sweep: {when}")
    if state.get("last_summary"):
        output.info(f"  {state['last_summary']}")
    if curator_module.is_paused(home):
        output.info("  paused — andromeda curator resume")
    elif not settings.enabled:
        output.info("  off — set `curator: true` to turn it on")
    output.console.print()

    if curatable:
        output.info(f"  {len(curatable)} skill(s) the agent wrote\n")
        for row in curatable:
            _line(row)
    else:
        output.info("  no agent-written skills yet — nothing to curate")

    if others:
        output.console.print()
        output.info(f"  {len(others)} skill(s) that are yours, and left alone")

    archived = skill_usage.archived_names(home)
    if archived:
        output.console.print()
        output.info(f"  {len(archived)} archived · andromeda curator restore <name>")
        for name in archived:
            output.console.print(f"      [dim]{name}[/dim]")

    return 0


def _line(row: dict) -> None:
    tint = STATE_COLOUR.get(row["state"], "dim")
    pin = " [cyan]pinned[/cyan]" if row["pinned"] else ""
    used = f"{row['uses']} use(s)" if row["uses"] else "never used"
    output.console.print(
        f"  [{tint}]{row['state'].ljust(8)}[/{tint}] [cyan]{row['name']}[/cyan]{pin}"
    )
    output.console.print(
        f"      [dim]{used} · idle {row['idle_days']:.0f} day(s)[/dim]"
    )


def sweep(workspace: str = "", dry_run: bool = False) -> int:
    home = config_module.home()
    settings = _settings()
    result = curator_module.sweep(
        _discover(workspace), home, settings, dry_run=dry_run
    )

    if not result.checked:
        output.info("  no agent-written skills to curate")
        return 0

    for move in result.moved:
        output.console.print(f"  [dim]{move}[/dim]")

    output.info(f"  {result.summary()}")
    if result.skipped_pinned:
        output.info(f"  {result.skipped_pinned} pinned and left alone")
    if dry_run:
        output.info("  nothing was changed — this was a preview")
    elif result.archived:
        output.info("  archived skills are recoverable: andromeda curator restore <name>")
    return 0


def review(workspace: str = "", show_only: bool = False) -> int:
    home = config_module.home()

    if show_only:
        written, proposals = curator_module.load_proposals(home)
        if not proposals:
            output.info("  no proposals on record — andromeda curator review")
            return 0
        output.info(f"  written {written}\n")
        _print_proposals(proposals)
        return 0

    skills = _discover(workspace)
    if not skill_usage.report(skills, home):
        output.info("  no agent-written skills to review")
        return 0

    from andromeda_agent import build_provider
    from andromeda_agent.errors import AgentError

    try:
        provider = build_provider(config_module.load())
    except AgentError as exc:
        output.agent_error(exc)
        return 1

    def ask(prompt: str) -> str:
        # One turn, no tools. The reviewer reads what it is given and answers;
        # letting it call tools would make a read-only review something that
        # can change the library it is reviewing.
        generator = provider.stream_turn(
            [{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.2,
            tools=None,
        )
        while True:
            try:
                next(generator)
            except StopIteration as stop:
                return stop.value.content

    output.info("  reading the library…")
    proposals = curator_module.review(skills, home, ask)

    if not proposals:
        output.ok("Nothing to change.")
        return 0

    output.console.print()
    _print_proposals(proposals)
    output.console.print()
    output.info("  these are proposals — nothing has been changed")
    return 0


def _print_proposals(proposals: list[curator_module.Proposal]) -> None:
    for item in proposals:
        name = item.skill or "(the library)"
        output.console.print(f"  [cyan]{name}[/cyan] [dim]{item.kind}[/dim]")
        output.console.print(f"      {item.what}")
        if item.why:
            output.console.print(f"      [dim]{item.why}[/dim]")


def pin(name: str, workspace: str = "") -> int:
    return _set_pin(name, True, workspace)


def unpin(name: str, workspace: str = "") -> int:
    return _set_pin(name, False, workspace)


def _set_pin(name: str, pinned: bool, workspace: str) -> int:
    home = config_module.home()
    skills = _discover(workspace)
    if name not in skills:
        known = ", ".join(sorted(skills)) or "none"
        output.fail(f"No skill named {name!r}.", f"Found: {known}")
        return 2

    skill_usage.set_pinned(home, name, pinned)
    if pinned:
        output.ok(f"{name} is pinned — the sweep will leave it alone.")
    else:
        output.ok(f"{name} is no longer pinned.")
    return 0


def restore(name: str) -> int:
    home = config_module.home()
    ok, detail = skill_usage.restore(home, name)
    if not ok:
        output.fail(f"Could not restore {name}: {detail}")
        return 2
    output.ok(f"{name} is back at {detail}.")
    return 0


def pause() -> int:
    curator_module.set_paused(config_module.home(), True)
    output.ok("The sweep is paused. Nothing will move until you resume it.")
    return 0


def resume() -> int:
    curator_module.set_paused(config_module.home(), False)
    output.ok("The sweep is on again.")
    return 0
