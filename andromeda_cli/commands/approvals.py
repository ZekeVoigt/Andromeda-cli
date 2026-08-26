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


# ---------------------------------------------------------------------------
# What the gate would do
# ---------------------------------------------------------------------------

# One line per step of `Policy.decide`, in its order. The order is the whole
# design of that function — a hard refusal must not be reachable by a softer
# answer further down — so a command that explains a verdict has to name the
# step that produced it, not just the verdict.
EXIT_ALLOWED = 0
EXIT_USAGE = 1
EXIT_ASKS = 2
EXIT_DENIED = 3


def why(spec, policy) -> tuple[str, str]:
    """(decision, the rule that produced it).

    Re-derived here rather than instrumented into `decide`, deliberately:
    the gate is security-critical and adding reporting to it would be adding
    a second thing it has to get right. This walks the same conditions in the
    same order and is checked against the real answer by its tests — if the
    two ever disagree, that is the bug worth finding.
    """
    from andromeda_agent.approval import ALWAYS_CONFIRM
    from andromeda_tools import tier_rank

    decision = policy.decide(spec)

    if policy.mode == "deny":
        return decision, "approval mode is `deny` — nothing runs"
    if spec.name not in policy.enabled and not spec.name.startswith("mcp__"):
        return decision, "the tool is switched off for this session"
    if policy.specialist is not None and not policy.specialist.admits(spec):
        return decision, f"the {policy.specialist.label} belt does not admit it"
    if tier_rank(spec.risk_tier) > tier_rank(policy.max_tier):
        return decision, (
            f"its tier ({spec.risk_tier}) is above the ceiling ({policy.max_tier})"
        )

    override = policy.overrides.get(spec.name)
    if override is not None:
        return decision, f"you set an override for it: {override}"

    if policy.allowlist is not None:
        entry = policy.allowlist.entry_for(spec.name, spec.risk_tier)
        if entry is not None:
            return decision, (
                f"a learned entry ({entry.trust_level}) at tier {entry.risk_tier}"
            )
        # Looked up by name, ignoring the tier — `entry_for` deliberately
        # answers None for a tier mismatch, which is the case being explained.
        stale = next(
            (
                item
                for item in policy.allowlist.all()
                if item.tool_name == spec.name
            ),
            None,
        )
        if stale is not None and stale.risk_tier != spec.risk_tier:
            return decision, (
                f"a learned entry exists but was granted at tier "
                f"{stale.risk_tier}, and the tool is now {spec.risk_tier}"
            )

    if spec.name in policy.session_grants:
        return decision, "you allowed it for this session"
    if spec.risk_tier in ALWAYS_CONFIRM and policy.mode != "auto":
        return decision, f"{spec.risk_tier} work always stops for a person"
    if policy.mode == "ask" and spec.risk_tier != "safe_local":
        return decision, "`ask` mode stops for anything that is not safe_local"
    return decision, "nothing stands in its way"


def test(tool: str, mode: str = "", workspace: str = "") -> int:
    """Say what the gate would do with a tool, without running anything.

    Nothing is executed, nobody is prompted, and nothing is written down —
    which is what makes this safe to run against `terminal`.
    """
    from andromeda_tools import Workspace, build_registry
    from andromeda_tools.todo import TodoList

    from ..session import build_policy

    config = config_module.load()
    if mode:
        config = {**config, "approval_mode": mode}

    workspace_root = Workspace(workspace) if workspace else Workspace()
    registry = build_registry(workspace_root, TodoList())
    spec = registry.get(tool)
    if spec is None:
        known = ", ".join(sorted(registry))
        output.fail(f"No tool named {tool!r}.", f"Known: {known}")
        return EXIT_USAGE

    policy = build_policy(config, interactive=True, allowlist=_allowlist())
    decision, reason = why(spec, policy)

    colour = {
        "allowed": "green",
        "needs_approval": "yellow",
        "denied": "red",
    }[decision]
    output.console.print(
        f"  [cyan]{tool}[/cyan] [dim]{spec.risk_tier}[/dim] "
        f"[{colour}]{decision}[/{colour}]"
    )
    output.console.print(f"      [dim]{reason}[/dim]")
    output.console.print(
        f"      [dim]mode {policy.mode} · ceiling {policy.max_tier}[/dim]"
    )

    return {
        "allowed": EXIT_ALLOWED,
        "needs_approval": EXIT_ASKS,
        "denied": EXIT_DENIED,
    }[decision]


# ---------------------------------------------------------------------------
# What is worth being asked about less
# ---------------------------------------------------------------------------

# Never proposed, however many times they were approved. The rule is about
# what a mistake costs: a benign tool wrongly left out costs one more prompt,
# and a destructive one wrongly promoted costs data. `terminal` is the case
# that matters — approving `git status` twenty times says nothing about the
# next command the model puts through the same tool.
NEVER_SUGGEST_TIERS = frozenset({"destructive", "irreversible"})


def suggest(apply: str = "") -> int:
    """Tools you have approved often enough to stop being asked about.

    Proposals only, unless `--apply` names one. A command that promoted things
    on its own would be learned trust widening itself, which is the one thing
    this allowlist is built not to do.
    """
    from andromeda_tools import Workspace, build_registry
    from andromeda_tools.todo import TodoList

    allowlist = _allowlist()
    registry = build_registry(Workspace(), TodoList())

    candidates = []
    withheld = []
    for name, spec in sorted(registry.items()):
        count = allowlist.approvals_of(name)
        if count < allowlist_suggest_after() or not allowlist.should_suggest(name):
            continue
        if spec.risk_tier in NEVER_SUGGEST_TIERS:
            withheld.append((name, spec, count))
            continue
        candidates.append((name, spec, count))

    if apply:
        return _apply(apply, candidates, allowlist)

    if not candidates and not withheld:
        output.info("  nothing you have approved often enough yet")
        output.info("  Answer ! at a prompt to stop being asked about a tool.")
        return 0

    for index, (name, spec, count) in enumerate(candidates, start=1):
        output.console.print(
            f"  [cyan]{index}[/cyan]  {name} [dim]{spec.risk_tier} · "
            f"approved {count} times[/dim]"
        )

    for name, spec, count in withheld:
        # Named rather than hidden: "why is terminal not on this list" is a
        # question the answer to which is the point.
        output.console.print(
            f"  [dim]—  {name} · approved {count} times · not proposed: "
            f"{spec.risk_tier} work stays at the gate[/dim]"
        )

    if candidates:
        output.console.print()
        output.info("  andromeda approvals suggest --apply 1,2   to stop being asked")
    return 0


def allowlist_suggest_after() -> int:
    from andromeda_agent.allowlist import SUGGEST_AFTER

    return SUGGEST_AFTER


def _apply(apply: str, candidates: list, allowlist) -> int:
    if not candidates:
        output.fail("Nothing is being proposed.")
        return EXIT_USAGE

    chosen = []
    for token in apply.split(","):
        token = token.strip()
        if not token.isdigit() or not 1 <= int(token) <= len(candidates):
            output.fail(f"{token!r} is not one of the numbers listed.")
            return EXIT_USAGE
        chosen.append(candidates[int(token) - 1])

    for name, spec, _count in chosen:
        allowlist.trust(name, spec.risk_tier, "always_allow")
        output.ok(f"{name} will not be asked about again at tier {spec.risk_tier}.")

    output.info("  A tool whose tier rises is back at the gate.")
    output.info("  andromeda approvals forget <tool> to undo one.")
    return 0
