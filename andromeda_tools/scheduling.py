"""What the agent can do about time.

Two tools, registered in two different places for two different reasons.

**`notepad`** exists only inside a scheduled run, bound to the job that is
running. It is how a job carries a cursor to its next wake-up. It is not
registered in an interactive session because there is no job to write to, and a
tool that exists but always errors teaches the model to keep trying it.

**`cron`** exists in an interactive session and is how the agent schedules
follow-up work. Its whole design turns on one rule:

  **An agent may propose autonomy. Only a person grants the unattended kind.**

That is the approval gate's "a child is never more permissive than its parent",
applied to time instead of to depth. A scheduled job runs in a context the
person is not in, so a job the agent creates can never be handed more than the
narrowed, read-only belt a non-interactive run gets anyway — `Schedule.add`
refuses `auto` outright for `origin="agent"`, and this tool cannot even ask for
it. Widening is `andromeda cron approve`, typed by someone who read the job
first.

Deliberately absent: **`run`**. Triggering a job synchronously from inside a
turn means an agent turn nested inside an agent turn, sharing a workspace and a
budget with nothing supervising the pair — which then needs a heartbeat
mechanism to keep the parent's watchdog at bay. A person types
`andromeda cron run <id>`; the agent does not.
"""

from __future__ import annotations

from typing import Any

from .spec import ToolResult, ToolSpec, failure

# `Notepad` and `Schedule` are typed loosely and imported inside the executors,
# never at module scope. `andromeda_agent` imports `andromeda_tools` for the
# risk tiers, so a top-level import here closes the cycle at import time — the
# same reason `loop.py` types its lane registry as `Any` and `config.py` pulls
# the model allowlist in from inside a function. By the time an executor runs,
# both packages are fully imported and the import is a dict lookup.

NOTEPAD_DESCRIPTION = (
    "Your notepad for THIS scheduled job. It is the only thing that survives "
    "to the next run — everything else starts fresh. Use it for cursors and "
    "watermarks: the id of the last item you handled, the timestamp you read "
    "up to, the short list you are still watching. Do not use it to cache "
    "data; store the marker, not what it points at. Actions: 'set' (write a "
    "note), 'get' (read one), 'list' (read all), 'forget' (remove one)."
)

CRON_DESCRIPTION = (
    "Schedule work to happen later, without the user present. Actions: "
    "'create', 'list', 'show', 'pause', 'resume', 'remove'.\n\n"
    "PREFER A WATCH SOURCE. If what the job checks can be read by a script or a "
    "URL, pass `watch` or `watch_url`: it runs first, and when its output has "
    "not changed you do not run at all. A job that polls inside its prompt "
    "costs a full turn every tick to conclude nothing happened.\n\n"
    "Schedules: 'every 30m', a five-field cron expression like '0 9 * * 1-5', "
    "'in 2h', or 'at 2026-09-01T09:00'. The last two run once.\n\n"
    "The prompt must be SELF-CONTAINED. It runs in a fresh session with none "
    "of this conversation, so 'check that file again' will fail — say which "
    "file.\n\n"
    "A job you create is read-only: it can look and report, and it cannot "
    "write files or run commands. If the work needs more than that, create it "
    "anyway and tell the user to run `andromeda cron approve <id> --approval "
    "auto` — granting a job the ability to change things unattended is their "
    "decision, not yours.\n\n"
    "Use this when the user asks for something recurring or for later. Do not "
    "use it to remember a fact (that is memory_store) or to do something now."
)


def notepad_spec(notepad: Any, job_id: str) -> ToolSpec:
    def run(action: str, key: str = "", value: str = "") -> ToolResult:
        from andromeda_agent.notepad import NotepadError

        action = (action or "").strip().lower()

        if action == "list":
            page = notepad.page(job_id)
            if not page:
                return ToolResult(content="Your notepad is empty.", display="notepad: empty")
            body = "\n".join(f"{k}: {v}" for k, v in sorted(page.items()))
            return ToolResult(content=body, display=f"notepad: {len(page)} note(s)")

        if action == "get":
            if not key:
                return failure("`get` needs a key.")
            found = notepad.get(job_id, key)
            return ToolResult(
                content=found or f"No note called {key!r}.", display=f"read {key}"
            )

        if action == "set":
            if not key:
                return failure("`set` needs a key.")
            try:
                notepad.set(job_id, key, value)
            except NotepadError as exc:
                return failure(str(exc))
            return ToolResult(content=f"Noted {key}.", display=f"note {key} = {value[:60]}")

        if action == "forget":
            if not key:
                return failure("`forget` needs a key.")
            removed = notepad.forget(job_id, key)
            return ToolResult(
                content="Forgotten." if removed else f"No note called {key!r}.",
                display=f"forget {key}",
            )

        return failure(f"Unknown action {action!r}. Use set, get, list or forget.")

    return ToolSpec(
        name="notepad",
        description=NOTEPAD_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "get", "list", "forget"],
                    "description": "What to do with the notepad.",
                },
                "key": {"type": "string", "description": "The note's name."},
                "value": {"type": "string", "description": "The note's contents, for 'set'."},
            },
            "required": ["action"],
        },
        # Same tier and category as `memory_store`, for the same reason: this
        # is durable state the agent writes, and gating it would mean a job
        # running in the narrowed non-interactive belt — which is every job in
        # the default mode — could never keep a cursor at all.
        risk_tier="safe_local",
        category="write",
        run=run,
        summarize=lambda arguments: (
            f"notepad {arguments.get('action', '')} {arguments.get('key', '')}".strip()
        ),
    )


def _describe(job) -> str:
    parts = [f"{job.id}  {job.name}", f"  runs {job.schedule} · {job.state}"]
    if job.repeat:
        parts.append(f"  {job.runs_done}/{job.repeat} runs done")
    if job.is_monitored:
        parts.append(f"  watches {job.monitor_kind} {job.monitor_source}")
    parts.append(f"  approval {job.approval_mode} · created by {job.origin}")
    # Where it runs, so a report about "your scheduled jobs" cannot claim a
    # local job survives a closed laptop, or that a hosted one is stoppable
    # from here.
    # An `auto` job has no single answer, so it reports where the next fire
    # goes and says that it can move. Reporting the *declared* location would
    # have said "this machine" for a job that has run in the cloud all week.
    resolved = job.placement()
    where = "a hosted runner" if resolved == "cloud" else "this machine"
    if job.runs_on == "auto":
        where += " (auto)"
    reach = "no filesystem" if job.workspace_kind == "detached" else job.workspace
    parts.append(f"  runs on {where} · reaches {reach}")
    last = job.last_run
    if last is not None:
        parts.append(f"  last {last.status}: {(last.summary or last.error)[:160]}")
    return "\n".join(parts)


def _job_session(name: str, workspace_root: str, created_in: str) -> str:
    """A transcript of the job's own, or nothing.

    Best-effort by construction. A session store that cannot be written to is
    not a reason to refuse to create the job — it just means the runs are
    reachable through `andromeda cron runs` rather than as a conversation.
    Falling back to the *creating* session would be worse than falling back to
    none: that is the interleaving this exists to stop.
    """
    try:
        from andromeda_cli import sessions as sessions_store

        return sessions_store.for_job(name, workspace_root, created_in).id
    except Exception:  # noqa: BLE001 - never fail a job over its transcript
        return ""


def cron_spec(schedule: Any, workspace_root: str, session_id: str = "") -> ToolSpec:
    def run(
        action: str,
        prompt: str = "",
        cron_schedule: str = "",
        name: str = "",
        job_id: str = "",
        repeat: int = 0,
        watch: str = "",
        watch_url: str = "",
        deliver: str = "none",
        needs_files: bool = False,
    ) -> ToolResult:
        from andromeda_agent.schedule import ScheduleError

        action = (action or "").strip().lower()

        if action == "list":
            jobs = schedule.all()
            if not jobs:
                return ToolResult(content="No scheduled jobs.", display="0 jobs")
            return ToolResult(
                content="\n\n".join(_describe(job) for job in jobs),
                display=f"{len(jobs)} job(s)",
            )

        if action in {"show", "pause", "resume", "remove"}:
            if not job_id:
                return failure(f"`{action}` needs a job_id. Call list first.")
            job = schedule.resolve(job_id)
            if job is None:
                return failure(f"No job matching {job_id!r}.")

            if action == "show":
                return ToolResult(content=_describe(job), display=f"show {job.id}")
            if action == "remove":
                schedule.remove(job.id)
                return ToolResult(content=f"Removed {job.id}.", display=f"remove {job.id}")
            if action == "pause":
                schedule.pause(job.id, "paused by the agent")
                return ToolResult(content=f"Paused {job.id}.", display=f"pause {job.id}")
            schedule.resume(job.id)
            return ToolResult(content=f"Resumed {job.id}.", display=f"resume {job.id}")

        if action == "create":
            if not cron_schedule:
                return failure("`create` needs a schedule, e.g. 'every 1h' or 'in 2h'.")
            if not prompt:
                return failure("`create` needs a self-contained prompt.")
            if watch and watch_url:
                return failure("Give one watch source, not both.")
            try:
                job = schedule.add(
                    cron_schedule,
                    prompt,
                    workspace_root,
                    name=name,
                    # Not a parameter. The tool cannot ask for `auto`, so there
                    # is no argument for the model to get wrong and no argument
                    # for a prompt injection to set.
                    approval_mode="ask",
                    repeat=int(repeat or 0),
                    # Exposed because the monitored shape is the *cheaper and
                    # safer* one, and a tool that can only create the expensive
                    # shape pushes every agent-made job into it. It grants
                    # nothing extra: a watch source only ever removes runs.
                    monitor_kind="script" if watch else ("url" if watch_url else ""),
                    monitor_source=watch or watch_url,
                    deliver=deliver if deliver in {"none", "notify", "stdout"} else "none",
                    origin="agent",
                    # Not "where does this run" — the model has no way to know
                    # that and it is not its call. The question it *can* answer
                    # is whether the job has to see this machine's files, and
                    # that is what decides placement at fire time.
                    #
                    # Most jobs do not: watching a deploy, checking an inbox,
                    # polling an API. Those get a detached workspace and run in
                    # the cloud, which is the point — a watcher earns its keep
                    # on the nights the lid is shut.
                    workspace_kind="device" if needs_files else "detached",
                    runs_on="auto",
                    # The job's OWN transcript, created here, not the
                    # conversation that asked for it. Attaching to the caller
                    # read well in the design and badly in use: a job polling
                    # every five minutes wrote a pair of messages into a *live*
                    # conversation every five minutes, interleaved with what
                    # the person was actually saying.
                    #
                    # Neither is a parameter. The model does not choose which
                    # conversation it is having, and a session id it could set
                    # would be a way to write into somebody else's transcript.
                    #
                    # This does NOT make the job read either session. The
                    # prompt stays self-contained — a job that re-read its
                    # whole thread would grow its own cost on every run, and
                    # would break the day the session was pruned.
                    attach_to=_job_session(name or "job", workspace_root, session_id),
                    created_in=session_id,
                )
            except ScheduleError as exc:
                return failure(str(exc))
            # The scheduler arms itself here rather than being homework.
            # Creating a job *is* the request for one, and a job that looks
            # scheduled but silently never fires is the worst outcome
            # available — the person finds out days later by noticing they
            # were never told anything.
            armed = False
            running = False
            try:
                from andromeda_cli.commands import service as service_module

                armed = service_module.ensure_installed()
                running = service_module.is_installed()
            except Exception:  # noqa: BLE001 - never fail a good job over its supervisor
                pass

            content = f"Scheduled {job.id} — {job.name}\n{_describe(job)}\n\n"
            where = job.placement()
            content += (
                "It will run in the cloud, so it keeps working when the laptop "
                "is shut.\n\n"
                if where == "cloud"
                else "It needs this machine's files, so it only fires while "
                "this machine is awake.\n\n"
            )
            if job.attach_to:
                # Told once, here, and never again. This is the only moment the
                # person can be handed the thread their job will talk in — and
                # without it the job's output lives somewhere they have no way
                # to find. Say it plainly rather than burying it.
                # The command is written out because the surface turns it into
                # a clickable row — see `Transcript.link_sessions_in`. Telling
                # the model to instruct somebody to *type* it is the thing to
                # avoid: the id is already on screen, and asking a person to
                # select, copy, leave the app and paste is asking them to be a
                # terminal.
                content += (
                    f"Its runs go to their own conversation, not this one, so "
                    f"they will not interrupt what we are doing. Mention that "
                    f"in one short sentence and include this line exactly once "
                    f"— the interface turns it into a clickable link, so do "
                    f"NOT tell the user to run or type it:\n"
                    f"  andromeda --resume {job.attach_to}\n\n"
                )
            if armed:
                content += "The background scheduler was started. "
            if running:
                content += (
                    "It is running and will fire on its own — say so plainly, "
                    "and do NOT tell the user to run any command to make it "
                    "work.\n\n"
                )
            else:
                # The honest branch. Claiming a job will fire when nothing is
                # there to fire it is the exact failure the auto-install exists
                # to prevent, and it would be worse coming from a confident
                # sentence than from silence.
                content += (
                    "NOTE: no background scheduler is installed on this machine "
                    "and one could not be started, so this job will not fire "
                    "until one runs. Tell the user that plainly rather than "
                    "implying it is live.\n\n"
                )
            content += (
                "It is read-only: it can look at things and report, but cannot "
                "change files or run commands. That is usually right. Mention "
                "the wider grant ONLY if this job actually needs to act — and "
                "then say `/approve " + job.id + "`, which works right here, "
                "never a shell command they would have to leave for."
            )
            return ToolResult(
                content=content,
                display=f"scheduled {job.id}: {job.name}",
            )

        return failure(
            f"Unknown action {action!r}. Use create, list, show, pause, resume or remove."
        )

    return ToolSpec(
        name="cron",
        description=CRON_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "show", "pause", "resume", "remove"],
                },
                "schedule": {
                    "type": "string",
                    "description": (
                        "When to run: 'every 30m', '0 9 * * 1-5', 'in 2h', or "
                        "'at 2026-09-01T09:00'. Required for 'create'."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "What the job should do. SELF-CONTAINED — it runs with "
                        "none of this conversation in front of it."
                    ),
                },
                "name": {"type": "string", "description": "A short name for the job."},
                "job_id": {
                    "type": "string",
                    "description": "Which job, for show/pause/resume/remove.",
                },
                "repeat": {
                    "type": "number",
                    "description": "Run this many times, then retire. Omit for forever.",
                },
                "watch": {
                    "type": "string",
                    "description": (
                        "A script in ~/.andromeda-cli/scripts/ to run FIRST each "
                        "tick. Its output is hashed; if it has not changed you do "
                        "not run at all, and the tick costs nothing. Use this "
                        "instead of polling in the prompt whenever what you are "
                        "watching can be read by a script. The script must emit "
                        "STABLE output — no timestamps, sorted — or every tick "
                        "looks like a change."
                    ),
                },
                "watch_url": {
                    "type": "string",
                    "description": (
                        "Same as `watch`, fetching a URL instead of running a "
                        "script. Only one of the two."
                    ),
                },
                "deliver": {
                    "type": "string",
                    "enum": ["none", "notify", "stdout"],
                    "description": (
                        "How the user hears about a run. `none` saves it for "
                        "`andromeda cron logs`; `notify` raises a desktop "
                        "notification. Output is always saved either way."
                    ),
                },
                "needs_files": {
                    "type": "boolean",
                    "description": (
                        "Whether this job must read or write files on THIS "
                        "machine. Default false. Say false for anything that "
                        "works over the network — watching a deployment, "
                        "checking an API, polling a site, reading an inbox — "
                        "so it can run while the machine is asleep, which is "
                        "when a scheduled job is worth having. Say true only "
                        "when the job genuinely needs the local filesystem, "
                        "such as tidying a directory or running this repo's "
                        "tests; that pins it to this machine and it will not "
                        "fire while the lid is shut."
                    ),
                },
            },
            "required": ["action"],
        },
        # Creating a job is creating something that will act later, with nobody
        # watching. It is read-only by construction, but "read-only" here still
        # means fetching URLs and reading the user's files on a timer, which is
        # a decision a person should be shown before it is made.
        risk_tier="destructive",
        category="admin",
        # `schedule` is the OpenAI parameter name; the executor takes it as
        # `cron_schedule` because `schedule` shadows the module imported above.
        # Renaming rather than shadowing is the point — `test_source_hygiene`
        # fails the build on a name that shadows an import.
        run=lambda action, schedule="", prompt="", name="", job_id="", repeat=0,
        watch="", watch_url="", deliver="none", needs_files=False: run(
            action, prompt, schedule, name, job_id, repeat, watch, watch_url,
            deliver, needs_files,
        ),
        summarize=lambda arguments: (
            f"schedule {arguments.get('schedule', '')!r}: "
            f"{str(arguments.get('prompt', ''))[:80]}"
            if arguments.get("action") == "create"
            else f"cron {arguments.get('action', '')} {arguments.get('job_id', '')}".strip()
        ),
    )
