"""Jobs that run somewhere you are not.

`test_autonomy.py` pins the first consent axis: an agent may propose autonomy,
only a person grants the unattended kind. This file pins the second, which
exists because "unattended on my laptop" and "unattended on hardware I do not
own, holding my credentials, spending my credit" are not the same grant.

The tests are grouped the way the failures would arrive:

1. **The impossible combination.** A cloud job with a local workspace cannot
   work, so it must not be creatable. The refusal text is asserted, not just
   the exception, because the message is the deliverable — someone who typed
   `--cloud` on a job that tidies their Downloads folder has a correct
   intention and needs to be told what to do instead.
2. **No route around the grant.** Every path that could put `runs_on: cloud`
   on a job without a person typing it.
3. **The ceiling holds at execution**, not only at creation — a store edited by
   hand must not get a shell in a container.
4. **Old jobs load unchanged**, because a migration that quietly moves a job to
   different hardware is the worst bug this feature could have.
"""

from __future__ import annotations

import json

import pytest

from andromeda_agent import cloud, runner
from andromeda_agent.schedule import Job, Schedule, ScheduleError


@pytest.fixture
def schedule(tmp_path) -> Schedule:
    return Schedule(tmp_path / "cron" / "cron.json")


def _add(schedule: Schedule, **kwargs) -> Job:
    defaults = dict(
        schedule="every 1h",
        prompt="say something",
        workspace="/Users/someone/project",
    )
    defaults.update(kwargs)
    expression = defaults.pop("schedule")
    prompt = defaults.pop("prompt")
    workspace = defaults.pop("workspace")
    return schedule.add(expression, prompt, workspace, **defaults)


# ---------------------------------------------------------------------------
# 1. The impossible combination
# ---------------------------------------------------------------------------


def test_a_cloud_job_with_a_local_workspace_is_refused(schedule):
    with pytest.raises(ScheduleError) as caught:
        _add(schedule, runs_on="cloud", workspace_kind="device")

    message = str(caught.value)
    # The workspace it cannot reach is named, because "refused" without the
    # reason reads as a bug in the CLI rather than a fact about containers.
    assert "/Users/someone/project" in message
    assert "cannot see it" in message
    # And both ways forward are offered. A refusal that does not say what to do
    # instead is a wall.
    assert "drop --cloud" in message
    assert "--detached" in message


def test_the_refusal_offers_repo_now_that_repo_exists(schedule):
    """This test used to assert the opposite, and the flip is the record.

    `repo` was deliberately absent from both the enum and the refusal while it
    was unbuilt — no inert surface ahead of implementation. It is built, so a
    person refused for having a local workspace should hear about the option
    that actually solves their case: work on a clone.
    """
    with pytest.raises(ScheduleError) as caught:
        _add(schedule, runs_on="cloud", workspace_kind="device")
    assert "--repo" in str(caught.value)
    assert "repo" in cloud.WORKSPACE_KINDS


def test_a_detached_cloud_job_is_allowed(schedule):
    job = _add(schedule, runs_on="cloud", workspace_kind="detached", workspace="")
    assert job.runs_on == "cloud"
    assert job.workspace_kind == "detached"


def test_detached_is_allowed_on_the_device_too(schedule):
    """It is a narrowing, and a narrowing is always available."""
    job = _add(schedule, runs_on="device", workspace_kind="detached", workspace="")
    assert job.runs_on == "device"
    assert job.workspace_kind == "detached"


def test_an_unknown_location_is_refused_rather_than_guessed(schedule):
    with pytest.raises(ScheduleError):
        _add(schedule, runs_on="edge")
    with pytest.raises(ScheduleError):
        _add(schedule, workspace_kind="s3")


# ---------------------------------------------------------------------------
# 2. No route around the grant
# ---------------------------------------------------------------------------


def test_an_agent_cannot_create_a_cloud_job_at_any_approval_mode(schedule):
    """Refused at `ask` too, not only at `auto`.

    On a hosted runner the *location* is the grant: a read-only job still
    spends the person's credit on a schedule while they are asleep.
    """
    for mode in ("ask", "deny"):
        with pytest.raises(ScheduleError) as caught:
            _add(
                schedule,
                origin="agent",
                approval_mode=mode,
                runs_on="cloud",
                workspace_kind="detached",
                workspace="",
            )
        assert "cannot be pinned to the cloud" in str(caught.value)
        assert "andromeda cron approve" in str(caught.value)


def test_the_cron_tool_has_no_way_to_ask_for_the_cloud():
    """Structural, not a check the model could be talked past.

    The tool's schema is the whole attack surface a prompt injection has. If
    the parameter does not exist there is no argument to set.
    """
    from andromeda_tools import scheduling

    spec = scheduling.cron_spec(schedule=None, workspace_root="/tmp")
    properties = spec.parameters["properties"]
    assert "runs_on" not in properties
    assert "cloud" not in properties
    assert "workspace_kind" not in properties
    assert "detached" not in properties
    # And the executor's own signature cannot take one either, so a future
    # caller cannot pass it through by keyword.
    import inspect

    assert "runs_on" not in inspect.signature(spec.run).parameters


def test_a_corrupt_location_reads_as_device(tmp_path):
    """A damaged field must never widen what a job may do.

    The existing rule for `approvalMode` and `origin`, on the axis cloud added.
    A corrupt `runsOn` that read as `cloud` would move a job onto hardware
    nobody granted it.
    """
    raw = Job(
        id="job_1",
        name="j",
        schedule="every 1h",
        prompt="p",
        workspace="/tmp",
    ).to_json()
    raw["runsOn"] = "clouD_"
    raw["workspaceKind"] = "nonsense"
    loaded = Job.from_json(raw)
    assert loaded is not None
    assert loaded.runs_on == "device"
    assert loaded.workspace_kind == "device"


def test_moving_an_existing_job_re_applies_every_creation_refusal(schedule):
    """`approve` must not become the way around `add`."""
    job = _add(schedule)  # a local job, with a /Users/… workspace
    with pytest.raises(ScheduleError) as caught:
        schedule.set_location(job.id, runs_on="cloud")
    assert "cannot see it" in str(caught.value)
    # And nothing was written on the way to refusing.
    assert schedule.resolve(job.id).runs_on == job.runs_on


def test_moving_a_job_whose_belt_the_cloud_forbids_is_refused_not_trimmed(schedule):
    """Silently dropping a tool somebody typed makes a job that does nothing."""
    job = _add(
        schedule,
        workspace_kind="detached",
        workspace="",
        enabled_tools=["web_fetch", "terminal"],
    )
    with pytest.raises(ScheduleError) as caught:
        schedule.set_location(job.id, runs_on="cloud")
    message = str(caught.value)
    assert "terminal" in message
    assert "unattended shell" in message
    assert schedule.resolve(job.id).runs_on == job.runs_on


def test_a_belt_the_location_forbids_is_refused_at_creation(schedule):
    with pytest.raises(ScheduleError) as caught:
        _add(
            schedule,
            runs_on="cloud",
            workspace_kind="detached",
            workspace="",
            enabled_tools=["cron"],
        )
    assert "cron" in str(caught.value)


def test_a_detached_job_cannot_hold_filesystem_tools(schedule):
    with pytest.raises(ScheduleError) as caught:
        _add(
            schedule,
            workspace_kind="detached",
            workspace="",
            enabled_tools=["write_file"],
        )
    assert "no workspace to write into" in str(caught.value)


# ---------------------------------------------------------------------------
# 3. The ceiling holds where the work happens
# ---------------------------------------------------------------------------


def test_narrowing_can_only_subtract():
    kept = cloud.narrow_tools(
        ["web_fetch", "terminal", "memory_search"], "cloud", "detached"
    )
    assert kept == ["web_fetch", "memory_search"]
    # It never invents a name, whatever it is handed.
    assert cloud.narrow_tools([], "cloud", "detached") == []


def test_the_device_keeps_its_tools():
    kept = cloud.narrow_tools(["terminal", "write_file"], "device", "device")
    assert kept == ["terminal", "write_file"]


def _run_once(tmp_path, job: Job, config: dict) -> dict:
    """One real `runner.execute`, with the model replaced by a recorder.

    Through the production entry point rather than the private one: the whole
    claim being tested is that the ceiling is applied on the path a fired job
    actually takes.
    """
    captured: dict = {}

    class _Turn:
        def send(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "done"

    def build(settings, workspace, _job):
        captured["settings"] = settings
        captured["workspace"] = workspace
        return _Turn()

    store = Schedule(tmp_path / "cron" / "cron.json")
    store._jobs[job.id] = job
    store.save()
    captured["run"] = runner.execute(job, store, config, tmp_path, build=build)
    return captured


def test_a_hand_edited_store_still_cannot_get_a_shell_in_the_cloud(tmp_path):
    """The store is a file. Somebody will edit it.

    `Schedule.add` refuses this belt, so by the time a run happens there should
    be nothing to remove — which is exactly why the runner removes it anyway.
    """
    job = Job(
        id="job_x",
        name="x",
        schedule="every 1h",
        prompt="do it",
        workspace="",
        runs_on="cloud",
        workspace_kind="detached",
        enabled_tools=["terminal", "web_fetch"],
    )
    captured = _run_once(
        tmp_path,
        job,
        {
            "enabled_tools": ["terminal", "web_fetch", "read_file"],
            "max_tier": "irreversible",
        },
    )

    assert "terminal" not in captured["settings"]["enabled_tools"]
    assert "web_fetch" in captured["settings"]["enabled_tools"]
    # And the tier ceiling clamped, whatever the machine allows.
    assert captured["settings"]["max_tier"] == cloud.CLOUD_MAX_TIER
    # A detached job is handed no workspace, and is told so, so it does not
    # spend its first two tool calls discovering the directory is empty.
    assert captured["workspace"] == ""
    assert "no filesystem" in captured["prompt"]


def test_a_stricter_machine_keeps_its_own_ceiling(tmp_path):
    """Narrowing must never be a way to widen."""
    job = Job(
        id="job_y",
        name="y",
        schedule="every 1h",
        prompt="do it",
        workspace="",
        runs_on="cloud",
        workspace_kind="detached",
    )
    captured = _run_once(
        tmp_path, job, {"enabled_tools": ["web_fetch"], "max_tier": "safe_local"}
    )
    assert captured["settings"]["max_tier"] == "safe_local"


def test_a_device_job_is_not_told_it_has_no_filesystem(tmp_path):
    """The added preamble costs tokens on every run, so it must be conditional."""
    job = Job(
        id="job_z",
        name="z",
        schedule="every 1h",
        prompt="do it",
        workspace=str(tmp_path),
    )
    captured = _run_once(tmp_path, job, {"enabled_tools": ["read_file"]})
    assert "no filesystem" not in captured["prompt"]
    assert captured["workspace"] == str(tmp_path)


# ---------------------------------------------------------------------------
# 4. Nothing moves on its own
# ---------------------------------------------------------------------------


def test_a_job_written_before_this_shipped_loads_unchanged(tmp_path):
    """The exact dict shape a v1 store holds, with neither field present."""
    store = tmp_path / "cron.json"
    store.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job_old",
                        "name": "old",
                        "schedule": "every 1h",
                        "prompt": "check the thing",
                        "workspace": "/Users/someone/project",
                        "approvalMode": "auto",
                        "origin": "user",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = Schedule(store).resolve("job_old")
    assert loaded is not None
    assert loaded.runs_on == "device"
    assert loaded.workspace_kind == "device"
    # Its existing consent is untouched by the migration.
    assert loaded.approval_mode == "auto"


def test_the_fields_survive_a_save_and_load(tmp_path):
    store = tmp_path / "cron.json"
    schedule = Schedule(store)
    job = _add(schedule, runs_on="cloud", workspace_kind="detached", workspace="")
    reloaded = Schedule(store).resolve(job.id)
    assert reloaded is not None
    assert reloaded.runs_on == "cloud"
    assert reloaded.workspace_kind == "detached"


# ---------------------------------------------------------------------------
# 5. Credentials do not follow it
# ---------------------------------------------------------------------------


def test_local_only_secret_references_are_refused_for_a_cloud_job():
    refusal = cloud.secrets_refusal(
        {
            "OPENROUTER_API_KEY": "op://Personal/OpenRouter/credential",
            "GITHUB_TOKEN": "keychain://github-token",
            "SOME_VAR": "env://SOME_VAR",
        },
        "cloud",
    )
    assert "OPENROUTER_API_KEY" in refusal
    assert "GITHUB_TOKEN" in refusal
    # `env://` reads the process environment, which a container has.
    assert "SOME_VAR" not in refusal
    assert "andromeda secrets put" in refusal


def test_the_same_references_are_fine_on_this_machine():
    assert cloud.secrets_refusal(
        {"GITHUB_TOKEN": "keychain://github-token"}, "device"
    ) == ""


def test_every_resolver_says_whether_it_survives_a_container():
    """A new scheme must decide this, rather than defaulting into the cloud."""
    from andromeda_agent import secrets

    local_only = {"op", "bw", "keychain", "cmd"}
    for name, resolver in secrets.RESOLVERS.items():
        if name in local_only:
            assert resolver.cloud_refusal, f"{name} must say why it cannot travel"
        else:
            assert resolver.cloud_refusal == ""


# ---------------------------------------------------------------------------
# 6. Two things must never both decide a job is due
#
# The `flock` that stops two local daemons double-firing does not cross a
# machine boundary. With a hosted trigger arming fires and a local tick loop
# also running, a job that sends a message would send it twice and the person
# would see one report.
# ---------------------------------------------------------------------------


def test_the_relay_provider_never_decides_a_job_is_due():
    from andromeda_agent import providers_cron

    provider = providers_cron.get("relay")
    assert provider.name == "relay"

    class _EverythingIsDue:
        def due(self, now=None):
            raise AssertionError("the relay provider must not ask the schedule")

    # Not "returns an empty list because nothing happens to be due" — it must
    # not consult the schedule at all, so the assertion is on the schedule
    # being untouched.
    assert provider.due(_EverythingIsDue()) == []


def test_the_built_in_provider_no_longer_decides_anything(monkeypatch):
    """D15, and the sharp end of `I-TRIGGER-7`.

    This provider used to answer `schedule.due(now)` — it worked out on this
    machine that a job was overdue. That made two components capable of the same
    conclusion, and the consequence was not theoretical: every cloud job ran
    twice, once in a container and once on the laptop, disagreeing.

    Now it asks. A job's own clock saying "overdue" is not enough and must never
    be enough again; only a fire the server handed over runs.
    """
    from andromeda_agent import providers_cron

    class _Schedule:
        def due(self, now=None):
            raise AssertionError(
                "the tick loop asked the local clock what was due — that is the "
                "second decider I-TRIGGER-7 forbids"
            )

        def resolve(self, job_id):
            return _job(job_id)

    # This machine has a server holding its clock. Stated rather than
    # inherited: otherwise the test reads whatever credentials the
    # machine running it happens to have, and passes or fails by
    # accident of environment.
    monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
    monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
    assert providers_cron.BuiltIn().due(_Schedule()) == []


def test_only_a_fire_the_server_handed_over_runs(monkeypatch):
    """And it runs at the moment the server named, not one this machine chose."""
    import time

    from andromeda_agent import providers_cron

    class _Schedule:
        def resolve(self, job_id):
            return _job(job_id)

    now = time.time()
    handed = [
        ("job_ready", "2026-08-27T10:00:00+00:00", now - 5),
        ("job_later", "2026-08-27T11:00:00+00:00", now + 3600),
    ]
    # This machine has a server holding its clock. Stated rather than
    # inherited: otherwise the test reads whatever credentials the
    # machine running it happens to have, and passes or fails by
    # accident of environment.
    monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
    monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: handed)

    provider = providers_cron.BuiltIn()
    ready = provider.due(_Schedule(), now=now)
    assert [job.id for job in ready] == ["job_ready"]
    # The server's own name for the moment, carried through so the claim, the
    # settle and the outcome report all address the same row.
    assert ready[0].pending_fire_at == "2026-08-27T10:00:00+00:00"

    provider.taken("job_ready", "2026-08-27T10:00:00+00:00")

    # The one that was not yet due is held, not dropped — that is what lets a
    # laptop keep working with the network gone. Nothing new is fetched here.
    monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
    later = provider.due(_Schedule(), now=now + 3601)
    assert [job.id for job in later] == ["job_later"]


def test_a_fire_is_offered_once_and_then_forgotten(monkeypatch):
    """`taken` is what stops a fire being re-offered on every tick while it is
    still running, or after it has been lost to another machine's claim."""
    import time

    from andromeda_agent import providers_cron

    class _Schedule:
        def resolve(self, job_id):
            return _job(job_id)

    now = time.time()
    # This machine has a server holding its clock. Stated rather than inherited:
    # otherwise the test reads whatever credentials the machine running it
    # happens to have, and passes or fails by accident of environment.
    monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
    monkeypatch.setattr(
        providers_cron, "fetch_due",
        lambda schedule: [("job_a", "2026-08-27T10:00:00+00:00", now - 5)],
    )
    provider = providers_cron.BuiltIn()
    assert [j.id for j in provider.due(_Schedule(), now=now)] == ["job_a"]
    provider.taken("job_a", "2026-08-27T10:00:00+00:00")

    # Re-polled and re-offered by the server is fine — the claim is what makes
    # that safe. What must not happen is the *local* queue serving it twice
    # without the server saying so.
    monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
    assert provider.due(_Schedule(), now=now) == []


def test_an_unreachable_server_yields_nothing_rather_than_falling_back(monkeypatch):
    """The cost of yielding nothing is a late run. The cost of falling back to
    the local clock is the double-fire this was rewritten to end, and those are
    not the same size."""
    from andromeda_agent import providers_cron

    class _Schedule:
        def due(self, now=None):
            raise AssertionError("fell back to the local clock")

        def resolve(self, job_id):
            return _job(job_id)

    def explode(schedule):
        raise RuntimeError("no network")

    # `fetch_due` swallows its own failures, so the provider sees an empty list
    # rather than an exception — a raising tick would also stop the heartbeat,
    # which reads as a crashed scheduler.
    # This machine has a server holding its clock. Stated rather than
    # inherited: otherwise the test reads whatever credentials the
    # machine running it happens to have, and passes or fails by
    # accident of environment.
    monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
    monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
    assert providers_cron.BuiltIn().due(_Schedule()) == []


def _job(job_id: str):
    """The smallest thing `BuiltIn.due` needs back from a store."""

    class _J:
        def __init__(self, ident: str) -> None:
            self.id = ident
            self.pending_fire_at = ""

    return _J(job_id)


def test_an_unrecognised_provider_falls_back_rather_than_stopping():
    """A scheduler that refuses to start over a misspelt setting is a scheduler
    that silently stopped."""
    from andromeda_agent import providers_cron

    assert providers_cron.get("rel4y").name == "built-in"
    assert providers_cron.get("").name == "built-in"


def test_the_local_store_still_knows_which_jobs_believe_they_are_overdue(monkeypatch):
    """The double-fire, closed without depending on a config setting.

    A cloud job is fired by the server, and the `flock` that stops two daemons
    doing this on one machine does not cross a machine boundary. `Relay` exists
    to return nothing from `due` — but it is opt-in, and an unset or misspelt
    `cron_provider` falls back to the built-in tick loop, which used to hand the
    daemon every due job regardless of where it runs.

    Seen live before this was fixed: one `--cloud` job ran on the hosted runner
    at 18:21:18 and again on the laptop at 18:21:19, and the two disagreed.
    """
    import time

    from andromeda_agent import providers_cron
    from andromeda_agent.schedule import Schedule

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        schedule = Schedule(Path(tmp) / "cron.json")
        hosted = schedule.add("every 3m", "x", "/tmp", runs_on="cloud",
                              workspace_kind="detached")
        mine = schedule.add("every 3m", "y", "/tmp", runs_on="device")
        # Both due right now.
        for job in (hosted, mine):
            job.next_run_at = time.time() - 1

        # `Schedule.due` is still the honest answer to "which jobs believe they
        # are overdue" — `cron list` wants it — and it still excludes hosted
        # jobs, which belong to the server's clock entirely.
        assert schedule.due() == [mine]

        # But it is **no longer what runs.** Neither provider consults it: after
        # D15 the only thing that starts a job is a fire the server handed over.
        # This is the assertion that would fail if somebody reconnected the
        # local clock to the scheduler.
        # This machine has a server holding its clock. Stated rather than
        # inherited: otherwise the test reads whatever credentials the
        # machine running it happens to have, and passes or fails by
        # accident of environment.
        monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
        monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
        assert providers_cron.get("").due(schedule) == []
        assert providers_cron.get("relay").due(schedule) == []


def test_no_provider_records_the_run_because_execute_does():
    """A provider owns timing and nothing else — recording included.

    It used to record, and that was the bug: recording lived with the callers,
    and the two *hosted* callers (`cron serve`'s fire handler and the Modal
    runner) have no provider to call. They recorded nothing, so `next_run_at`
    stayed on the fire that had just happened and the job re-fired one spent
    moment for ever. `cron.execute` records now, on every path out, which is
    the only place all four callers pass through.
    """
    from andromeda_agent import providers_cron

    recorded = []

    class _Schedule:
        def record(self, job, run):
            recorded.append((job, run))

    for name in ("relay", "built-in"):
        providers_cron.get(name).after_run(_Schedule(), "job", "run")
    assert recorded == []


# ---------------------------------------------------------------------------
# 7. What a cadence costs
#
# A hosted runner stays awake ~5 minutes after each fire, so a fire costs five
# minutes of machine time however short the job is. That makes cost depend on
# how *often* a job fires and almost not at all on how long it runs, which is
# the opposite of what anybody assumes.
# ---------------------------------------------------------------------------


def test_a_cadence_faster_than_the_idle_window_is_called_out():
    """`every 5m` never lets the machine stop, which is a server-priced job."""
    note = cloud.wake_cost_note("every 5m")
    assert "never stop" in note
    assert "watch" in note  # and it names the thing that makes ticks free


def test_the_cliff_is_caught_exactly_at_the_idle_window():
    """The off-by-one that actually happened.

    Measuring the gap from `first + 1` rather than `first` inflates it by a
    second, so `every 5m` reads as 301s and slips past a `<= 300` check — the
    single cadence this function most needs to catch.
    """
    assert "never stop" in cloud.wake_cost_note("every 5m")
    # And one second's worth of cadence either side behaves differently, which
    # is what proves the boundary is where it claims to be.
    assert "never stop" not in cloud.wake_cost_note("every 6m")


def test_an_expensive_but_legitimate_cadence_reports_its_duty_cycle():
    """Reported, not judged. Somebody may well want this and should know."""
    note = cloud.wake_cost_note("every 15m")
    assert "%" in note


def test_a_cheap_cadence_says_nothing():
    """A warning on every job is a warning nobody reads."""
    assert cloud.wake_cost_note("every 6h") == ""
    assert cloud.wake_cost_note("0 9 * * 1-5") == ""


def test_an_unparseable_schedule_does_not_raise_here():
    """This runs while a job is being described, and a cost note is never worth
    failing a command over."""
    assert cloud.wake_cost_note("not a schedule") == ""
    assert cloud.wake_cost_note("") == ""


# ---------------------------------------------------------------------------
# `auto` — placement as a fire-time question
# ---------------------------------------------------------------------------


class TestHybridPlacement:
    """Where a job runs was a creation-time question, and it was the wrong
    question: somebody scheduling "watch my deploys" does not know or care
    where it runs. Worse, the answer was permanent — a job made on a Tuesday
    afternoon ran on the laptop forever, including every night the lid was
    shut, which is exactly when a watcher earns its keep."""

    def test_a_job_that_needs_local_files_cannot_leave_the_machine(self):
        from andromeda_agent import cloud

        assert cloud.resolve_placement("auto", "device") == "device"

    def test_everything_else_prefers_the_cloud(self):
        from andromeda_agent import cloud

        assert cloud.resolve_placement("auto", "detached") == "cloud"
        assert cloud.resolve_placement("auto", "repo") == "cloud"

    def test_an_explicit_choice_is_honoured_as_written(self):
        from andromeda_agent import cloud

        assert cloud.resolve_placement("device", "detached") == "device"
        assert cloud.resolve_placement("cloud", "detached") == "cloud"

    def test_it_falls_back_to_the_laptop_with_no_runner(self):
        """Running late on the laptop beats not running."""
        from andromeda_agent import cloud

        assert cloud.resolve_placement("auto", "detached", False) == "device"

    def test_auto_takes_the_stricter_of_the_two_ceilings(self):
        """A job whose permissions changed depending on whether a laptop
        happened to be awake is not something anybody could consent to."""
        from andromeda_agent import cloud

        assert cloud.max_tier_for("auto") == cloud.max_tier_for("cloud")

    def test_an_agent_may_create_auto_but_still_not_pin_to_cloud(self):
        from andromeda_agent import cloud

        assert cloud.agent_origin_refusal("auto") == ""
        assert "cannot be pinned" in cloud.agent_origin_refusal("cloud")

    def test_an_existing_job_is_not_silently_moved_to_the_cloud(self):
        """A job written before `auto` existed was created under a promise
        that it runs here. Reading it back as `auto` would move somebody's
        jobs onto a hosted runner without being asked."""
        loaded = Job.from_json(
            {
                "id": "job_old",
                "name": "old",
                "schedule": "every 1h",
                "prompt": "p",
                "workspace": "/Users/someone/project",
            }
        )
        assert loaded is not None
        assert loaded.runs_on == "device"

    def test_a_new_job_gets_auto(self):
        job = Job(
            id="j", name="n", schedule="every 1h", prompt="p", workspace="/tmp"
        )
        assert job.runs_on == "auto"

    def test_where_now_reports_the_last_real_fire(self):
        """A session badge that says ⌂ when the job has been running in the
        cloud all week is worse than no badge."""
        job = Job(
            id="j", name="n", schedule="every 1h", prompt="p",
            workspace="", workspace_kind="detached",
        )
        assert job.where_now() == "cloud"
        job.last_placement = "device"
        assert job.where_now() == "device"
        assert Job.from_json(job.to_json()).last_placement == "device"


def test_exactly_one_thing_can_decide_any_job_is_due(monkeypatch):
    """`I-TRIGGER-7`, as a property of the whole matrix rather than one case.

    "Exactly one component decides that anything is due. Any code path where a
    second component can reach the same conclusion is a bug, whatever locking it
    has."

    After D15 the answer is uniform: **the server decides, for every job.** Every
    job is uploaded, the server holds its clock, and this machine runs only what
    it has been handed. So the table below has one column that must be true
    everywhere and one that must be false everywhere — which is a much stronger
    claim than the complement-of-two-filters it replaces.

    Written as a table because the bug this guards was invisible in any single
    case: every cloud job was running twice, once in a container and once on the
    laptop, disagreeing, and it looked fine from either side alone.
    """
    import tempfile
    import time
    from pathlib import Path

    from andromeda_agent import cloud_client, providers_cron
    from andromeda_agent.schedule import Schedule

    with tempfile.TemporaryDirectory() as tmp:
        schedule = Schedule(Path(tmp) / "cron.json")
        jobs = [
            schedule.add("every 1m", "x", "/tmp", runs_on="device"),
            schedule.add("every 1m", "x", "/tmp", runs_on="cloud",
                         workspace_kind="detached"),
            schedule.add("every 1m", "x", "/tmp", runs_on="auto",
                         workspace_kind="detached"),
            schedule.add("every 1m", "x", "/tmp", runs_on="auto",
                         workspace_kind="device"),
        ]
        # Everything overdue, so nothing is excluded merely by timing.
        for job in jobs:
            job.next_run_at = time.time() - 1

        # Nothing is fetched, so the tick loop has been handed nothing.
        # This machine has a server holding its clock. Stated rather than
        # inherited: otherwise the test reads whatever credentials the
        # machine running it happens to have, and passes or fails by
        # accident of environment.
        monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
        monkeypatch.setattr(providers_cron, "fetch_due", lambda schedule: [])
        started_locally = providers_cron.get("").due(schedule)

        for job in jobs:
            # Every job reaches the server. One it has not heard of is one that
            # never fires at all now, not one that fires late.
            assert cloud_client._placement_of(job) in {"device", "cloud"}
            # And nothing starts here on this machine's own say-so.
            assert job not in started_locally, (
                f"a {job.runs_on}/{job.workspace_kind} job was started by the "
                f"local tick loop without the server handing it over"
            )


def test_an_auto_job_that_prefers_the_cloud_is_now_actually_sent_there():
    """The gap D15 closed, kept as a test so it cannot quietly reopen.

    `placement()` resolves `auto` + a detached workspace to `"cloud"` — that is
    what the user asked for, and the ceiling and the badge both read it. But the
    upload filter used to be `runs_on == "cloud"`, a *different* question, so the
    server never heard about the job and this machine ran it. It said one place
    and ran in another: safe, because exactly one thing decided it, and wrong.

    Both halves are asserted, because fixing one without the other is how it
    comes back: the job is uploaded as `cloud`, **and** the local store no longer
    counts it among the jobs this machine believes are its to run.
    """
    import tempfile
    import time
    from pathlib import Path

    from andromeda_agent import cloud_client
    from andromeda_agent.schedule import Schedule

    with tempfile.TemporaryDirectory() as tmp:
        schedule = Schedule(Path(tmp) / "cron.json")
        job = schedule.add("every 1m", "x", "/tmp", runs_on="auto",
                           workspace_kind="detached")
        job.next_run_at = time.time() - 1

        assert job.placement() == "cloud"
        assert job.runs_on != "cloud"                    # the raw field still differs
        assert cloud_client._placement_of(job) == "cloud"  # and the wire agrees
        assert job not in schedule.due()                 # so this machine leaves it


def test_a_device_job_is_pinned_to_the_machine_that_created_it():
    """D38 — a job touching `~/projects` must run where `~/projects` is.

    The pin travels as the device id, and only a device job carries one: a
    hosted job has no machine of its own, and pinning it to a laptop would be
    the one mistake that stops it running when the laptop is shut, which is the
    entire feature.
    """
    import tempfile
    from pathlib import Path

    from andromeda_agent import cloud_client
    from andromeda_agent.schedule import Schedule

    seen: list = []
    with tempfile.TemporaryDirectory() as tmp:
        schedule = Schedule(Path(tmp) / "cron.json")
        local = schedule.add("every 1m", "x", "/tmp", runs_on="device")
        hosted = schedule.add("every 1m", "x", "/tmp", runs_on="cloud",
                              workspace_kind="detached")

        def capture(base, token, device, job):
            seen.append((job.id, cloud_client._placement_of(job)))

        assert cloud_client._placement_of(local) == "device"
        assert cloud_client._placement_of(hosted) == "cloud"


def test_a_machine_with_no_account_still_runs_its_own_jobs(monkeypatch, tmp_path):
    """The regression D15 would have shipped, caught before it did.

    Routing every decision through the server is right for a machine that has a
    server. A CLI that was never signed in has none — so `fetch_due` would fail,
    be swallowed into an empty list, and the scheduler would silently run
    nothing at all, for ever, with no error anywhere. That is the exact failure
    shape this area keeps having to design against, introduced by the change
    meant to end it.

    `I-TRIGGER-7` is satisfied either way. It forbids *two* components reaching
    the same conclusion; it does not require the conclusion to be reached in a
    datacenter. With no server, the local clock is the only decider there is.
    """
    import time

    from andromeda_agent import providers_cron
    from andromeda_agent.schedule import Schedule

    schedule = Schedule(tmp_path / "cron.json")
    job = schedule.add("every 1m", "x", "/tmp", runs_on="device")
    job.next_run_at = time.time() - 1

    monkeypatch.setattr(providers_cron, "signed_in", lambda: False)
    monkeypatch.setattr(
        providers_cron, "fetch_due",
        lambda s: (_ for _ in ()).throw(AssertionError("asked a server it has not got")),
    )
    assert providers_cron.BuiltIn().due(schedule) == [job]


def test_being_signed_in_but_offline_does_not_fall_back_to_the_local_clock(
    monkeypatch, tmp_path
):
    """The other half, and the one that matters for correctness.

    Signed in and unreachable is **not** the same as having no account. The
    server may be firing these jobs right now, so guessing costs a duplicated
    side effect — a message sent twice, a file written twice — where holding off
    costs a late run. Those are not the same size, and the fallback that looks
    harmless here is the one that already shipped a bug.
    """
    import time

    from andromeda_agent import providers_cron
    from andromeda_agent.schedule import Schedule

    schedule = Schedule(tmp_path / "cron.json")
    job = schedule.add("every 1m", "x", "/tmp", runs_on="device")
    job.next_run_at = time.time() - 1

    monkeypatch.setattr(providers_cron, "signed_in", lambda: True)
    # `fetch_due` swallows its own network failures into an empty list, because
    # a raising tick would also stop the heartbeat and read as a crash.
    monkeypatch.setattr(providers_cron, "fetch_due", lambda s: [])
    assert providers_cron.BuiltIn().due(schedule) == []
