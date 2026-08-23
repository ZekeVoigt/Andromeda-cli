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
