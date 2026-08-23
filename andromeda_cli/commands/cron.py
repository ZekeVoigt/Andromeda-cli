"""Scheduled jobs.

The command surface for `andromeda_agent.schedule`, plus the loop that actually
runs them.

Four things here are deliberate and worth not undoing:

  - **Creating a job states what it may do.** `add` prints the consent in full
    before it writes anything, because a job created with `--approval auto` may
    run shell commands unattended and "I typed a flag" is not the same as
    knowing that.
  - **A run is one process's worth of work, recorded whether it succeeds or
    not.** A scheduler that only logs failures leaves you unable to tell "it
    ran and found nothing" from "it never ran".
  - **One scheduler at a time.** Two daemons on one schedule fire every job
    twice, and a second copy of a job that writes files is not a duplicate
    report, it is a second edit.
  - **A tick is a heartbeat.** An empty history means "nothing was due" and
    "nothing has run for a week" equally well, and those need different
    reactions from the person reading it.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from andromeda_agent import Callbacks, build_provider
from andromeda_agent import runner as runner_module
from andromeda_agent import blueprints as blueprints_module
from andromeda_agent import providers_cron
from andromeda_agent import seeding as seeding_module
from andromeda_agent.executions import Ledger
from andromeda_agent.notepad import Notepad
from andromeda_agent.suggestions import Suggestions
from andromeda_agent.schedule import (
    Job,
    Run,
    Schedule,
    ScheduleError,
    SchedulerBusy,
    exclusive,
    heartbeat,
    heartbeat_age,
)

from .. import config as config_module
from .. import output
from .. import sessions as sessions_store
from ..session import build_conversation, notepad_path, schedule_path

TICK_SECONDS = 20
# Older installs kept the schedule beside config.yaml. It now lives in its own
# directory because a job has output, a monitor cache and a notepad beside it,
# and scattering four kinds of file through the home directory is how people
# lose them. Moved on first read rather than migrated by a version number:
# there is exactly one old shape and it has exactly one new home.
LEGACY_PATH_NAME = "cron.json"


def _schedule() -> Schedule:
    path = schedule_path()
    legacy = config_module.home() / LEGACY_PATH_NAME
    if legacy.exists() and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(path)
    return Schedule(path)


def _notepad() -> Notepad:
    return Notepad(notepad_path())


def _suggestions() -> Suggestions:
    return Suggestions(schedule_path().parent / "suggestions.json")


def _ledger() -> Ledger:
    return Ledger(schedule_path().parent / "executions.db")


def _lock_path() -> Path:
    return schedule_path().parent / "scheduler.lock"


def _heartbeat_path() -> Path:
    return schedule_path().parent / "heartbeat"


def _when(timestamp: float) -> str:
    if not timestamp:
        return "never"
    delta = timestamp - time.time()
    if delta < 0:
        return "due"
    if delta < 3600:
        return f"in {int(delta / 60)}m"
    if delta < 86400:
        # Hours *and* minutes. Truncating to whole hours renders a job due in
        # 119 minutes as "in 1h", which reads as an hour out and makes the
        # whole column look wrong.
        hours, minutes = divmod(int(delta / 60), 60)
        return f"in {hours}h" if not minutes else f"in {hours}h {minutes}m"
    return datetime.fromtimestamp(timestamp).strftime("%a %d %b %H:%M")


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


# ---------------------------------------------------------------------------
# Creating and inspecting
# ---------------------------------------------------------------------------


def add(
    schedule_expression: str,
    prompt: str,
    name: str = "",
    approval: str = "ask",
    workspace: str | None = None,
    repeat: int = 0,
    deliver: str = "none",
    script: str = "",
    no_agent: bool = False,
    watch: str = "",
    watch_url: str = "",
    after: list[str] | None = None,
    deliver_target: str = "",
    model: str = "",
    thinking: str = "",
    tools: str = "",
    skills: list[str] | None = None,
    attach_to: str = "",
) -> int:
    root = str(Path(workspace).expanduser().resolve() if workspace else Path.cwd())

    if watch and watch_url:
        output.fail(
            "Give one watch source, not two.",
            "--watch runs a script; --watch-url fetches a page.",
        )
        return 2

    try:
        job = _schedule().add(
            schedule_expression,
            prompt,
            root,
            name=name,
            approval_mode=approval,
            repeat=repeat,
            deliver=deliver,
            script=script,
            no_agent=no_agent,
            monitor_kind="script" if watch else ("url" if watch_url else ""),
            monitor_source=watch or watch_url,
            context_from=after or [],
            deliver_target=deliver_target,
            model=model,
            thinking=thinking,
            enabled_tools=[name.strip() for name in tools.split(",") if name.strip()],
            skills=skills or [],
            attach_to=attach_to,
        )
    except ScheduleError as exc:
        output.fail(str(exc))
        return 2

    _describe_new(job)
    return 0


def _describe_new(job: Job) -> None:
    output.ok(f"Created {job.id} — {job.name}")
    output.info(f"  runs      {job.schedule} · next {_when(job.next_run_at)}")
    if job.repeat:
        output.info(f"  repeat    {job.repeat} time(s), then it retires")
    output.info(f"  in        {job.workspace}")

    if job.no_agent:
        output.info(
            f"  script    {job.script} — no model call at all; its output is the "
            "report, and no output means silence."
        )
    elif job.script:
        output.info(f"  script    {job.script} — its output goes into the prompt")

    if job.is_monitored:
        output.info(
            f"  watches   {job.monitor_source} — the agent only runs when this "
            "changes, so an unchanged tick costs nothing."
        )
    if job.context_from:
        output.info(f"  after     reads the latest output of {', '.join(job.context_from)}")
    if job.model or job.thinking or job.enabled_tools or job.skills:
        overrides = [
            item
            for item in (
                f"model {job.model}" if job.model else "",
                f"thinking {job.thinking}" if job.thinking else "",
                f"{len(job.enabled_tools)} tools" if job.enabled_tools else "",
                f"skills {', '.join(job.skills)}" if job.skills else "",
            )
            if item
        ]
        output.info(f"  its own   {' · '.join(overrides)}")
    if job.attach_to:
        output.info(f"  attaches  each run to session {job.attach_to}")

    if job.deliver == "none":
        output.info("  tells you nothing — read it with `andromeda cron logs`")
    else:
        output.info(f"  delivers  {job.deliver}")

    # Stated in full, at creation, because nobody will be watching when it runs.
    if job.no_agent:
        pass
    elif job.approval_mode == "auto":
        output.console.print(
            "  [yellow]approval  auto — this job may write files and run shell "
            "commands with nobody watching.[/yellow]"
        )
    elif job.approval_mode == "deny":
        output.info("  approval  deny — it may reason and report, and use no tools.")
    else:
        output.info(
            "  approval  ask — with nobody to ask, it gets read-only tools only. "
            "Use --approval auto if it needs to change things."
        )

    output.info(f"\n  andromeda cron run {job.id}   # try it now")
    if heartbeat_age(_heartbeat_path()) is None:
        output.info("  andromeda cron install       # nothing is running jobs yet")


def show_list() -> int:
    schedule = _schedule()
    jobs = schedule.all()
    if not jobs:
        output.info("No scheduled jobs.")
        output.info('  andromeda cron add "every 1h" "check the build"')
        return 0

    width = max(len(job.name) for job in jobs)
    marks = {
        "on": "[green]on  [/green]",
        "off": "[dim]off [/dim]",
        "paused": "[yellow]held[/yellow]",
        "done": "[dim]done[/dim]",
    }
    for job in jobs:
        last = job.last_run
        outcome = ""
        if last is not None:
            outcome = {
                "ok": "[green]ok[/green]",
                "failed": "[red]failed[/red]",
                "no_change": "[dim]no change[/dim]",
                "silent": "[dim]quiet[/dim]",
            }.get(last.status, "")
        output.console.print(
            f"  {marks[job.state]} [cyan]{job.id}[/cyan]  {job.name.ljust(width)}  "
            f"[dim]{job.schedule} · {_when(job.next_run_at)}[/dim]  {outcome}"
        )
        if job.paused_reason:
            output.console.print(f"       [yellow]{job.paused_reason}[/yellow]")

    _report_scheduler()
    output.info(f"  {schedule.path}")
    return 0


def _report_scheduler() -> None:
    """Whether anything is actually going to run these.

    The most useful line in the whole command, and the one a scheduler usually
    leaves out: a list of jobs tells you what is configured, not what is alive.
    """
    age = heartbeat_age(_heartbeat_path())
    if age is None:
        output.console.print(
            "\n  [yellow]No scheduler has ever run here — these jobs will not "
            "fire.[/yellow]"
        )
        output.info("  andromeda cron install     # run it in the background")
        output.info("  andromeda cron daemon      # or in this terminal")
        return
    if age > TICK_SECONDS * 4:
        output.console.print(
            f"\n  [yellow]The scheduler last ticked {_ago(age)} — it is probably "
            "not running.[/yellow]"
        )
        return
    output.console.print(f"\n  [dim]scheduler ticked {_ago(age)}[/dim]")


def show(identifier: str) -> int:
    schedule = _schedule()
    job = schedule.resolve(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.", "andromeda cron list")
        return 2

    output.info(f"  {job.id} · {job.name}")
    output.info(f"  schedule  {job.schedule} · next {_when(job.next_run_at)}")
    output.info(f"  approval  {job.approval_mode} · created by {job.origin}")
    output.info(f"  in        {job.workspace}")
    if job.repeat:
        output.info(f"  repeat    {job.runs_done}/{job.repeat}")
    if job.script:
        output.info(f"  script    {job.script}{' (no agent)' if job.no_agent else ''}")
    if job.is_monitored:
        changed = _when(job.monitor_changed_at) if job.monitor_changed_at else "never"
        output.info(f"  watches   {job.monitor_kind}: {job.monitor_source}")
        output.info(f"  changed   {changed}")
    if job.context_from:
        output.info(f"  after     {', '.join(job.context_from)}")
    if job.paused_reason:
        output.console.print(f"  [yellow]held      {job.paused_reason}[/yellow]")

    notes = _notepad().page(job.id)
    if notes:
        output.info("  notepad")
        for key, value in sorted(notes.items()):
            output.console.print(f"    [dim]{key}: {value[:90]}[/dim]")

    if job.prompt:
        output.console.print(f"\n  [dim]{job.prompt}[/dim]\n")

    if not job.runs:
        output.info("  Never run.")
        return 0

    for run in job.runs[-10:]:
        when = datetime.fromtimestamp(run.started_at).strftime("%d %b %H:%M")
        mark = {
            "ok": "[green]✓[/green]",
            "failed": "[red]✗[/red]",
            "no_change": "[dim]·[/dim]",
            "silent": "[dim]·[/dim]",
        }.get(run.status, "?")
        took = int(run.finished_at - run.started_at) if run.finished_at else 0
        detail = run.summary if run.status != "failed" else run.error
        late = f" [yellow]{int(run.late_by / 60)}m late[/yellow]" if run.late_by > 90 else ""
        output.console.print(
            f"  {mark} [dim]{when}  {took}s[/dim]{late}  "
            f"{detail.splitlines()[0][:80] if detail else ''}"
        )
    output.info(f"\n  andromeda cron logs {job.id}")
    return 0


def logs(identifier: str, index: int = 0) -> int:
    """The full output of a run, not the one-line excerpt."""
    schedule = _schedule()
    job = schedule.resolve(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.", "andromeda cron list")
        return 2

    files = schedule.outputs(job)
    if not files:
        output.info(f"{job.id} has produced no output yet.")
        return 0

    if index >= len(files):
        output.fail(f"{job.id} has only {len(files)} saved run(s).")
        return 2

    chosen = files[index]
    # Straight through, unrendered: this is the model's markdown and the
    # `andromeda cron logs x > note.md` case has to produce markdown.
    output.console.print(chosen.read_text(encoding="utf-8"), highlight=False, markup=False)
    if len(files) > 1:
        output.info(f"\n  {index + 1}/{len(files)} · {chosen}")
    return 0


def remove(identifier: str) -> int:
    job = _schedule().remove(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2
    output.ok(f"Removed {job.id} — {job.name}")
    return 0


def enable(identifier: str, enabled: bool) -> int:
    schedule = _schedule()
    job = schedule.set_enabled(identifier, enabled)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2
    output.ok(f"{job.id} is now {'enabled' if enabled else 'disabled'}.")
    return 0


def resume(identifier: str) -> int:
    schedule = _schedule()
    job = schedule.resume(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2
    output.ok(f"{job.id} resumed · next {_when(job.next_run_at)}")
    return 0


def approve(identifier: str, approval: str) -> int:
    """Widen what a job may do. The only widening path, and a person types it."""
    schedule = _schedule()
    job = schedule.resolve(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2

    if approval == "auto":
        # Shown before it is granted, not after. The prompt is the thing being
        # consented to, so it is printed verbatim — the same rule the approval
        # gate follows for a command.
        output.console.print(f"  [dim]{job.prompt or job.script}[/dim]")
        output.console.print(
            "  [yellow]This job will be able to write files and run shell "
            f"commands in {job.workspace}, unattended.[/yellow]"
        )

    try:
        schedule.approve(job.id, approval)
    except ScheduleError as exc:
        output.fail(str(exc))
        return 2
    output.ok(f"{job.id} now runs with approval: {approval}")
    return 0


def notepad(identifier: str, action: str, key: str = "", value: str = "") -> int:
    schedule = _schedule()
    job = schedule.resolve(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2

    pad = _notepad()
    if action == "list":
        page = pad.page(job.id)
        if not page:
            output.info(f"{job.id} has an empty notepad.")
            return 0
        for note_key, note_value in sorted(page.items()):
            output.console.print(f"  [cyan]{note_key}[/cyan]  [dim]{note_value}[/dim]")
        return 0

    if action == "set":
        try:
            pad.set(job.id, key, value)
        except Exception as exc:  # noqa: BLE001 - NotepadError and nothing else
            output.fail(str(exc))
            return 2
        output.ok(f"{job.id}: {key} = {value}")
        return 0

    if action == "forget":
        output.ok("Forgotten." if pad.forget(job.id, key) else f"No note {key!r}.")
        return 0

    if action == "clear":
        output.ok(f"Cleared {pad.clear(job.id)} note(s).")
        return 0

    output.fail(f"Unknown action {action!r}.", "set, list, forget or clear")
    return 2


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _build_for(notepad_store: Notepad):
    """The thing `runner.execute` calls to get one answer.

    A thin wrapper rather than `build_conversation` itself: the runner's
    contract is "give me something with `.send(prompt)`", which keeps
    `andromeda_agent` from importing the CLI's session assembly. The notepad
    and the job id go straight through — a scheduled run is a session with one
    extra tool bound to it, not a different kind of thing.
    """

    def build(settings: dict, workspace: str, job: Job):
        provider = build_provider(settings)
        conversation, _record = build_conversation(
            settings,
            provider,
            interactive=False,
            workspace_root=workspace,
            notepad=notepad_store,
            job_id=job.id,
        )

        class _Turn:
            def send(self, prompt: str) -> str:
                return conversation.send(prompt, Callbacks())

        return _Turn()

    return build


def execute(
    job: Job, config: dict, schedule: Schedule | None = None, source: str = "manual"
) -> Run:
    """One run, bracketed by a durable ledger entry.

    The ledger row is written *before* anything with a side effect and closed
    after, so a scheduler killed mid-job leaves a row that says so. Without it,
    "never ran" and "ran, did the thing, died before recording it" are the same
    empty history — and guessing between them is how a job gets done twice.
    """
    schedule = schedule or _schedule()
    pad = _notepad()
    ledger = _ledger()
    attempt = ledger.claim(job.id, source=source)
    ledger.running(attempt)
    try:
        run = runner_module.execute(
            job,
            schedule,
            config,
            config_module.home(),
            build=_build_for(pad),
            notepad=pad,
        )
    except BaseException as exc:  # noqa: BLE001 - the ledger must close either way
        ledger.finish(attempt, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    ledger.finish(attempt, ok=run.status != "failed", error=run.error)
    _attach(job, run)
    return run


def _attach(job: Job, run: Run) -> None:
    """Append a run to the session it was created from, when it names one.

    So a scheduled follow-up shows up in `andromeda --resume` next to the
    conversation that asked for it, rather than only in a directory somebody
    has to remember exists. Best-effort: a missing session is not a failed job.
    """
    if not job.attach_to or run.status in {"no_change", "silent"}:
        return
    try:
        record = sessions_store.resolve(job.attach_to)
        if record is None:
            return
        body = run.summary if run.status != "failed" else run.error
        record.messages = [
            *record.messages,
            {"role": "user", "content": f"[scheduled job {job.name} ran]"},
            {"role": "assistant", "content": body or "(no output)"},
        ]
        record.save()
    except Exception:  # noqa: BLE001 - never fail a run over its transcript copy
        pass


def run_now(identifier: str) -> int:
    schedule = _schedule()
    job = schedule.resolve(identifier)
    if job is None:
        output.fail(f"No job matching {identifier!r}.")
        return 2

    output.info(f"Running {job.id} — {job.name}")
    run = execute(job, config_module.load(), schedule, source="manual")
    schedule.record(job, run)

    took = int(run.finished_at - run.started_at)
    if run.status == "no_change":
        output.info(f"  nothing changed ({took}s) — the agent did not run")
        return 0
    if run.status == "silent":
        # Two ways to reach this and they deserve different words: a script
        # that printed nothing, and an agent that said it had nothing worth
        # waking anyone for. Reporting the first for the second reads as a bug.
        reason = "the script printed nothing" if job.no_agent else "nothing worth reporting"
        output.info(f"  {reason} ({took}s)")
        return 0
    if run.ok:
        output.ok(f"Finished in {took}s")
        output.console.print(run.summary, highlight=False)
        return 0
    output.fail(run.error or "The job failed.")
    return 1


def daemon(once: bool = False) -> int:
    """Fire jobs as they come due.

    A foreground loop rather than a forked background process: `launchd` and
    `systemd` both want to own the process they supervise, and a tool that
    daemonises itself underneath them is a tool with two ideas about whether it
    is running. `andromeda cron install` writes the supervisor's file.
    """
    try:
        with exclusive(_lock_path()):
            return _tick_forever(once)
    except SchedulerBusy as exc:
        output.fail(
            str(exc),
            "Two schedulers fire every job twice. Stop the other one first.",
        )
        return 2


def _tick_forever(once: bool) -> int:
    provider = providers_cron.get(str(config_module.load().get("cron_provider") or ""))
    output.ok("Scheduler running. Ctrl-C to stop.")
    output.info(f"  {len(_schedule().all())} job(s) · checking every {TICK_SECONDS}s")
    output.info(f"  {provider.describe()}")

    # Attempts left in flight by a previous scheduler that did not exit
    # cleanly. Marked `unknown` rather than retried: whether their side effects
    # ran is genuinely not knowable, and both guesses are wrong in a way
    # somebody notices.
    recovered = _ledger().recover()
    if recovered:
        output.info(
            f"  {recovered} attempt(s) from a previous run left in an unknown "
            "state — `andromeda cron executions`"
        )

    while True:
        try:
            # Reloaded each tick, so `cron add` in another terminal takes effect
            # without a restart.
            schedule = _schedule()
            heartbeat(_heartbeat_path())
            config = config_module.load()

            for job in provider.due(schedule):
                late = job.lateness()
                if job.missed():
                    # Said out loud rather than silently absorbed. A job that
                    # was six hours late because the laptop was shut produced a
                    # report about a six-hour-old world, and the person reading
                    # it should know that.
                    output.info(
                        f"  {job.id} was due {_ago(late)} — running once, not "
                        "once per missed interval"
                    )
                output.info(
                    f"{datetime.now().strftime('%H:%M:%S')}  running {job.id} — {job.name}"
                )
                run = execute(job, config, schedule, source="schedule")
                provider.after_run(schedule, job, run)
                _report(job, run)

            if once:
                return 0
            time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            output.console.print()
            output.info("Scheduler stopped.")
            return 0


def _report(job: Job, run: Run) -> None:
    took = int(run.finished_at - run.started_at)
    if run.status == "no_change":
        output.info(f"  {job.id} unchanged — no model call")
    elif run.status == "silent":
        output.info(
            f"  {job.id} "
            + ("printed nothing" if job.no_agent else "had nothing to report")
        )
    elif run.ok:
        output.ok(f"  {job.id} finished in {took}s")
    else:
        output.fail(f"  {job.id}: {run.error}")
        if job.paused_reason:
            output.console.print(f"  [yellow]{job.paused_reason}[/yellow]")


# ---------------------------------------------------------------------------
# Suggestions — automations proposed, never created
# ---------------------------------------------------------------------------


def suggest_list(seed: bool = True) -> int:
    store = _suggestions()
    if seed:
        _seed(store)

    pending = store.pending()
    if not pending:
        output.info("Nothing suggested.")
        output.info("  andromeda cron blueprint     # the automations on offer")
        return 0

    for position, suggestion in enumerate(pending, start=1):
        output.console.print(
            f"  [cyan]{position}[/cyan]  {suggestion.title}  "
            f"[dim]{suggestion.source}[/dim]"
        )
        output.console.print(f"     [dim]{suggestion.description}[/dim]")
        spec = suggestion.spec
        if spec.get("schedule"):
            output.console.print(f"     [dim]runs {spec['schedule']}[/dim]")

    output.info("\n  andromeda cron suggest accept <n>   # create it")
    output.info("  andromeda cron suggest dismiss <n>  # never offer it again")
    return 0


def _seed(store: Suggestions) -> None:
    """Refresh the proposals. Cheap, and it never creates anything.

    Wrapped because every source can fail independently — a broken skill
    frontmatter must not stop the curated ones from being offered.
    """
    from andromeda_tools import browser as browser_module
    from andromeda_tools import skills as skills_module

    capabilities: set[str] = set()
    try:
        if browser_module.playwright_available():
            capabilities.add("browser")
    except Exception:  # noqa: BLE001
        pass

    try:
        seeding_module.seed_all(
            store,
            skills=skills_module.discover(Path.cwd()),
            prompts=_recent_prompts(),
            capabilities=capabilities,
        )
    except Exception:  # noqa: BLE001 - a suggestion engine is not worth a crash
        pass


def _recent_prompts(limit: int = 200) -> list[str]:
    """What the person has actually asked for, across sessions.

    Read from the saved transcripts rather than inferred: the `usage` source
    proposes automation for a thing said repeatedly, and "repeatedly" needs
    evidence that outlives one session.
    """
    prompts: list[str] = []
    for record in sessions_store.recent(limit=50):
        for message in record.messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                prompts.append(message["content"])
            if len(prompts) >= limit:
                return prompts
    return prompts


def suggest_accept(reference: str, workspace: str | None = None) -> int:
    store = _suggestions()
    schedule = _schedule()
    root = str(Path(workspace).expanduser().resolve() if workspace else Path.cwd())

    suggestion = store.resolve(reference)
    if suggestion is None:
        output.fail(f"No suggestion matching {reference!r}.", "andromeda cron suggest")
        return 2

    # A suggestion whose spec is a bare blueprint reference genuinely needs
    # values from the person. Sending them to the blueprint is a better answer
    # than inventing a URL for them.
    if suggestion.spec.get("blueprint") and "schedule" not in suggestion.spec:
        key = suggestion.spec["blueprint"]
        output.info(f"{suggestion.title} needs a few details first:")
        return blueprint_show(key)

    try:
        _accepted, job = store.accept(reference, schedule, root)
    except ScheduleError as exc:
        output.fail(str(exc))
        return 2
    if job is None:
        output.fail(f"No suggestion matching {reference!r}.")
        return 2

    _describe_new(job)
    return 0


def suggest_dismiss(reference: str) -> int:
    suggestion = _suggestions().dismiss(reference)
    if suggestion is None:
        output.fail(f"No suggestion matching {reference!r}.")
        return 2
    output.ok(f"Dismissed {suggestion.title}. It will not be offered again.")
    return 0


# ---------------------------------------------------------------------------
# Blueprints — an automation as a form, not as cron syntax
# ---------------------------------------------------------------------------


def blueprint_list() -> int:
    by_category: dict[str, list] = {}
    for blueprint in blueprints_module.CATALOG:
        by_category.setdefault(blueprint.category, []).append(blueprint)

    for category, entries in sorted(by_category.items()):
        output.console.print(f"\n  [dim]{category}[/dim]")
        for blueprint in entries:
            output.console.print(f"    [cyan]{blueprint.key.ljust(16)}[/cyan] {blueprint.title}")
            output.console.print(f"    {' ' * 16} [dim]{blueprint.description}[/dim]")
    output.info("\n  andromeda cron blueprint show <key>")
    return 0


def blueprint_show(key: str) -> int:
    blueprint = blueprints_module.get(key)
    if blueprint is None:
        output.fail(f"No blueprint {key!r}.", "andromeda cron blueprint")
        return 2

    output.console.print(f"  [bold]{blueprint.title}[/bold]")
    output.console.print(f"  [dim]{blueprint.description}[/dim]\n")
    for slot in blueprint.slots:
        default = f" [dim](default {slot.default})[/dim]" if slot.default else ""
        required = "" if slot.optional else " [dim]required[/dim]"
        output.console.print(f"  [cyan]--{slot.name}[/cyan]  {slot.label}{default}{required}")
        if slot.options:
            output.console.print(f"      [dim]{', '.join(map(str, slot.options))}[/dim]")
        if slot.help:
            output.console.print(f"      [dim]{slot.help}[/dim]")

    output.console.print(f"\n  [dim]{blueprints_module.command_for(blueprint)}[/dim]")
    return 0


def blueprint_use(key: str, values: list[str], workspace: str | None = None) -> int:
    blueprint = blueprints_module.get(key)
    if blueprint is None:
        output.fail(f"No blueprint {key!r}.", "andromeda cron blueprint")
        return 2

    parsed: dict[str, str] = {}
    for item in values:
        name, _, value = item.partition("=")
        if not value:
            output.fail(f"{item!r} is not name=value.", "andromeda cron blueprint show " + key)
            return 2
        parsed[name.lstrip("-")] = value

    try:
        spec = blueprints_module.fill(blueprint, parsed)
    except blueprints_module.BlueprintError as exc:
        output.fail(str(exc))
        return 2

    root = str(Path(workspace).expanduser().resolve() if workspace else Path.cwd())
    schedule_expression = spec.pop("schedule")
    prompt = spec.pop("prompt")
    try:
        job = _schedule().add(schedule_expression, prompt, root, **spec)
    except ScheduleError as exc:
        output.fail(str(exc))
        return 2

    _describe_new(job)
    return 0


# ---------------------------------------------------------------------------
# Executions — what was in flight when the machine went down
# ---------------------------------------------------------------------------


def executions(identifier: str = "", unresolved_only: bool = False) -> int:
    ledger = _ledger()
    schedule = _schedule()

    job_id = ""
    if identifier:
        job = schedule.resolve(identifier)
        if job is None:
            output.fail(f"No job matching {identifier!r}.")
            return 2
        job_id = job.id

    rows = ledger.unresolved() if unresolved_only else ledger.recent(job_id)
    if not rows:
        output.info("No attempts recorded.")
        return 0

    names = {job.id: job.name for job in schedule.all()}
    marks = {
        "completed": "[green]✓[/green]",
        "failed": "[red]✗[/red]",
        "unknown": "[yellow]?[/yellow]",
        "running": "[cyan]•[/cyan]",
        "claimed": "[dim]·[/dim]",
    }
    for row in rows:
        when = str(row["claimed_at"])[:19].replace("T", " ")
        name = names.get(row["job_id"], row["job_id"])
        output.console.print(
            f"  {marks.get(row['status'], '?')} [dim]{when}[/dim]  "
            f"{name[:28].ljust(28)}  [dim]{row['status']} · {row['source']}[/dim]"
        )
        if row["error"]:
            output.console.print(f"      [dim]{str(row['error'])[:100]}[/dim]")

    unknown = [row for row in rows if row["status"] == "unknown"]
    if unknown:
        output.console.print(
            f"\n  [yellow]{len(unknown)} attempt(s) ended in an unknown state — "
            "whether their side effects ran is not knowable.[/yellow]"
        )
    return 0
