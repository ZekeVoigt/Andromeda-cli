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

    **Re-arming is not here yet.** The call that tells the server when this job
    next wants firing arrives with the server side of C4. That order is
    deliberate rather than incidental: the refusal to double-fire is worth
    having on its own, and shipping the arming half first would mean a window
    where two things could start the same job.
    """

    name = "relay"

    def due(self, schedule: Any, now: float | None = None) -> list:
        return []

    def after_run(self, schedule: Any, job: Any, run: Any) -> None:
        # Nothing. The run is still recorded locally — `cron.execute` does it,
        # on every path out, for every caller. It moved there because the two
        # hosted callers did not have a provider to call and so recorded
        # nothing, which left `next_run_at` on the fire that had just happened.
        return None

    def describe(self) -> str:
        return "the hosted scheduler (fires arrive at `andromeda cron serve`)"


class BuiltIn:
    """The tick loop, which no longer works out for itself that anything is due.

    It used to: `due` meant `next_run_at` has passed, evaluated here, on this
    machine. That made two components capable of reaching the same conclusion —
    this loop and the server's scheduler — and `I-TRIGGER-7` exists because that
    is exactly how one job comes to run twice. It did, for every cloud job, for
    days, once in a container and once on the laptop, with different answers.

    So the loop **asks**. The server owns `nextRunAt` for every job now (D15),
    hands over the fires it has decided on, and this executes them at the times
    it was given. The distinction that matters is not who runs the work — that
    is still this machine, for anything needing its files — but who concludes
    the work is owed. One answer, one place.

    **The horizon is what keeps a laptop useful without a network.** Each poll
    asks for a little of the near future as well as the present and caches what
    comes back, so a dropped connection costs new *arming* rather than the runs
    already handed over. It is still not deciding: every cached fire carries the
    `fireAt` the server chose.

    **A failed poll yields nothing, and that is deliberate.** The cost of
    yielding nothing is a late run; the cost of falling back to the local clock
    is the double-fire this class was rewritten to end. Those are not the same
    size, and the fallback that looks harmless is the one that already shipped
    a bug.
    """

    name = "built-in"

    def __init__(self) -> None:
        # Server-issued fires this machine holds but has not run yet, keyed by
        # `(job_id, fire_at)` so a re-poll cannot enqueue one twice.
        self._handed: dict[tuple[str, str], float] = {}

    def due(self, schedule: Any, now: float | None = None) -> list:
        import time as _time

        now = _time.time() if now is None else now

        # **A machine with no account decides for itself, and that is still one
        # decider.**
        #
        # `I-TRIGGER-7` forbids two components reaching the same conclusion. It
        # does not require the conclusion to be reached in a datacenter. A CLI
        # that was never signed in has no server holding its clock, so nothing
        # else can possibly fire these jobs — and routing them through a server
        # that does not know they exist would mean the scheduler silently ran
        # nothing at all, for ever, with no error anywhere. That is the failure
        # this whole area keeps having to design against, and it would have been
        # introduced by the change meant to end it.
        #
        # The distinction is deliberate and narrow: **not signed in** falls back
        # to the local clock; **signed in but unreachable** does not. In the
        # second case the server may well be firing these jobs right now, and
        # guessing costs a duplicated side effect rather than a late run.
        if not signed_in():
            return list(schedule.due(now))

        for job_id, fire_at, at in fetch_due(schedule):
            self._handed.setdefault((job_id, fire_at), at)

        ready: list = []
        for (job_id, fire_at), at in sorted(self._handed.items(), key=lambda kv: kv[1]):
            if at > now:
                continue
            job = schedule.resolve(job_id)
            if job is None:
                # The job was deleted here while the server still had it. Drop
                # the fire rather than carry it forever; the next push tells the
                # server, and until then its row is stale, not this machine's
                # problem to act on.
                self._handed.pop((job_id, fire_at), None)
                continue
            job.pending_fire_at = fire_at
            ready.append(job)
        return ready

    def taken(self, job_id: str, fire_at: str) -> None:
        """Forget a fire this machine has finished with, won or lost."""
        self._handed.pop((job_id, fire_at), None)

    def after_run(self, schedule: Any, job: Any, run: Any) -> None:
        # Also nothing, and for the same reason: recording is `cron.execute`'s,
        # so that a path which never reaches a provider still advances cadence.
        return None

    def describe(self) -> str:
        return "the built-in tick loop (andromeda cron daemon)"


def signed_in() -> bool:
    """Does this machine have a server that could be holding its clock?

    The question is about a *relationship*, not reachability — a laptop on a
    train is still signed in, and its jobs are still the server's to fire.
    Conflating the two is what would turn a flaky connection into a double-fire.
    """
    try:
        from andromeda_cli import config as _config

        credentials = _config.load_credentials()
    except Exception:  # noqa: BLE001 - unreadable credentials are not a pairing
        return False
    return bool(credentials.device_token and credentials.base_url)


def fetch_due(schedule: Any) -> list[tuple[str, str, float]]:
    """Ask the server what this machine should run. `(job_id, fire_at, when)`.

    Split out of `BuiltIn.due` so the network can be replaced in a test without
    replacing the queueing logic that is the interesting part.

    Every failure is swallowed into an empty list *here*, where "the server
    could not be reached" is still distinguishable from "nothing is due" — and
    the caller keeps whatever it was already handed. Letting the exception out
    would stop the whole tick, which also stops the heartbeat, which reads as a
    crashed scheduler.
    """
    try:
        from andromeda_cli import config as _config

        from . import cloud_client
    except Exception:  # noqa: BLE001 - no cloud client, no hosted timing
        return []

    try:
        credentials = _config.load_credentials()
        rows = cloud_client.due_jobs(
            credentials.base_url,
            credentials.device_token,
            credentials.device_id,
            horizon_seconds=DUE_HORIZON_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a late run beats a double one
        return []

    out: list[tuple[str, str, float]] = []
    for row in rows:
        job_id = str(row.get("jobId") or "")
        fire_at = str(row.get("fireAt") or "")
        when = row.get("nextRunAt")
        if not job_id or not fire_at or not isinstance(when, (int, float)):
            continue
        out.append((job_id, fire_at, float(when) / 1000.0))
    return out


# How far ahead this machine asks for. Long enough that a laptop shut for a
# lunch break misses nothing; short enough that a job edited in the meantime is
# not run from a copy the server handed over before the edit.
DUE_HORIZON_SECONDS = 15 * 60


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
    wanted = (name or "").strip().lower()
    found = _PROVIDERS.get(wanted)
    if found is None:
        found = _plugin_providers().get(wanted)
    return found if found is not None else _PROVIDERS["built-in"]


def names() -> list[str]:
    return sorted(set(_PROVIDERS) | set(_plugin_providers()))


def _plugin_providers() -> dict[str, CronProvider]:
    """Providers a plugin registered, or nothing.

    After the built-ins, so `built-in` and `relay` cannot be shadowed, and the
    unknown-name fallback still lands on `built-in`. The whole point of that
    fallback is that a scheduler never silently stops, and a plugin is one more
    way for a name to go missing — an uninstalled plugin is exactly the typo
    case with a different cause.
    """
    try:
        from . import plugins as plugins_module
    except ImportError:  # pragma: no cover - half-installed package
        return {}
    return plugins_module.cron_providers()


register(BuiltIn())
register(Relay())
