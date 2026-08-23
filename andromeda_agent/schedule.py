"""Jobs that run without you.

Autonomy is the point where a harness stops being a tool you drive and starts
being one that acts. That changes what "approved" has to mean, so the rule here
is the one Andromeda already settled on elsewhere:

  **Consent is established at creation, and it must be stated.**

A scheduled job carries the approval mode it was created with. Nobody is at the
keyboard when it fires, so a prompt is not an option — either the person said,
at creation time and in as many words, what this job may do, or the job runs
with the narrowed belt a non-interactive run always gets. There is no path that
turns "I made a job" into "it may do anything".

The rest is deliberately boring: JSON on disk, a tick loop, one process per run.
A scheduler whose state you cannot read with `cat` is a scheduler you cannot
debug at 3am, which is exactly when you need to.

Three things a job can be, beyond "a prompt on a timer" — all three there for
the same reason, which is that an agent turn is the most expensive part of the
loop:

  - **A script job** (`no_agent`). The script *is* the job and its stdout is the
    output. No model call at all. Empty stdout is silence — a watchdog that
    reports every time it finds nothing is a watchdog people mute.
  - **A monitored job** (`monitor_*`). A cheap source runs first and its output
    is hashed; unchanged means the agent does not run. See `monitor.py`.
  - **A fed job** (`script`, `context_from`). Something else produces the facts
    and the agent only has to reason about them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from croniter import CroniterBadCronError, croniter

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from . import monitor as monitor_module
from .models import THINKING_LEVELS

# A job that has never run has no history to prune; one that has runs forever.
MAX_RUNS_KEPT = 50
MIN_INTERVAL_SECONDS = 60

APPROVAL_MODES = ("ask", "auto", "deny")

# How the person is told a run happened. The output file is written either way —
# `deliver` is about being *told*, never about whether the work is kept, because
# losing a job's output to a delivery setting is a bug nobody would ever guess at.
DELIVERY_MODES = ("none", "notify", "stdout", "webhook")

# Who asked for this job to exist. Load-bearing, not bookkeeping: an agent may
# propose autonomy, and only a person may grant the unattended kind. See
# `Schedule.add`.
ORIGINS = ("user", "agent")

# A run this far past its scheduled time was missed, not merely late — the
# machine was asleep or the scheduler was not running. It fires once and says
# how late it was, rather than firing once per interval it slept through.
CATCH_UP_GRACE_SECONDS = 90

# Consecutive failures before a job stops trying. A job whose credentials
# expired will fail identically forever; retrying it every minute for a week is
# a bill, and the failure is already recorded the first time.
MAX_CONSECUTIVE_FAILURES = 5


class ScheduleError(ValueError):
    pass


def is_one_shot(expression: str) -> bool:
    """`in 2h` and `at 2026-09-01T09:00` fire once and retire."""
    lowered = (expression or "").strip().lower()
    return lowered.startswith("in ") or lowered.startswith("at ")


def parse_schedule(expression: str) -> str:
    """Validate a schedule, returning the canonical form.

    Four forms, in the order people reach for them:

      `every 30m`            a repeating interval
      `0 9 * * 1-5`          a five-field cron expression
      `in 2h`                once, that far from now
      `at 2026-09-01T09:00`  once, at a wall-clock time

    The interval and the two one-shot forms exist because thinking in cron
    fields is the hard part of writing a job that only needs to run hourly, or
    once, tomorrow.
    """
    expression = " ".join((expression or "").split())
    if not expression:
        raise ScheduleError("A schedule is required.")

    if expression.lower().startswith("in "):
        # Validated now so a bad `in 2x` fails at creation rather than at the
        # first tick, in a daemon nobody is watching.
        _interval_seconds("every " + expression[3:])
        return expression.lower()

    if expression.lower().startswith("at "):
        _absolute_time(expression)
        # Only the keyword is lowered. `strptime`'s literal `T` separator is
        # not reliably case-insensitive, so canonicalising the whole string
        # would leave a stored schedule that parsed once and may not again.
        return "at " + expression[3:].strip()

    if expression.lower().startswith("every "):
        seconds = _interval_seconds(expression)
        if seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleError(
                f"The shortest interval is {MIN_INTERVAL_SECONDS}s — a job that "
                "fires faster than it finishes will pile up."
            )
        return expression.lower()

    try:
        croniter(expression)
    except (CroniterBadCronError, ValueError) as exc:
        raise ScheduleError(
            f"{expression!r} is neither a cron expression nor an interval. "
            "Try '0 9 * * 1-5' or 'every 30m'."
        ) from exc
    return expression


UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _interval_seconds(expression: str) -> int:
    raw = expression.lower().removeprefix("every ").strip()
    if not raw:
        raise ScheduleError("Say how often, e.g. 'every 30m'.")
    unit = raw[-1]
    if unit not in UNITS:
        raise ScheduleError(f"Unknown unit {unit!r}. Use s, m, h or d.")
    try:
        amount = int(raw[:-1])
    except ValueError as exc:
        raise ScheduleError(f"{raw!r} is not a number followed by s, m, h or d.") from exc
    if amount <= 0:
        raise ScheduleError("The interval must be positive.")
    return amount * UNITS[unit]


# Accepted wall-clock forms, most specific first. Local time, deliberately: a
# person writing `at 09:00` means nine o'clock where they are, and a scheduler
# that silently reads that as UTC fires at the wrong time for most of the world.
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%H:%M",
)


def _absolute_time(expression: str) -> float:
    raw = expression.strip()[3:].strip()
    if not raw:
        raise ScheduleError("Say when, e.g. 'at 2026-09-01T09:00' or 'at 09:00'.")
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            # A bare time means the next occurrence of it, which is today if it
            # has not passed and tomorrow if it has. Anything else schedules a
            # job in the past, which never fires and looks broken.
            today = datetime.now().replace(
                hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
            )
            if today.timestamp() <= time.time():
                today = today + timedelta(days=1)
            return today.timestamp()
        return parsed.timestamp()
    raise ScheduleError(
        f"{raw!r} is not a time this understands. Try '2026-09-01T09:00', "
        "'2026-09-01' or '09:00'."
    )


def next_fire(expression: str, after: float) -> float:
    lowered = expression.lower()
    if lowered.startswith("every "):
        return after + _interval_seconds(expression)
    if lowered.startswith("in "):
        return after + _interval_seconds("every " + expression[3:])
    if lowered.startswith("at "):
        return _absolute_time(expression)
    return croniter(expression, datetime.fromtimestamp(after, tz=timezone.utc)).get_next(float)


# What a tick amounted to. `no_change` is a first-class outcome, not a variety
# of success: it is the answer to "is this monitor alive and finding nothing",
# which is a different question from "did it run".
RUN_STATUSES = ("ok", "failed", "no_change", "silent")


@dataclass
class Run:
    started_at: float
    finished_at: float = 0.0
    ok: bool = False
    summary: str = ""
    error: str = ""
    status: str = ""
    # Where the full output was written. The summary in this file is an
    # excerpt; a job that produces four pages should not put four pages into
    # the state file every scheduler reads on every tick.
    output_path: str = ""
    # Seconds between when the run was due and when it actually started.
    # Recorded rather than smoothed over, because "it ran six hours late
    # because the laptop was shut" and "it ran on time" are different facts.
    late_by: float = 0.0
    # Whether this tick cost a model call. Stored rather than derived from the
    # status: a `no_agent` script job and a suppressed monitor tick both end
    # `ok`-shaped and neither bills anything, and the whole point of those two
    # features is being able to see that in the history.
    used_model: bool = False

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "ok" if self.ok else "failed"

    def to_json(self) -> dict[str, Any]:
        return {
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "ok": self.ok,
            "summary": self.summary,
            "error": self.error,
            "status": self.status,
            "outputPath": self.output_path,
            "lateBy": self.late_by,
            "usedModel": self.used_model,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Run":
        status = str(raw.get("status") or "")
        return cls(
            started_at=float(raw.get("startedAt") or 0),
            finished_at=float(raw.get("finishedAt") or 0),
            ok=bool(raw.get("ok")),
            summary=str(raw.get("summary") or ""),
            error=str(raw.get("error") or ""),
            status=status if status in RUN_STATUSES else "",
            output_path=str(raw.get("outputPath") or ""),
            late_by=float(raw.get("lateBy") or 0),
            used_model=bool(raw.get("usedModel")),
        )


@dataclass
class Job:
    id: str
    name: str
    schedule: str
    prompt: str
    workspace: str
    # The consent, recorded at creation. Never widened afterwards by anything
    # the job itself does or says.
    approval_mode: str = "ask"
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    next_run_at: float = 0.0
    runs: list[Run] = field(default_factory=list)

    # How many times to run before retiring. 0 is forever; a one-shot schedule
    # implies 1.
    repeat: int = 0
    runs_done: int = 0

    # How the person hears about a run. The output file is written regardless.
    deliver: str = "none"

    # A script under `<home>/scripts/`. With `no_agent` it IS the job; without
    # it, its stdout is injected into the prompt as fresh facts.
    script: str = ""
    no_agent: bool = False

    # The cheap source that decides whether the agent runs at all.
    monitor_kind: str = ""       # "" | "script" | "url"
    monitor_source: str = ""
    monitor_hash: str = ""
    monitor_changed_at: float = 0.0

    # Other jobs whose most recent output is prepended to this one's prompt.
    context_from: list[str] = field(default_factory=list)

    # Where `deliver: webhook` posts to.
    deliver_target: str = ""

    # Per-job overrides of the session defaults, all optional and all empty by
    # default. They exist because a job is not the session that created it, and
    # the settings that suit a person typing are rarely the ones that suit a
    # thing running at 3am. `model` is still checked against the build's allowlist when it is
    # used — a per-job override is not a way around the lock.
    model: str = ""
    thinking: str = ""
    # The toolbelt, narrowed. Fewer tools is fewer schemas in every prompt this
    # job ever sends, which for a job firing hourly is the largest single cost
    # nobody looks at. Empty means the session default.
    enabled_tools: list[str] = field(default_factory=list)
    # Skills loaded into this job's prompt before it starts.
    skills: list[str] = field(default_factory=list)
    # A session id this job's output is appended to, so a scheduled run shows
    # up in `andromeda --resume` next to the conversation it came from.
    attach_to: str = ""

    # Who created it. An agent may propose autonomy; only a person grants the
    # unattended kind — see `Schedule.add`.
    origin: str = "user"

    # Why it stopped, when it stopped itself. Distinct from `enabled=False`,
    # which is a person's decision and carries no reason.
    paused_reason: str = ""
    consecutive_failures: int = 0

    # ---- derived ----------------------------------------------------------

    @property
    def last_run(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    @property
    def last_output(self) -> Run | None:
        """The most recent run that actually produced something.

        Not `last_run`: a monitor job's history is mostly `no_change` ticks,
        and chaining off one would feed the next job an empty string every
        time the watched thing held still.
        """
        for run in reversed(self.runs):
            if run.status in {"ok", "silent"} and (run.summary or run.output_path):
                return run
        return None

    @property
    def retired(self) -> bool:
        return bool(self.repeat) and self.runs_done >= self.repeat

    @property
    def state(self) -> str:
        """One word for the list view."""
        if self.retired:
            return "done"
        if self.paused_reason:
            return "paused"
        return "on" if self.enabled else "off"

    @property
    def is_monitored(self) -> bool:
        return bool(self.monitor_kind and self.monitor_source)

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.enabled or self.paused_reason or self.retired:
            return False
        return 0 < self.next_run_at <= now

    def lateness(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.next_run_at) if self.next_run_at else 0.0

    def missed(self, now: float | None = None) -> bool:
        """Late enough that the machine was asleep, not merely busy."""
        return self.lateness(now) > CATCH_UP_GRACE_SECONDS

    def schedule_next(self, after: float | None = None) -> float:
        """The next fire time, measured from now rather than from the miss.

        Measuring from `next_run_at` would make a laptop that slept through six
        hourly ticks fire six times in a row to "catch up" — which is a
        thundering herd of the model, not a recovery. One run, then back on
        cadence.
        """
        after = time.time() if after is None else after
        self.next_run_at = next_fire(self.schedule, after)
        return self.next_run_at

    def record(self, run: Run) -> None:
        self.runs.append(run)
        del self.runs[:-MAX_RUNS_KEPT]

        # A suppressed tick is not an attempt at the work, so it neither counts
        # toward `repeat` nor resets the failure streak. A monitor job with
        # `repeat: 3` means "tell me about the next three changes", not "wake
        # up three times and stop".
        if run.status == "no_change":
            return

        self.runs_done += 1
        if run.status == "failed":
            self.consecutive_failures += 1
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self.paused_reason = (
                    f"paused after {self.consecutive_failures} failures in a row — "
                    "`andromeda cron resume` when it is fixed"
                )
        else:
            self.consecutive_failures = 0

        if self.retired:
            self.paused_reason = self.paused_reason or "finished its run count"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "workspace": self.workspace,
            "approvalMode": self.approval_mode,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "nextRunAt": self.next_run_at,
            "repeat": self.repeat,
            "runsDone": self.runs_done,
            "deliver": self.deliver,
            "script": self.script,
            "noAgent": self.no_agent,
            "monitorKind": self.monitor_kind,
            "monitorSource": self.monitor_source,
            "monitorHash": self.monitor_hash,
            "monitorChangedAt": self.monitor_changed_at,
            "contextFrom": list(self.context_from),
            "deliverTarget": self.deliver_target,
            "model": self.model,
            "thinking": self.thinking,
            "enabledTools": list(self.enabled_tools),
            "skills": list(self.skills),
            "attachTo": self.attach_to,
            "origin": self.origin,
            "pausedReason": self.paused_reason,
            "consecutiveFailures": self.consecutive_failures,
            "runs": [run.to_json() for run in self.runs],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Job | None":
        identifier = str(raw.get("id") or "").strip()
        schedule = str(raw.get("schedule") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        script = str(raw.get("script") or "").strip()
        # A `no_agent` job has no prompt — the script is the whole job. An
        # earlier version required one here, so a watchdog saved correctly and
        # then vanished on the next load, which reads as "my job disappeared"
        # and leaves nothing behind to explain it. What makes a job loadable is
        # having something to *do*, and a script is something to do.
        if not identifier or not schedule or not (prompt or script):
            return None

        mode = str(raw.get("approvalMode") or "ask")
        kind = str(raw.get("monitorKind") or "")
        deliver = str(raw.get("deliver") or "none")
        origin = str(raw.get("origin") or "user")
        return cls(
            id=identifier,
            name=str(raw.get("name") or identifier),
            schedule=schedule,
            prompt=prompt,
            workspace=str(raw.get("workspace") or ""),
            # An unrecognised mode reads as `ask`, the narrow one. A corrupt
            # field must never widen what a job may do.
            approval_mode=mode if mode in APPROVAL_MODES else "ask",
            enabled=bool(raw.get("enabled", True)),
            created_at=float(raw.get("createdAt") or 0),
            next_run_at=float(raw.get("nextRunAt") or 0),
            repeat=max(0, int(raw.get("repeat") or 0)),
            runs_done=max(0, int(raw.get("runsDone") or 0)),
            deliver=deliver if deliver in DELIVERY_MODES else "none",
            script=str(raw.get("script") or ""),
            no_agent=bool(raw.get("noAgent")),
            # An unrecognised monitor kind disables monitoring rather than
            # defaulting to one: a corrupt field must not silently start
            # fetching a URL, and must not silently suppress every run either.
            monitor_kind=kind if kind in monitor_module.MONITOR_KINDS else "",
            monitor_source=str(raw.get("monitorSource") or ""),
            monitor_hash=str(raw.get("monitorHash") or ""),
            monitor_changed_at=float(raw.get("monitorChangedAt") or 0),
            context_from=[str(i) for i in (raw.get("contextFrom") or []) if str(i).strip()],
            deliver_target=str(raw.get("deliverTarget") or ""),
            model=str(raw.get("model") or ""),
            thinking=str(raw.get("thinking") or ""),
            enabled_tools=[str(i) for i in (raw.get("enabledTools") or []) if str(i).strip()],
            skills=[str(i) for i in (raw.get("skills") or []) if str(i).strip()],
            attach_to=str(raw.get("attachTo") or ""),
            # Same rule as the approval mode, in the same direction: unknown
            # provenance reads as `agent`, the one that may not be widened
            # without a person saying so.
            origin=origin if origin in ORIGINS else "agent",
            paused_reason=str(raw.get("pausedReason") or ""),
            consecutive_failures=max(0, int(raw.get("consecutiveFailures") or 0)),
            runs=[Run.from_json(r) for r in (raw.get("runs") or []) if isinstance(r, dict)],
        )


# Commands that stop or restart the thing running the job. This is a real
# failure mode, not a hypothetical one: an agent schedules a job that restarts
# the supervisor, the supervisor revives it, the resumed turn re-runs the same
# logic, and the machine sits in a respawn loop every ten seconds until someone
# breaks it by hand. Anchored on command shapes, never on prose — a cron *prompt* is fed
# to a model, not a shell, so matching the English phrase "restart the
# scheduler" would refuse legitimate jobs while stopping none of the real ones.
_LIFECYCLE_PATTERNS = (
    re.compile(r"\bandromeda\s+cron\s+(daemon|install|uninstall)\b", re.I),
    re.compile(r"\blaunchctl\b[^\n]*\bandromeda\b", re.I),
    re.compile(r"\bsystemctl\b[^\n]*\bandromeda\b", re.I),
    re.compile(r"\bpkill\b[^\n]*\bandromeda\b", re.I),
    re.compile(r"\bkillall\b[^\n]*\bandromeda\b", re.I),
)


def lifecycle_refusal(*texts: str) -> str:
    """Why this job may not be created, or an empty string.

    Defence in depth rather than the only defence: `terminal` is `destructive`,
    so a job in the default `ask` mode has no shell at all. This catches the
    `auto` case, where it would.
    """
    for text in texts:
        for pattern in _LIFECYCLE_PATTERNS:
            if pattern.search(text or ""):
                return (
                    "This job would stop or restart the scheduler that runs it. "
                    "Under a supervisor that is a respawn loop, not a restart — "
                    "the job dies, the service comes back, and the job fires "
                    "again. Do it by hand instead."
                )
    return ""


class Schedule:
    """Every job on this machine, in one readable file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._jobs: dict[str, Job] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt schedule runs nothing rather than running something
            # unpredictable. The file is left for a person to look at.
            return
        for item in raw.get("jobs", []) if isinstance(raw, dict) else []:
            job = Job.from_json(item) if isinstance(item, dict) else None
            if job is not None:
                self._jobs[job.id] = job

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"jobs": [job.to_json() for job in self._jobs.values()]}, indent=2)
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        temporary.replace(self.path)

    # ---- managing ---------------------------------------------------------

    def add(
        self,
        schedule: str,
        prompt: str,
        workspace: str,
        name: str = "",
        approval_mode: str = "ask",
        *,
        repeat: int = 0,
        deliver: str = "none",
        script: str = "",
        no_agent: bool = False,
        monitor_kind: str = "",
        monitor_source: str = "",
        context_from: list[str] | None = None,
        deliver_target: str = "",
        model: str = "",
        thinking: str = "",
        enabled_tools: list[str] | None = None,
        skills: list[str] | None = None,
        attach_to: str = "",
        origin: str = "user",
    ) -> Job:
        canonical = parse_schedule(schedule)
        prompt = (prompt or "").strip()
        if approval_mode not in APPROVAL_MODES:
            raise ScheduleError(f"approval must be one of {', '.join(APPROVAL_MODES)}.")
        if deliver not in DELIVERY_MODES:
            raise ScheduleError(f"deliver must be one of {', '.join(DELIVERY_MODES)}.")
        if origin not in ORIGINS:
            raise ScheduleError(f"origin must be one of {', '.join(ORIGINS)}.")

        if no_agent:
            if not script:
                raise ScheduleError(
                    "A --no-agent job is its script, so it needs one. Without a "
                    "script there is nothing left to run."
                )
            if monitor_kind:
                raise ScheduleError(
                    "A --no-agent job has no agent run to suppress, so a monitor "
                    "source would do nothing. Use one or the other."
                )
        elif not prompt:
            raise ScheduleError("A job needs something to do.")

        if monitor_kind and monitor_kind not in monitor_module.MONITOR_KINDS:
            raise ScheduleError(
                f"monitor kind must be one of {', '.join(monitor_module.MONITOR_KINDS)}."
            )
        if bool(monitor_kind) != bool(monitor_source):
            raise ScheduleError("A monitor needs both a kind and a source.")

        if deliver == "webhook" and not deliver_target:
            raise ScheduleError("`--deliver webhook` needs a URL to post to.")

        if model:
            # Checked here, at the place a job is written, as well as wherever
            # it is used. The BYOK lane has no server-side backstop, so the
            # allowlist has to be enforced at both — the same rule
            # `config.validate` follows.
            from .models import is_allowed, refusal

            if not is_allowed(model):
                raise ScheduleError(refusal(model))

        if thinking and thinking not in THINKING_LEVELS:
            raise ScheduleError(
                f"thinking must be one of {', '.join(THINKING_LEVELS)}."
            )

        # **An agent may propose autonomy; only a person grants the unattended
        # kind.** This is the approval gate's "a child is never more permissive
        # than its parent", applied to time instead of to depth: a scheduled
        # job is a context the person is not in, so the agent creating one can
        # never hand it more than the narrowed belt a non-interactive run gets
        # anyway. Promotion to `auto` is an explicit `andromeda cron approve`,
        # made by someone who can read what the job will do first.
        if origin == "agent" and approval_mode == "auto":
            raise ScheduleError(
                "A job you create cannot run in `auto` mode. Create it, tell the "
                "user what it needs, and let them run `andromeda cron approve "
                "<id> --approval auto` if they agree."
            )

        guard = lifecycle_refusal(prompt, script)
        if guard:
            raise ScheduleError(guard)

        # A one-shot schedule that repeated forever would fire once and then
        # never again (its `at` time is in the past), leaving a job that looks
        # scheduled and is not. Saying so as `repeat=1` makes it retire
        # honestly instead.
        if is_one_shot(canonical) and not repeat:
            repeat = 1

        job = Job(
            id=f"job_{uuid.uuid4().hex[:8]}",
            name=" ".join((name or prompt or script).split())[:60],
            schedule=canonical,
            prompt=prompt,
            workspace=workspace,
            approval_mode=approval_mode,
            repeat=max(0, int(repeat or 0)),
            deliver=deliver,
            script=script,
            no_agent=no_agent,
            monitor_kind=monitor_kind,
            monitor_source=monitor_source,
            context_from=[i for i in (context_from or []) if i],
            deliver_target=deliver_target,
            model=model,
            thinking=thinking,
            enabled_tools=[i for i in (enabled_tools or []) if i],
            skills=[i for i in (skills or []) if i],
            attach_to=attach_to,
            origin=origin,
        )
        job.schedule_next()
        self._jobs[job.id] = job
        self.save()
        return job

    def approve(self, identifier: str, approval_mode: str) -> Job | None:
        """Change what an existing job may do.

        Separate from `add` because this is the only widening path in the
        module, and a widening path should be one function a reader can find.
        It always comes from a person at a terminal — the `cron` model tool
        does not expose it.
        """
        if approval_mode not in APPROVAL_MODES:
            raise ScheduleError(f"approval must be one of {', '.join(APPROVAL_MODES)}.")
        job = self.resolve(identifier)
        if job is None:
            return None
        job.approval_mode = approval_mode
        self.save()
        return job

    def resolve(self, identifier: str) -> Job | None:
        wanted = (identifier or "").strip()
        if not wanted:
            return None
        if wanted in self._jobs:
            return self._jobs[wanted]
        matches = [job for key, job in self._jobs.items() if key.startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        named = [job for job in self._jobs.values() if job.name == wanted]
        return named[0] if len(named) == 1 else None

    def remove(self, identifier: str) -> Job | None:
        job = self.resolve(identifier)
        if job is None:
            return None
        del self._jobs[job.id]
        self.save()
        return job

    def set_enabled(self, identifier: str, enabled: bool) -> Job | None:
        job = self.resolve(identifier)
        if job is None:
            return None
        job.enabled = enabled
        if enabled and job.next_run_at <= time.time():
            job.schedule_next()
        self.save()
        return job

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at)

    def due(self, now: float | None = None) -> list[Job]:
        return [job for job in self.all() if job.due(now)]

    def record(self, job: Job, run: Run) -> None:
        job.record(run)
        job.schedule_next()
        self.save()

    def pause(self, identifier: str, reason: str) -> Job | None:
        job = self.resolve(identifier)
        if job is None:
            return None
        job.paused_reason = reason or "paused"
        self.save()
        return job

    def resume(self, identifier: str) -> Job | None:
        """Clear a self-imposed pause and put the job back on cadence.

        The failure counter is cleared too. Leaving it would mean a job that
        was paused after five failures pauses again on the very next one, which
        is not what "resume" means to anyone.
        """
        job = self.resolve(identifier)
        if job is None:
            return None
        job.paused_reason = ""
        job.consecutive_failures = 0
        if job.retired:
            # Resuming a finished one-shot restarts its count; otherwise it is
            # immediately retired again and `resume` looks broken.
            job.runs_done = 0
        job.schedule_next()
        self.save()
        return job

    # ---- where output lives ----------------------------------------------

    @property
    def root(self) -> Path:
        return self.path.parent

    def output_dir(self, job: Job) -> Path:
        return self.root / "output" / job.id

    def output_path(self, job: Job, when: float) -> Path:
        stamp = datetime.fromtimestamp(when).strftime("%Y%m%d-%H%M%S")
        return self.output_dir(job) / f"{stamp}.md"

    def monitor_cache(self, job: Job) -> Path:
        """The previous monitor reading, kept only so the next change can diff.

        Beside the output rather than inside `cron.json`: it is the *whole*
        source text, and the state file is read and rewritten on every tick by
        every process that touches the scheduler.
        """
        return self.output_dir(job) / "monitor-last.txt"

    def outputs(self, job: Job, limit: int = 20) -> list[Path]:
        directory = self.output_dir(job)
        if not directory.is_dir():
            return []
        files = sorted(
            (p for p in directory.glob("*.md")), key=lambda p: p.name, reverse=True
        )
        return files[:limit]


# ---------------------------------------------------------------------------
# One scheduler at a time
# ---------------------------------------------------------------------------


class SchedulerBusy(RuntimeError):
    """Another scheduler already holds the lock."""


@contextmanager
def exclusive(path: Path):
    """Hold the scheduler lock for the duration, or refuse.

    Two daemons on one schedule fire every job twice, and the second copy of a
    job that writes files is not a duplicate report — it is a second edit. An
    advisory `flock` is the cheapest thing that is actually correct here: it is
    released by the kernel when the process dies, so a `kill -9` does not leave
    a lock file that has to be cleaned up by hand.

    Where `fcntl` is unavailable (Windows) this degrades to no locking rather
    than to a broken lock. Said out loud because a lock that silently does
    nothing is worse than none — this one is documented as best-effort there,
    and the daemon still refuses a second copy it can see.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - closed in the finally below
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SchedulerBusy(
                    f"Another scheduler is already running (holding {path})."
                ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def heartbeat(path: Path) -> None:
    """Say the scheduler is alive, once per tick.

    An empty run history means "nothing was due" and "nothing has been running
    for a week" equally well, and those need different reactions. The file's
    mtime distinguishes them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8")


def heartbeat_age(path: Path) -> float | None:
    """Seconds since the scheduler last ticked, or None if it never has."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None
