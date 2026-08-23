"""One run of one job.

Everything a scheduled job can be composes here, in the order that makes each
part able to save the work of the next:

  1. **The monitor.** A cheap source runs first. If what it says has not
     changed, the run stops here and nothing else in this list happens — no
     script, no model call, no delivery. That ordering is the whole economic
     argument for monitor mode.
  2. **The script.** With `no_agent` it *is* the job: its stdout is the output
     and there is no model call at all. Otherwise its stdout becomes facts in
     the prompt.
  3. **The chain.** `context_from` pulls the most recent real output of other
     jobs in, so one job can collect and another can reason.
  4. **The notepad.** What this job remembered last time.
  5. **The agent**, finally, on a prompt assembled from all of the above.
  6. **The record.** The output file is written whatever happened; delivery is
     only about being told.

`build` is injected rather than imported so this module does not depend on the
CLI's session assembly — the same shape `evals.run_scenario` uses, and for the
same reason: it makes a run testable without a model, and it keeps
`andromeda_agent` from importing `andromeda_cli`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol

from . import delivery as delivery_module
from . import monitor as monitor_module
from . import scripts as scripts_module
from .errors import AgentError
from .notepad import Notepad
from .schedule import Job, Run, Schedule

MAX_SUMMARY = 2_000
MAX_CHAINED_CHARS = 8_000


class Builder(Protocol):
    """Makes something that can answer one prompt for this job."""

    def __call__(
        self, settings: dict[str, Any], workspace: str, job: Job
    ) -> Any: ...


def context_blocks(
    job: Job, schedule: Schedule, notepad: Notepad, home: Path, config: dict[str, Any]
) -> tuple[list[str], Run | None]:
    """Everything prepended to the job's prompt, and a failure if one happened.

    Returned as a list rather than a joined string so the caller can say which
    part was empty when a job does nothing — "the script produced no output" is
    a much better report than "the job found nothing".
    """
    blocks: list[str] = []

    if job.script and not job.no_agent:
        result = scripts_module.run(home, job.script, workspace=job.workspace)
        if not result.ok:
            return blocks, _failed(job, f"{job.script}: {result.error}")
        if result.output.strip():
            blocks.append(
                f"Output of `{job.script}`, run just now for this job:\n\n{result.output}"
            )

    for other_id in job.context_from:
        other = schedule.resolve(other_id)
        if other is None:
            # Named and missing is worth saying. Silently dropping it gives a
            # job that quietly reasons about less than it was told to.
            blocks.append(f"[no job matching {other_id!r} — its output is missing]")
            continue
        latest = other.last_output
        if latest is None:
            blocks.append(f"[{other.name} has not produced output yet]")
            continue
        blocks.append(
            f"Most recent output of `{other.name}`:\n\n"
            + _read_output(latest, schedule)[:MAX_CHAINED_CHARS]
        )

    page = notepad.render(job.id)
    if page:
        blocks.append(page)

    return blocks, None


def _read_output(run: Run, schedule: Schedule) -> str:
    """The full output if it is still on disk, else the excerpt."""
    if run.output_path:
        try:
            return Path(run.output_path).read_text(encoding="utf-8")
        except OSError:
            pass
    return run.summary


def _failed(job: Job, message: str, started: float | None = None) -> Run:
    started = time.time() if started is None else started
    return Run(
        started_at=started,
        finished_at=time.time(),
        ok=False,
        status="failed",
        error=message[:500],
        late_by=job.lateness(started),
    )


def execute(
    job: Job,
    schedule: Schedule,
    config: dict[str, Any],
    home: Path,
    *,
    build: Builder,
    notepad: Notepad | None = None,
) -> Run:
    """Run the job once and return what happened. Never raises."""
    started = time.time()
    late = job.lateness(started)
    notepad = notepad or Notepad(Path(home) / "cron" / "notepad.json")
    monitor_block = ""
    monitor_text = ""

    # ---- 1. the monitor ---------------------------------------------------
    if job.is_monitored:
        sample = monitor_module.read(
            job.monitor_kind,
            job.monitor_source,
            Path(home),
            workspace=job.workspace,
            allow_private_network=bool(config.get("allow_private_network")),
        )
        if not sample.ok:
            # A source that failed is an error, never a change. Leaving the
            # stored hash alone means a source that recovers to what it said
            # before still suppresses, instead of announcing a change that
            # never happened.
            return _failed(job, f"monitor source failed: {sample.error}", started)

        if sample.digest == job.monitor_hash:
            return Run(
                started_at=started,
                finished_at=time.time(),
                ok=True,
                status="no_change",
                summary="the watched source is unchanged",
                late_by=late,
            )

        previous = ""
        cache = schedule.monitor_cache(job)
        try:
            previous = cache.read_text(encoding="utf-8")
        except OSError:
            previous = ""
        monitor_text = sample.text
        monitor_block = monitor_module.change_block(previous, monitor_text)

    # ---- 2. a script job needs no model -----------------------------------
    if job.no_agent:
        run = _script_job(job, home, started, late)
    else:
        run = _agent_job(
            job, schedule, config, home, notepad, monitor_block, started, late, build
        )

    # ---- the monitor baseline moves only on a run that worked -------------
    #
    # Storing it before the agent ran would mean a change that happened to make
    # the job fail is never seen again: the hash advances, the next tick
    # compares equal, and the thing you were watching changed in silence. It
    # can re-fire forever instead, which is bounded by the consecutive-failure
    # auto-pause.
    if job.is_monitored and run.status in {"ok", "silent"}:
        job.monitor_hash = monitor_module.digest(monitor_text)
        job.monitor_changed_at = started
        try:
            cache = schedule.monitor_cache(job)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(monitor_text, encoding="utf-8")
        except OSError:
            # The hash is the source of truth for suppression; the cached text
            # only makes the next diff readable. Losing it costs a diff, not a
            # detection.
            pass

    # ---- the silence contract ---------------------------------------------
    #
    # A job that reports "nothing to report" every hour trains the person to
    # ignore it, and then it is worse than not existing. The prompt asks for a
    # marker; a run that emits one is recorded in full and delivers nothing.
    if run.status == "ok" and delivery_module.is_silence(run.summary):
        run.status = "silent"

    # ---- 6. the record ----------------------------------------------------
    body = run.summary if run.status != "failed" else run.error
    if run.status != "no_change":
        try:
            path = delivery_module.write_output(
                schedule.output_path(job, started), job.name, run.status, body, started
            )
            run.output_path = str(path)
        except OSError as exc:
            # The run happened. Failing it because the log could not be written
            # would throw away work that succeeded.
            run.summary = f"{run.summary}\n\n[output file could not be written: {exc}]"

    # A `silent` run is one that deliberately found nothing to say. Announcing
    # it is how a watchdog becomes something people mute.
    if run.status in {"ok", "failed"} and job.deliver != "none":
        delivery_module.deliver(
            job.deliver, job.name, body, ok=run.ok, target=job.deliver_target
        )

    return run


def _script_job(job: Job, home: Path, started: float, late: float) -> Run:
    result = scripts_module.run(home, job.script, workspace=job.workspace)
    if not result.ok:
        return _failed(job, f"{job.script}: {result.error}", started)

    output = result.output.strip()
    if not output:
        # Empty stdout is silence, by contract. A watchdog that reports every
        # time it finds nothing is a watchdog people stop reading, and then it
        # is worse than no watchdog at all.
        return Run(
            started_at=started,
            finished_at=time.time(),
            ok=True,
            status="silent",
            summary="",
            late_by=late,
        )

    return Run(
        started_at=started,
        finished_at=time.time(),
        ok=True,
        status="ok",
        summary=result.output[:MAX_SUMMARY],
        late_by=late,
    )


def _agent_job(
    job: Job,
    schedule: Schedule,
    config: dict[str, Any],
    home: Path,
    notepad: Notepad,
    monitor_block: str,
    started: float,
    late: float,
    build: Builder,
) -> Run:
    blocks, failure = context_blocks(job, schedule, notepad, Path(home), config)
    if failure is not None:
        failure.started_at = started
        failure.late_by = late
        return failure

    if job.skills:
        # Named rather than loaded here: `skill_load` reads the body, and
        # pre-loading every skill's full text would put it in the prompt whether
        # the job needed it or not. This tells the job which ones are its own.
        blocks.append(
            "Skills for this job, to load with `skill_load` before you start: "
            + ", ".join(job.skills)
        )

    if monitor_block:
        # First, ahead of everything else: it is the reason this run is
        # happening at all.
        blocks.insert(0, monitor_block)

    prompt = "\n\n---\n\n".join([*blocks, job.prompt]) if blocks else job.prompt

    # The job's recorded approval mode replaces whatever the config says, so a
    # job created read-only stays read-only even if the person has since set
    # `approval_mode: auto` for their own interactive sessions. Consent belongs
    # to the job, not to the machine.
    settings = {**config, "approval_mode": job.approval_mode}
    # Per-job overrides, applied last so they beat both the file and the
    # environment — a job's settings are the most local statement of intent
    # about that job, exactly as a flag is for an invocation. An empty field
    # means "whatever the session would use", never "the default".
    if job.model:
        settings["model"] = job.model
    if job.thinking:
        settings["thinking"] = job.thinking
    if job.enabled_tools:
        # Intersected, never replaced. A job cannot name a tool the machine has
        # switched off and get it back — the same subtract-only rule
        # `Policy.narrow` enforces for a delegated lane.
        settings["enabled_tools"] = [
            name for name in config.get("enabled_tools", []) if name in set(job.enabled_tools)
        ]

    try:
        conversation = build(settings, job.workspace, job)
        answer = conversation.send(prompt)
    except AgentError as exc:
        return _failed(job, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - a failed job must not stop the daemon
        return _failed(job, f"{type(exc).__name__}: {exc}", started)

    return Run(
        started_at=started,
        finished_at=time.time(),
        ok=True,
        status="ok",
        summary=(answer or "").strip()[:MAX_SUMMARY],
        late_by=late,
        used_model=True,
    )
