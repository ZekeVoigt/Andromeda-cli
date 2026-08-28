"""Jobs that run somewhere you are not.

`schedule.py` answers "may this job act while nobody is watching?". This module
answers the question that only exists once a job can run on hardware the person
does not own:

  **May this job act while nobody is watching, on a machine they cannot reach,
  holding credentials they gave it?**

Those are not the same grant, and the second is strictly larger. On your own
laptop an unattended job is still bounded by a machine you can unplug, a disk
you can read and a process you can `kill`. On a hosted runner none of that is
true, so the belt has to be narrower and the consent has to be its own act.

Two fields carry it, and they are orthogonal on purpose:

  ``runs_on``         where the loop executes — ``device`` or ``cloud``
  ``workspace_kind``  what it may touch — ``device`` or ``detached``

The useful combinations are not the diagonal, which is why there are two fields
and not one flag:

  device + device     today's job, unchanged
  device + detached   runs here, touches no files — a tighter belt, locally
  cloud  + detached   runs with the laptop off. The reason this module exists.
  cloud  + device     **refused at creation.** See `location_refusal`.

That last cell is the whole honesty of the design. A container cannot see
``/Users/you/Downloads``, and a job that silently no-ops is worse than one that
was never created: it looks scheduled, reports nothing, and reads as "the thing
I was watching never changed".

``repo`` — clone a git remote, work, push a branch — is the third, and it landed
last on purpose: it is the only kind that can change something somebody reads
later. Everything bounding it was built first. `repo.py` holds the rule, which
is structural rather than instructed: **a job never pushes to a branch it did
not create**, checked against the branch name this run generated rather than
against a denylist of protected names, because a denylist is a list somebody's
default branch is missing from.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from andromeda_tools import RiskTier

# Where the loop executes.
# `auto` is the default and the one most jobs should carry. The other two are
# a person overriding it.
#
# The device/cloud split was a question asked at creation time, and it was the
# wrong question: somebody scheduling "watch my deploys" does not know or care
# where it runs, they care that it runs. Worse, the answer was permanent — a
# job created on a Tuesday afternoon ran on the laptop forever, including every
# night the lid was shut, which is precisely when a watcher earns its keep.
#
# `auto` moves the decision from creation time to fire time. See
# `resolve_placement`.
RUN_LOCATIONS = ("device", "cloud", "auto")

# What the job may touch.
#
# `repo` landed last, deliberately: it is the only kind that can change
# something a person reads later, so it waited for the consent axis, the tool
# ceiling, the fire claim and the budgets — all of which exist to bound it. See
# `repo.py` for the rule it is built around: a job never pushes to a branch it
# did not create.
WORKSPACE_KINDS = ("device", "detached", "repo")


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------

# The highest risk tier anything may reach in the cloud, whatever the job's
# approval mode says. `destructive` and `irreversible` both need a person who
# can watch and stop; a hosted runner has neither.
CLOUD_MAX_TIER: RiskTier = "outbound"

# Tools no cloud job gets, at any approval mode, with any grant. A constant in
# the source rather than a setting, because a configurable ceiling is a ceiling
# somebody raises at 2am to unblock one job and never lowers.
#
# Anchored on tool NAMES, never on prose. The lifecycle refusal in `schedule.py`
# already learned this the expensive way: a cron prompt is fed to a model, not a
# shell, so matching the English "run a shell command" would refuse legitimate
# jobs and stop none of the real ones.
CLOUD_DENIED_TOOLS: dict[str, str] = {
    # An unattended shell on a box holding a live credential is the thing this
    # whole module exists to prevent.
    "terminal": "an unattended shell on a hosted machine",
    # A cloud job that can create cloud jobs is the respawn-loop refusal with a
    # credit card attached.
    "cron": "a job that can schedule more jobs, unattended and billed",
    # The refs-only browser is good, and a signed-in browser profile on a
    # hosted box is a session-cookie custody problem that deserves its own
    # design rather than a line in this dict.
    "browser_navigate": "a signed-in browser session on hardware you do not hold",
    "browser_snapshot": "a signed-in browser session on hardware you do not hold",
    "browser_read": "a signed-in browser session on hardware you do not hold",
    "browser_click": "a signed-in browser session on hardware you do not hold",
    "browser_type": "a signed-in browser session on hardware you do not hold",
    "browser_press": "a signed-in browser session on hardware you do not hold",
    "browser_scroll": "a signed-in browser session on hardware you do not hold",
    "browser_back": "a signed-in browser session on hardware you do not hold",
}

# Tools that need a filesystem the job is not going to have. Denied for
# `workspace_kind: detached` wherever it runs — including on the device, where
# `detached` is a person deliberately tightening the belt and must mean it.
DETACHED_DENIED_TOOLS: dict[str, str] = {
    "write_file": "there is no workspace to write into",
    "patch": "there is no workspace to patch",
    "read_file": "there is no workspace to read from",
    "list_dir": "there is no workspace to list",
    "search_files": "there is no workspace to search",
}


def denied_tools(names: Iterable[str], runs_on: str, workspace_kind: str) -> dict[str, str]:
    """Which of these tool names this job may not have, and why each.

    Returned as a mapping rather than a set because the caller's job is to say
    *why* — "3 tools were dropped" is the report that sends somebody reading
    source at 3am.
    """
    refused: dict[str, str] = {}
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if runs_on == "cloud" and name in CLOUD_DENIED_TOOLS:
            refused[name] = CLOUD_DENIED_TOOLS[name]
        elif workspace_kind == "detached" and name in DETACHED_DENIED_TOOLS:
            refused[name] = DETACHED_DENIED_TOOLS[name]
    return refused


def narrow_tools(names: Iterable[str], runs_on: str, workspace_kind: str) -> list[str]:
    """The belt, with everything this location forbids removed.

    Subtract-only, exactly as `Policy.narrow` is: this can never add a name,
    so a job spec is never a way to reach a tool the location denies. Order is
    preserved so a printed belt reads the way the person typed it.
    """
    refused = denied_tools(names, runs_on, workspace_kind)
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        name = (name or "").strip()
        if not name or name in refused or name in seen:
            continue
        seen.add(name)
        kept.append(name)
    return kept


def max_tier_for(runs_on: str) -> RiskTier | None:
    """The tier ceiling this location imposes, or None for no extra clamp.

    `auto` takes the cloud ceiling, not the device one. A job that *may* run
    unattended is bounded by the stricter of the two places it might land —
    working out the ceiling per fire would mean a job whose permissions change
    depending on whether a laptop happened to be awake, which is not something
    anybody could reason about or consent to.
    """
    return CLOUD_MAX_TIER if runs_on in {"cloud", "auto"} else None


def resolve_placement(
    runs_on: str, workspace_kind: str, cloud_available: bool = True
) -> str:
    """Where this fire actually happens. Always `device` or `cloud`.

    `device` and `cloud` are honoured as written — a person who said where it
    runs meant it.

    `auto` decides here, and the workspace decides for it. A job whose
    workspace is a directory on this machine cannot run anywhere else; the
    container has no such directory, and pretending otherwise produces a job
    that reports success having looked at nothing. Everything else prefers the
    cloud, because the whole value of a scheduled job is the hours nobody is at
    the keyboard.

    `cloud_available` is the escape hatch for the one case the workspace rule
    gets wrong: no runner is reachable, because the account has no cloud or the
    server is down. Running late on the laptop beats not running at all.
    """
    if runs_on in {"device", "cloud"}:
        return runs_on
    if workspace_kind == "device":
        return "device"
    return "cloud" if cloud_available else "device"


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def location_refusal(runs_on: str, workspace_kind: str, workspace: str) -> str:
    """Why this job may not be created here, or an empty string.

    The message is the deliverable. Someone typing `--cloud` on a job that
    tidies their Downloads folder has a correct intention and an impossible
    request, and the difference between a good product and a bad one is
    whether they find that out now or in a week of silent non-reports.
    """
    if runs_on not in RUN_LOCATIONS:
        return f"runs_on must be one of {', '.join(RUN_LOCATIONS)}."
    if workspace_kind not in WORKSPACE_KINDS:
        return f"workspace kind must be one of {', '.join(WORKSPACE_KINDS)}."

    if runs_on == "cloud" and workspace_kind == "device":
        where = workspace or "a directory on this machine"
        return (
            f"This job's workspace is {where}, which exists only on this "
            "machine. A cloud job runs in a container that cannot see it, so "
            "`--cloud` with a local workspace is not a thing that can work.\n\n"
            "Three ways forward:\n"
            "  • drop --cloud            it runs here, when this machine is awake\n"
            "  • --detached              it runs in the cloud with no filesystem\n"
            "  • --repo <https url>      it works on a fresh clone and pushes a branch"
        )
    return ""


def agent_origin_refusal(runs_on: str) -> str:
    """Why an agent may not put its own job in the cloud.

    The rule `schedule.py` is built around — *an agent may propose autonomy,
    only a person grants the unattended kind* — extended along the second axis.
    Refused at **every** approval mode, including the read-only default, because
    the location is the grant here: a read-only job on a hosted runner still
    spends the person's credit, on a schedule, while they are asleep.

    Like the `auto` refusal it guards, this is belt-and-braces rather than the
    only defence: the `cron` tool has no `runs_on` parameter at all, so there is
    no argument for the model to get wrong and none for a prompt injection to
    set. This catches any future caller that forgets.
    """
    if runs_on == "cloud":
        return (
            "A job you create cannot be pinned to the cloud. Leave it on `auto` "
            "— the person grants the unattended half when they approve the "
            "call, and it then runs wherever makes sense per fire. Pinning is "
            "theirs: `andromeda cron approve <id> --run-on cloud`."
        )
    return ""


def tools_refusal(names: Iterable[str], runs_on: str, workspace_kind: str) -> str:
    """Why this belt cannot be granted here, or an empty string.

    Named tools are *refused*, not silently dropped. The intersection in
    `narrow_tools` is right for a belt the job inherited; it is wrong for one a
    person typed, because dropping what somebody explicitly asked for produces
    a job that mysteriously does nothing — the exact failure this module exists
    to prevent, arriving through the door marked "helpful".
    """
    refused = denied_tools(names, runs_on, workspace_kind)
    if not refused:
        return ""
    lines = [f"  {name} — {why}" for name, why in sorted(refused.items())]
    head = (
        "A cloud job cannot have these tools:"
        if runs_on == "cloud"
        else "A detached job cannot have these tools:"
    )
    return "\n".join([head, *lines])


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def secrets_refusal(references: Mapping[str, str], runs_on: str) -> str:
    """Why this job's credentials cannot follow it into the cloud.

    `secrets.py` resolves `op://`, `bw://`, `keychain://` and `cmd://`. Every
    one of them is a reference to *this* machine — a keychain, a signed-in
    helper, a binary on this `PATH`. In a container they do not fail
    interestingly; they fail as a missing environment variable, at fire time, in
    a log nobody is reading.

    So the check happens at creation, where a person is present to read it. The
    seam is already there: `RESOLVERS` is a dict, so "which schemes survive a
    container" is a membership test rather than a new abstraction.
    """
    if runs_on != "cloud" or not references:
        return ""

    from . import secrets as secrets_module

    broken: list[str] = []
    for name, reference in sorted(references.items()):
        scheme = secrets_module.scheme_of(str(reference))
        resolver = secrets_module.RESOLVERS.get(scheme)
        if resolver is None or getattr(resolver, "cloud_refusal", ""):
            why = (
                getattr(resolver, "cloud_refusal", "")
                if resolver is not None
                else f"`{scheme}://` is not a scheme this build knows"
            )
            broken.append(f"  {name} — {why}")

    if not broken:
        return ""
    return "\n".join(
        [
            "These credentials resolve against this machine and cannot follow "
            "the job into a container:",
            *broken,
            "",
            "Put them where the runner can reach them instead:",
            "  andromeda secrets put <NAME> --cloud",
        ]
    )


# ---------------------------------------------------------------------------
# Saying what was granted
# ---------------------------------------------------------------------------


def grant_summary(job) -> list[str]:
    """What moving this job to the cloud actually hands over.

    Printed verbatim before the grant, never after. `cron approve --approval
    auto` already prints the prompt for the same reason; cloud adds four more
    lines because four more things can surprise you — where it runs, what it can
    reach, what it may spend, and who hears about it.
    """
    belt = job.enabled_tools or ["the session default belt, narrowed by the cloud ceiling"]
    return [
        f"  prompt     {job.prompt or job.script}",
        f"  runs       {job.schedule}",
        "  where      a hosted runner — not this machine, and not stoppable from here",
        f"  reaches    {'no filesystem at all' if job.workspace_kind == 'detached' else job.workspace}",
        f"  tools      {', '.join(belt)}",
        f"  ceiling    nothing above `{CLOUD_MAX_TIER}` runs, whatever the approval mode says",
        f"  model      {job.model or 'the account default'}",
        f"  tells you  {job.deliver}",
    ]


# ---------------------------------------------------------------------------
# What a cadence costs
# ---------------------------------------------------------------------------

# How long a hosted runner stays awake after a fire before the platform stops
# it. Measured on Fly, 2026-08-25: ~303s. It is the least obvious number in this
# whole feature, because it makes the *cost* of a job depend on how often it
# fires and almost not at all on how long it runs.
IDLE_BEFORE_STOP_SECONDS = 300


def wake_cost_note(expression: str) -> str:
    """What this cadence will actually cost, in words, or "".

    Said at creation because the alternative is finding it on a bill. A person
    typing `--every 5m` has made a reasonable-looking choice that happens to
    land exactly on a cliff, and nothing about the schedule syntax hints at it.

    The arithmetic nobody expects: a runner stays awake about five minutes after
    each fire, so **a fire costs five minutes of machine time however short the
    job is**. A two-second watchdog and a four-minute agent turn cost the same.
    Two consequences follow, and both run against normal scheduling advice —
    cluster fires rather than spreading them, and cap how *often* a job fires
    rather than how long it runs.
    """
    import time as _time

    from .schedule import next_fire, parse_schedule

    try:
        canonical = parse_schedule(expression)
        first = next_fire(canonical, _time.time())
        # Measured from `first` exactly, not `first + 1`. The nudge looks
        # harmless and is not: it inflates every gap by a second, so `every 5m`
        # measures as 301s and slips past a `<= 300` cliff check by one — the
        # single cadence this function most needs to catch.
        second = next_fire(canonical, first)
    except Exception:  # noqa: BLE001 - an unparseable schedule is refused elsewhere
        return ""

    gap = second - first
    if gap <= 0:
        return ""

    if gap <= IDLE_BEFORE_STOP_SECONDS:
        return (
            "This fires at least as often as the runner takes to fall asleep "
            f"(~{IDLE_BEFORE_STOP_SECONDS // 60} min), so the machine will never "
            "stop. That is the same cost as a server left running around the "
            "clock. Consider a longer interval, or a `--watch` source so most "
            "ticks cost nothing at all."
        )

    # A fire keeps the machine up for the idle window, so the duty cycle is the
    # window over the interval. Reported rather than judged: a person may well
    # want an expensive cadence, and should simply know it is one.
    duty = min(100, round(100 * IDLE_BEFORE_STOP_SECONDS / gap))
    if duty >= 20:
        return (
            f"Each fire keeps the runner awake about "
            f"{IDLE_BEFORE_STOP_SECONDS // 60} min, so this cadence keeps it "
            f"running roughly {duty}% of the time."
        )
    return ""
