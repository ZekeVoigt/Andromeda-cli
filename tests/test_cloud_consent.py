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
        assert "cannot run in the cloud" in str(caught.value)
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
    assert schedule.resolve(job.id).runs_on == "device"


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
    assert schedule.resolve(job.id).runs_on == "device"


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


def test_the_built_in_provider_still_decides(monkeypatch):
    """The other direction, so the test above cannot pass by the registry being
    broken."""
    from andromeda_agent import providers_cron

    class _Schedule:
        def due(self, now=None):
            return ["a job"]

    assert providers_cron.get("built-in").due(_Schedule()) == ["a job"]


def test_an_unrecognised_provider_falls_back_rather_than_stopping():
    """A scheduler that refuses to start over a misspelt setting is a scheduler
    that silently stopped."""
    from andromeda_agent import providers_cron

    assert providers_cron.get("rel4y").name == "built-in"
    assert providers_cron.get("").name == "built-in"


def test_the_relay_provider_still_records_the_run():
    """It owns timing and nothing else. The ledger and the output file are this
    machine's own account of what happened and do not depend on who decided it
    should happen."""
    from andromeda_agent import providers_cron

    recorded = []

    class _Schedule:
        def record(self, job, run):
            recorded.append((job, run))

    providers_cron.get("relay").after_run(_Schedule(), "job", "run")
    assert recorded == [("job", "run")]


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
