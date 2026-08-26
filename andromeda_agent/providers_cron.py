"""Who decides when a job runs.

A seam so a deployment can hand scheduling to something else — a cloud
scheduler, a queue, whatever the environment already supervises — at the size
this build needs it.

The seam is a protocol and a registry, not a framework. There is one provider
in the box (`built-in`: the tick loop in `andromeda cron daemon`), and the
value of naming it is that the *other* half — creating, listing and recording
jobs — is now provably independent of who fires them. A second provider has to
implement four methods and cannot reach into the store.

**A provider decides timing and nothing else.** It does not get to say what a
job may do: `Schedule.add` still validates every spec, the approval mode still
travels with the job, and `runner.execute` still reads the job's own consent.
A scheduling backend that could widen a job would be a way to launder one.
"""

from __future__ import annotations

from typing import Any, Protocol


class CronProvider(Protocol):
    """Anything that can decide a job is due."""

    name: str

    def due(self, schedule: Any, now: float | None = None) -> list: ...

    def after_run(self, schedule: Any, job: Any, run: Any) -> None: ...

    def describe(self) -> str: ...


class Relay:
    """Timing owned by the server. This provider fires nothing itself.

    The safety-critical half of the hosted lane, and it is the half that has to
    exist *first*. With a hosted trigger arming fires and a local tick loop also
    running, both would decide the same job is due — and the `flock` that stops
    two daemons doing this on one machine does not cross a machine boundary. A
    job that writes a file, sends a message or posts a webhook would do it
    twice, and the person would see one report.

    So `due()` returns nothing, unconditionally. The local daemon keeps running
    — it still owns heartbeats, `device` jobs, and the missed-run bookkeeping —
    but with this provider selected it never *decides* a cloud job is due. The
    only thing that starts a cloud job is a fire arriving at `cron serve`.

    Selecting it is `cron_provider: relay` in config.yaml. `get()`'s existing
    fallback covers the typo: an unrecognised name runs the built-in loop rather
    than refusing to start, because a scheduler that stops over a misspelt
    setting is a scheduler that silently stopped.

    **Re-arming is not here yet.** `after_run` records the run and stops; the
    call that tells the server when this job next wants firing arrives with the
    server side of C4. That order is deliberate rather than incidental: the
    refusal to double-fire is worth having on its own, and shipping the arming
    half first would mean a window where two things could start the same job.
    """

    name = "relay"

    def due(self, schedule: Any, now: float | None = None) -> list:
        return []

    def after_run(self, schedule: Any, job: Any, run: Any) -> None:
        # The run is still recorded locally. The ledger and the output file are
        # this machine's own account of what happened and do not depend on who
        # decided it should happen.
        schedule.record(job, run)

    def describe(self) -> str:
        return "the hosted scheduler (fires arrive at `andromeda cron serve`)"


class BuiltIn:
    """The tick loop. Due means `next_run_at` has passed."""

    name = "built-in"

    def due(self, schedule: Any, now: float | None = None) -> list:
        return schedule.due(now)

    def after_run(self, schedule: Any, job: Any, run: Any) -> None:
        schedule.record(job, run)

    def describe(self) -> str:
        return "the built-in tick loop (andromeda cron daemon)"


_PROVIDERS: dict[str, CronProvider] = {}


def register(provider: CronProvider) -> None:
    _PROVIDERS[provider.name] = provider


def get(name: str = "") -> CronProvider:
    """The named provider, or the built-in.

    An unknown name falls back rather than raising, and that is deliberate: a
    config naming a provider that is not installed should run the jobs on the
    built-in loop, not stop running them. A scheduler that refuses to start
    because of a typo in a setting is a scheduler that silently stopped.
    """
    return _PROVIDERS.get((name or "").strip().lower(), _PROVIDERS["built-in"])


def names() -> list[str]:
    return sorted(_PROVIDERS)


register(BuiltIn())
register(Relay())
