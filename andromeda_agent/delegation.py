"""Handing one piece of work to a narrowed helper.

Deliberately **not** named `sessions_spawn`. The hosted tool of that name
returns a run id at once, runs lanes in the background, and offers `operator`
and `browser` specialists that hold a connected account or the one browser.
None of that exists here: delegation is synchronous and there are three belts.
Reusing the name with a smaller schema would make the shared-name guard
meaningless — a model that learned `background: true` on one surface would find
it silently ignored on the other. A different capability gets a different name.

What is carried over exactly is the part that matters for safety:

  - The child gets a **belt**, and the belt is a hard denial (see `approval`).
  - The child's policy is derived with `Policy.narrow()`, so it can only ever
    hold a subset of what the parent holds.
  - No child can delegate. `can_spawn` is false on every specialist, and
    `delegate` is removed from the child's registry regardless.
  - The child starts on a fresh context and cannot see this conversation.

Lanes run in the background by default, at most three at a time
(`MAX_CONCURRENT_LANES`). `subagents_list`, `subagents_status` and
`subagents_wait` mirror the hosted registry's names *and* schemas exactly —
unlike `delegate` itself, those three contracts are ones this harness can honour
in full.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from andromeda_tools import ToolResult, ToolSpec
from andromeda_tools.spec import failure

from . import hooks
from .lanes import Lane, LaneRegistry
from .specialists import SPECIALISTS, resolve

# What the parent gets back. Long enough for a real report, short enough that
# three lanes do not blow up the parent's context.
MAX_REPORT = 12_000

BRIEF_TEMPLATE = """You are the {label} lane, running one piece of delegated work.

{purpose}

You are on a fresh context. You cannot see the conversation that sent you here,
so everything you need is below. Do not ask questions — there is nobody to
answer them; work with what you have and say plainly what you could not
establish.

## Your tools
{toolbelt}

That is the complete list. If the task needs something not on it, say so and
stop. Never write out a command, a shell block, or anything shaped like a tool
call as part of your answer — the lane that sent you cannot tell that apart from
work you actually did, and will report it as done.

## Task
{task}
{context}{criteria}{expected}
You have at most {max_turns} steps. Finish inside them: an unfinished lane that
ran out of budget is indistinguishable from one that failed, so if you are
running short, report what you have rather than starting something new."""


@dataclass
class Delegation:
    """One completed lane, as the parent sees it."""

    specialist: str
    label: str
    report: str
    turns: int
    # Tools the lane actually called, read off its transcript — not off what it
    # says it did. A lane with no shell will happily write `<shell>...</shell>`
    # into its prose, and a parent that reads that as evidence will report work
    # that never happened. Observed live; see `_evidence`.
    tools_used: list[str] = field(default_factory=list)
    truncated: bool = False
    # The lane's own working copy, when `worktree_isolation` is on. Carried
    # into the report because the parent cannot otherwise find the branch the
    # work is on — and a lane whose commits nobody merges did the work twice.
    worktree: Any = None


def build_brief(
    specialist_id: str,
    task: str,
    context: str = "",
    success_criteria: list[str] | None = None,
    expected_output: str = "",
    toolbelt: list[str] | None = None,
) -> str:
    specialist = SPECIALISTS[specialist_id]

    context_block = f"\n## What you need to know\n{context.strip()}\n" if context.strip() else ""

    criteria_block = ""
    if success_criteria:
        lines = "\n".join(f"- {item}" for item in success_criteria if str(item).strip())
        if lines:
            # Graded, not merely stated. The hosted runtime accepts criteria and
            # never checks them; requiring the lane to answer each one before it
            # returns is the cheap half of that, and it is free here.
            criteria_block = (
                "\n## Checks your answer must satisfy\n"
                f"{lines}\n\n"
                "Before you finish, state for each check whether it is met, and "
                "how you established that. If one is not met, say so — a lane "
                "that claims a check it did not make is worse than one that "
                "reports a gap.\n"
            )

    expected_block = (
        f"\n## Shape to return\n{expected_output.strip()}\n" if expected_output.strip() else ""
    )

    belt_list = (
        "\n".join(f"- {name}" for name in sorted(toolbelt))
        if toolbelt
        else "- (none — you can only reason and report)"
    )

    return BRIEF_TEMPLATE.format(
        label=specialist.label,
        toolbelt=belt_list,
        purpose=specialist.purpose,
        task=task.strip(),
        context=context_block,
        criteria=criteria_block,
        expected=expected_block,
        max_turns=specialist.max_turns,
    )


def _needs_browser(specialist_id: str) -> bool:
    return specialist_id == "browser"


def _exclusive_surface(specialist_id: str, isolated: bool) -> str:
    """Which surface this lane holds alone, if any.

    The browser is one browser, always. The working tree is shared only when
    lanes do not each have a copy of it — with `worktree_isolation` on, two
    builders are genuinely independent and serialising them would throw away
    the reason for the setting.
    """
    if _needs_browser(specialist_id):
        return "browser"
    belt = SPECIALISTS.get(specialist_id)
    if belt is not None and belt.writes_tree and not isolated:
        return "tree"
    return ""


def make_delegate_tool(
    run_lane: Callable[..., Delegation],
    on_start: Callable[[str, str], None] | None = None,
    registry: LaneRegistry | None = None,
    session_id: str = "",
    isolated: bool = False,
) -> ToolSpec:
    """The `delegate` tool, bound to something that can actually run a lane.

    The runner is injected rather than imported so this module does not depend
    on the surface, and so a test can drive delegation without a provider.
    """

    def run(
        task: str,
        specialist: str = "scout",
        context: str = "",
        successCriteria: list[str] | None = None,  # noqa: N803 - schema field name
        expectedOutput: str = "",  # noqa: N803 - schema field name
        allowedTools: list[str] | None = None,  # noqa: N803 - schema field name
        deniedTools: list[str] | None = None,  # noqa: N803 - schema field name
        label: str = "",
        background: bool = True,
    ) -> ToolResult:
        task = (task or "").strip()
        if not task:
            return failure("A delegated lane needs a task.")

        belt = resolve(specialist)
        if belt is None:
            return failure(
                f"No specialist named {specialist!r}. "
                f"Available: {', '.join(SPECIALISTS)}"
            )

        if on_start is not None:
            on_start(belt.id, label or task[:60])

        if background and registry is not None:
            lane = registry.start(
                specialist=belt.id,
                label=label or task[:60],
                task=task,
                run=lambda lane: _observed(
                    lambda: run_lane(
                        specialist=belt.id,
                        task=task,
                        context=context or "",
                        success_criteria=successCriteria or [],
                        expected_output=expectedOutput or "",
                        allowed_tools=allowedTools or None,
                        denied_tools=deniedTools or None,
                        label=label or "",
                        lane=lane,
                    ),
                    parent_session_id=session_id,
                    specialist_id=belt.id,
                    run_id=lane.id,
                    task=task,
                ),
                exclusive=_exclusive_surface(belt.id, isolated),
            )
            return ToolResult(
                content=(
                    f"Started lane {lane.id} ({belt.label}). It runs alongside this "
                    f"turn — start any other lanes now, then call subagents_wait "
                    f"before you rely on the answer."
                ),
                display=f"{belt.label} lane {lane.id} started",
                metadata={"lane": lane.id, "specialist": belt.id, "background": True},
            )

        try:
            outcome = _observed(
                lambda: run_lane(
                    specialist=belt.id,
                    task=task,
                    context=context or "",
                    success_criteria=successCriteria or [],
                    expected_output=expectedOutput or "",
                    allowed_tools=allowedTools or None,
                    denied_tools=deniedTools or None,
                    label=label or "",
                ),
                parent_session_id=session_id,
                specialist_id=belt.id,
                run_id="",
                task=task,
            )
        except Exception as exc:  # noqa: BLE001 - a failed lane is a result
            return failure(f"The {belt.label} lane failed: {exc}")

        report = outcome.report.strip() or "(the lane returned nothing)"
        truncated = len(report) > MAX_REPORT
        if truncated:
            report = report[:MAX_REPORT] + "\n\n… report truncated."

        plural = "" if outcome.turns == 1 else "s"
        header = (
            f"[{outcome.label or belt.label} · {outcome.turns} step{plural} · "
            f"{_evidence(outcome)}]"
        )
        if outcome.worktree is not None:
            header = f"{header}\n[{outcome.worktree.summary()}]"
        metadata = {
            "specialist": belt.id,
            "turns": outcome.turns,
            "truncated": truncated,
        }
        if outcome.worktree is not None:
            metadata["worktree"] = outcome.worktree.as_dict()
        return ToolResult(
            content=f"{header}\n{report}",
            display=f"{belt.label}: {outcome.turns} step{plural}",
            metadata=metadata,
        )

    return ToolSpec(
        name="delegate",
        description=(
            "Hand one self-contained piece of work to a narrowed helper and wait "
            "for its report. The helper starts on a fresh context and cannot see "
            "this conversation, so state the task so it stands entirely on its "
            "own. Each specialist has a fixed toolbelt and step budget it cannot "
            "exceed, and no helper can delegate further. Use this to keep a long "
            "search or a bounded sub-task out of your own context — not for work "
            "you could do in one or two tool calls yourself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What the helper must accomplish. It cannot see this "
                        "conversation, so state it so it stands entirely alone."
                    ),
                },
                "specialist": {
                    "type": "string",
                    "enum": list(SPECIALISTS),
                    "description": (
                        "scout: reads, searches and reports; changes nothing. "
                        "writer: drafts text and cannot reach anything outside "
                        "this machine. verifier: re-reads and checks work, and "
                        "cannot store what it concludes. Defaults to scout, "
                        "which cannot change anything."
                    ),
                    "default": "scout",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Facts the helper needs but cannot discover for itself. "
                        "Anything established earlier here is invisible to it "
                        "unless you restate it."
                    ),
                },
                "successCriteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Checks the answer must satisfy. The helper is required "
                        "to state whether each is met before it returns."
                    ),
                },
                "expectedOutput": {
                    "type": "string",
                    "description": (
                        "The shape to return, e.g. 'a JSON array of {url, title}'. "
                        "Without this you get prose you have to re-parse."
                    ),
                },
                "allowedTools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exclusive allowlist, applied on top of the specialist's "
                        "belt. Cannot grant anything you do not already hold."
                    ),
                },
                "deniedTools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tools to withhold. Applied after allowedTools.",
                },
                "label": {
                    "type": "string",
                    "description": "Short name for this lane, shown while it works.",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Default true: returns a lane id at once, so the next "
                        "delegate in this response starts alongside it. Set false "
                        "only when you cannot continue at all until this one lane "
                        "answers — that blocks the turn, and two such lanes cost "
                        "the sum of their times, not the longer."
                    ),
                    "default": True,
                },
            },
            "required": ["task"],
        },
        # Its danger is whatever the child does, and the child is bounded by a
        # belt that can only narrow what this session already holds. The gate
        # that matters is the one the child inherits, not a prompt here.
        risk_tier="safe_local",
        category="write",
        run=run,
        summarize=lambda arguments: (
            f"delegate to {arguments.get('specialist', 'scout')}: "
            f"{str(arguments.get('task', ''))[:80]}"
        ),
    )


def _observed(
    work: Callable[[], Delegation],
    *,
    parent_session_id: str,
    specialist_id: str,
    run_id: str,
    task: str,
) -> Delegation:
    """Run a lane between its two lifecycle events.

    Wrapped around both the blocking and the background path, so a hook counts
    every lane rather than only the ones that happened to be waited on. A lane
    that raises still reports a stop — with `status="error"`, because a child
    that vanished silently is the failure mode that made lane debugging hard in
    the first place.
    """
    common = {
        "parent_session_id": parent_session_id,
        "specialist_id": specialist_id,
        "run_id": run_id,
        "task": task,
    }
    hooks.fire("subagent_start", **common)
    started = time.monotonic()
    try:
        outcome = work()
    except BaseException as exc:
        hooks.fire(
            "subagent_stop",
            **common,
            status="error",
            summary=f"{type(exc).__name__}: {exc}",
            tool_call_history=[],
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        raise
    hooks.fire(
        "subagent_stop",
        **common,
        status="completed",
        summary=outcome.report,
        tool_call_history=list(outcome.tools_used),
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )
    return outcome


def _evidence(outcome: Delegation) -> str:
    """What the lane actually did, from its transcript rather than its prose.

    Stated in the header so the parent has a ground truth to weigh the report
    against. A lane that called nothing and describes having run a command is
    then visibly describing something it did not do.
    """
    if not outcome.tools_used:
        return "called no tools"
    counts: dict[str, int] = {}
    for name in outcome.tools_used:
        counts[name] = counts.get(name, 0) + 1
    rendered = ", ".join(
        name if count == 1 else f"{name}×{count}" for name, count in sorted(counts.items())
    )
    return f"called {rendered}"


# ---------------------------------------------------------------------------
# Inspecting lanes.
#
# These three mirror `lib/agent-runtime/subagents/tool-definitions.ts` by name
# and by schema. Unlike `sessions_spawn`, whose contract includes managed
# accounts and browser profiles this harness has no equivalent of, these are
# contracts it can honour in full — so they take the hosted names, and the
# drift guard holds them to it.
# ---------------------------------------------------------------------------


def _render(lanes: list[Lane]) -> str:
    if not lanes:
        return "No lanes."
    return "\n".join(lane.summary() for lane in lanes)


def _report(lane: Lane) -> str:
    if lane.status == "running":
        state = "stale — no progress for a long time" if lane.is_stale else "still running"
        detail = f", currently in {lane.current_tool}" if lane.current_tool else ""
        return f"[{lane.id} · {lane.specialist}] {state}{detail} after {int(lane.elapsed)}s."
    if lane.status == "failed":
        return f"[{lane.id} · {lane.specialist}] failed after {int(lane.elapsed)}s: {lane.error}"

    outcome = lane.result
    body = getattr(outcome, "report", None) or "(the lane returned nothing)"
    evidence = _evidence(outcome) if outcome is not None else "called no tools"
    steps = getattr(outcome, "turns", 0)
    plural = "" if steps == 1 else "s"
    header = f"[{lane.id} · {lane.label} · {steps} step{plural} · {evidence}]"
    # A background lane's work is only reachable through its branch, and this
    # report is the only place the parent is told which one.
    worktree = getattr(outcome, "worktree", None)
    if worktree is not None:
        header = f"{header}\n[{worktree.summary()}]"
    return f"{header}\n{body}"


def make_lane_tools(registry: LaneRegistry) -> list[ToolSpec]:
    def listing(activeOnly: bool = False) -> ToolResult:  # noqa: N803 - schema field
        lanes = registry.all(active_only=activeOnly)
        return ToolResult(content=_render(lanes), display=f"{len(lanes)} lane(s)")

    def status(runId: str) -> ToolResult:  # noqa: N803 - schema field
        lane = registry.get(runId)
        if lane is None:
            return failure(f"No lane {runId!r}. Call subagents_list to see them.")
        return ToolResult(content=_report(lane), display=f"{lane.id}: {lane.status}")

    def wait(
        runIds: list[str] | None = None,  # noqa: N803 - schema field
        timeoutSeconds: float = 0,  # noqa: N803 - schema field
    ) -> ToolResult:
        lanes = registry.wait(runIds, timeout=float(timeoutSeconds or 0))
        if not lanes:
            return ToolResult(content="No lanes were running.", display="nothing to wait for")

        reports = [_report(lane) for lane in lanes]
        still = [lane for lane in lanes if lane.status == "running"]
        if still:
            reports.append(
                f"{len(still)} lane(s) had not finished when the wait expired: "
                + ", ".join(lane.id for lane in still)
            )
        return ToolResult(
            content="\n\n".join(reports),
            display=f"{len(lanes) - len(still)}/{len(lanes)} finished",
            metadata={"finished": len(lanes) - len(still), "total": len(lanes)},
        )

    return [
        ToolSpec(
            name="subagents_list",
            description="List the lanes started in this session and their state.",
            parameters={
                "type": "object",
                "properties": {
                    "activeOnly": {
                        "type": "boolean",
                        "description": "If true, only return accepted/running subagents.",
                        "default": False,
                    }
                },
            },
            risk_tier="safe_local",
            category="read",
            run=listing,
            summarize=lambda _arguments: "list lanes",
        ),
        ToolSpec(
            name="subagents_status",
            description="Check one lane: whether it finished, and its report if it did.",
            parameters={
                "type": "object",
                "properties": {
                    "runId": {
                        "type": "string",
                        "description": "Run ID returned by sessions_spawn.",
                    }
                },
                "required": ["runId"],
            },
            risk_tier="safe_local",
            category="read",
            run=status,
            summarize=lambda arguments: f"status of {arguments.get('runId', '?')}",
        ),
        ToolSpec(
            name="subagents_wait",
            description=(
                "Wait for background lanes to finish and collect their reports. "
                "Call this before relying on anything a lane was asked to find."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "runIds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Run IDs to wait for. Omit to wait for every "
                            "background subagent still running in this session."
                        ),
                    },
                    "timeoutSeconds": {
                        "type": "number",
                        "description": (
                            "Give up waiting after N seconds and return whatever "
                            "finished, with the rest listed as still running "
                            "(0 = wait indefinitely)."
                        ),
                        "default": 0,
                    },
                },
            },
            risk_tier="safe_local",
            category="read",
            run=wait,
            summarize=lambda _arguments: "wait for lanes",
        ),
    ]
