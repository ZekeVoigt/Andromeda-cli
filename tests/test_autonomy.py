"""Jobs that run without you.

The autonomy layer has a property unit tests are unusually good at pinning:
almost all of it is about what happens when nobody is watching, which is
exactly when a person cannot notice that it went wrong.

Four things get the most attention here, in this order:

1. **Consent.** An agent may propose autonomy; only a person grants the
   unattended kind. Everything that could route around that is tested.
2. **Suppression.** A monitor that fires when nothing changed is a bill; one
   that stays quiet when something did is a broken alarm. Both directions.
3. **Silence.** A `no_agent` job with no output says nothing, and a job that
   said nothing is still recorded as having run.
4. **Not losing things.** A job that saves and does not load again, an output
   file nobody can find, a notepad that grows without bound.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from andromeda_agent import monitor, notepad as notepad_module, runner, scripts
from andromeda_agent.schedule import (
    Job,
    Run,
    Schedule,
    ScheduleError,
    SchedulerBusy,
    exclusive,
    heartbeat,
    heartbeat_age,
    lifecycle_refusal,
    parse_schedule,
)


@pytest.fixture
def home(tmp_path) -> Path:
    (tmp_path / "scripts").mkdir()
    return tmp_path


@pytest.fixture
def schedule(tmp_path) -> Schedule:
    return Schedule(tmp_path / "cron" / "cron.json")


def write_script(home: Path, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return name


class TestScriptContainment:
    """A job spec is data, and a path in data that can point anywhere is
    arbitrary code execution on a timer."""

    def test_a_named_script_resolves(self, home):
        write_script(home, "ok.sh", "echo hi")
        assert scripts.resolve(home, "ok.sh").name == "ok.sh"

    def test_an_absolute_path_is_refused(self, home):
        with pytest.raises(scripts.ScriptError, match="absolute"):
            scripts.resolve(home, "/etc/hosts")

    def test_escaping_the_directory_is_refused(self, home):
        (home / "outside.sh").write_text("echo no")
        with pytest.raises(scripts.ScriptError, match="outside"):
            scripts.resolve(home, "../outside.sh")

    def test_a_symlink_out_is_refused(self, home):
        """The check is on the resolved path — a link is exactly how a
        contained path stops being contained."""
        (home / "elsewhere.sh").write_text("echo no")
        os.symlink(home / "elsewhere.sh", home / "scripts" / "link.sh")
        with pytest.raises(scripts.ScriptError, match="outside"):
            scripts.resolve(home, "link.sh")

    def test_an_unknown_extension_is_refused_not_guessed(self, home):
        write_script(home, "thing.rb", "puts 1")
        with pytest.raises(scripts.ScriptError, match="extension"):
            scripts.resolve(home, "thing.rb")

    def test_a_missing_script_says_so(self, home):
        with pytest.raises(scripts.ScriptError, match="No script"):
            scripts.resolve(home, "nope.sh")

    def test_running_captures_stdout_only(self, home):
        write_script(home, "both.sh", "echo out; echo err >&2")
        result = scripts.run(home, "both.sh")
        assert result.ok and result.output.strip() == "out"

    def test_stderr_surfaces_only_on_failure(self, home):
        """Folding stderr into stdout would make a monitor source look changed
        every time it logged progress."""
        write_script(home, "bad.sh", "echo why >&2; exit 3")
        result = scripts.run(home, "bad.sh")
        assert not result.ok and "why" in result.error and result.exit_code == 3


class TestMonitor:
    def test_identical_output_hashes_identically(self):
        assert monitor.digest("a\nb") == monitor.digest("a\nb")

    def test_whitespace_is_not_normalised(self):
        """Normalising means guessing which differences matter, and a guess
        that is wrong in the quiet direction is a monitor that never fires."""
        assert monitor.digest("a b") != monitor.digest("a  b")

    def test_a_first_reading_is_a_baseline_not_a_change(self):
        block = monitor.change_block("", "hello")
        assert "BASELINE" in block and "hello" in block
        assert "CHANGE DETECTED" not in block

    def test_a_change_carries_a_diff_and_the_new_text(self):
        block = monitor.change_block("one\ntwo", "one\nthree")
        assert "CHANGE DETECTED" in block
        assert "-two" in block and "+three" in block
        assert "The source now reads" in block

    def test_a_huge_diff_is_capped(self):
        block = monitor.change_block(
            "\n".join(str(i) for i in range(2000)),
            "\n".join(str(i * 2) for i in range(2000)),
        )
        assert "more diff lines" in block

    def test_a_failing_script_source_reports_an_error(self, home):
        write_script(home, "boom.sh", "exit 1")
        sample = monitor.read("script", "boom.sh", home)
        assert not sample.ok and sample.error

    def test_an_unknown_kind_is_an_error_not_a_change(self, home):
        assert not monitor.read("telepathy", "x", home).ok


class TestNotepad:
    def test_it_survives_a_reload(self, tmp_path):
        pad = notepad_module.Notepad(tmp_path / "n.json")
        pad.set("job_1", "cursor", "42")
        assert notepad_module.Notepad(tmp_path / "n.json").get("job_1", "cursor") == "42"

    def test_pages_are_per_job(self, tmp_path):
        pad = notepad_module.Notepad(tmp_path / "n.json")
        pad.set("job_1", "k", "a")
        pad.set("job_2", "k", "b")
        assert pad.get("job_1", "k") == "a" and pad.get("job_2", "k") == "b"

    def test_an_oversized_value_is_refused(self, tmp_path):
        pad = notepad_module.Notepad(tmp_path / "n.json")
        with pytest.raises(notepad_module.NotepadError, match="cursor, not a cache"):
            pad.set("job_1", "k", "x" * (notepad_module.MAX_VALUE_BYTES + 1))

    def test_the_per_job_total_is_capped(self, tmp_path):
        """This text is prepended to every prompt the job ever sends."""
        pad = notepad_module.Notepad(tmp_path / "n.json")
        chunk = "x" * (notepad_module.MAX_VALUE_BYTES - 10)
        for index in range(4):
            pad.set("job_1", f"k{index}", chunk)
        with pytest.raises(notepad_module.NotepadError, match="exceed"):
            pad.set("job_1", "k9", chunk)

    def test_refusing_leaves_the_existing_notes_alone(self, tmp_path):
        """Evicting to make room would lose exactly the cursor the job needs."""
        pad = notepad_module.Notepad(tmp_path / "n.json")
        pad.set("job_1", "cursor", "42")
        with pytest.raises(notepad_module.NotepadError):
            pad.set("job_1", "big", "x" * (notepad_module.MAX_VALUE_BYTES + 1))
        assert pad.get("job_1", "cursor") == "42"

    def test_an_empty_page_renders_to_nothing(self, tmp_path):
        assert notepad_module.Notepad(tmp_path / "n.json").render("job_1") == ""

    def test_a_corrupt_file_reads_as_empty_rather_than_crashing(self, tmp_path):
        (tmp_path / "n.json").write_text("{not json", encoding="utf-8")
        assert notepad_module.Notepad(tmp_path / "n.json").page("job_1") == {}


class TestConsent:
    def test_an_agent_cannot_create_an_unattended_job(self, schedule):
        """The approval gate's rule, applied to time instead of depth."""
        with pytest.raises(ScheduleError, match="cannot run in `auto`"):
            schedule.add("every 1h", "do it", "/tmp", approval_mode="auto", origin="agent")

    def test_a_person_can(self, schedule):
        job = schedule.add("every 1h", "do it", "/tmp", approval_mode="auto")
        assert job.approval_mode == "auto" and job.origin == "user"

    def test_promotion_is_a_separate_call(self, schedule):
        job = schedule.add("every 1h", "do it", "/tmp", origin="agent")
        assert job.approval_mode == "ask"
        assert schedule.approve(job.id, "auto").approval_mode == "auto"

    def test_unknown_provenance_reads_as_agent(self, schedule):
        """The narrow direction, matching how a corrupt approval mode reads."""
        assert Job.from_json({"id": "a", "schedule": "every 1h", "prompt": "x", "origin": "?"}).origin == "agent"

    def test_a_job_that_restarts_the_scheduler_is_refused(self, schedule):
        with pytest.raises(ScheduleError, match="respawn loop"):
            schedule.add("every 1h", "run andromeda cron daemon", "/tmp")

    def test_the_guard_does_not_fire_on_prose(self):
        """A cron prompt is fed to a model, not a shell."""
        assert not lifecycle_refusal("Summarise how the API gateway handles restarts")
        assert not lifecycle_refusal("check whether the andromeda scheduler is healthy")


class TestJobShapes:
    def test_a_no_agent_job_needs_a_script(self, schedule):
        with pytest.raises(ScheduleError, match="needs one"):
            schedule.add("every 1h", "", "/tmp", no_agent=True)

    def test_a_no_agent_job_cannot_also_monitor(self, schedule):
        with pytest.raises(ScheduleError, match="no agent run to suppress"):
            schedule.add(
                "every 1h", "", "/tmp", script="s.sh", no_agent=True,
                monitor_kind="script", monitor_source="m.sh",
            )

    def test_a_monitor_needs_both_halves(self, schedule):
        with pytest.raises(ScheduleError, match="both a kind and a source"):
            schedule.add("every 1h", "x", "/tmp", monitor_kind="script")

    def test_a_no_agent_job_survives_a_reload(self, tmp_path):
        """It has no prompt — the script is the whole job. Requiring one here
        meant a watchdog saved correctly and vanished on the next load."""
        path = tmp_path / "cron" / "cron.json"
        first = Schedule(path)
        job = first.add("every 1h", "", "/tmp", script="s.sh", no_agent=True, name="w")
        assert Schedule(path).resolve(job.id) is not None

    def test_a_one_shot_schedule_retires_itself(self, schedule):
        """Otherwise it fires once and then looks scheduled forever."""
        job = schedule.add("in 1h", "do it once", "/tmp")
        assert job.repeat == 1

    def test_repeat_retires_after_its_count(self, schedule):
        job = schedule.add("every 1m", "x", "/tmp", repeat=2)
        for _ in range(2):
            job.record(Run(started_at=time.time(), ok=True, status="ok"))
        assert job.retired and job.state == "done"
        assert not job.due(time.time() + 10_000)

    def test_a_suppressed_tick_does_not_count_toward_repeat(self, schedule):
        """`repeat: 3` on a monitor means "the next three changes", not
        "wake up three times"."""
        job = schedule.add(
            "every 1m", "x", "/tmp", repeat=2, monitor_kind="script", monitor_source="m.sh"
        )
        for _ in range(5):
            job.record(Run(started_at=time.time(), ok=True, status="no_change"))
        assert not job.retired and job.runs_done == 0


class TestFailureHandling:
    def test_a_job_that_keeps_failing_stops_trying(self, schedule):
        job = schedule.add("every 1m", "x", "/tmp")
        for _ in range(5):
            job.record(Run(started_at=time.time(), ok=False, status="failed", error="no"))
        assert job.paused_reason and not job.due(time.time() + 10_000)

    def test_one_success_clears_the_streak(self, schedule):
        job = schedule.add("every 1m", "x", "/tmp")
        for _ in range(4):
            job.record(Run(started_at=time.time(), ok=False, status="failed"))
        job.record(Run(started_at=time.time(), ok=True, status="ok"))
        assert job.consecutive_failures == 0 and not job.paused_reason

    def test_resume_clears_the_counter_too(self, schedule):
        """Otherwise it pauses again on the very next failure, which is not
        what resume means to anybody."""
        job = schedule.add("every 1m", "x", "/tmp")
        for _ in range(5):
            job.record(Run(started_at=time.time(), ok=False, status="failed"))
        resumed = schedule.resume(job.id)
        assert resumed.consecutive_failures == 0 and not resumed.paused_reason

    def test_a_missed_run_fires_once_not_once_per_interval(self, schedule):
        """A laptop that slept through six hourly ticks must not produce six
        runs — that is a thundering herd, not a recovery."""
        job = schedule.add("every 1h", "x", "/tmp")
        job.next_run_at = time.time() - 6 * 3600
        assert job.missed()
        job.schedule_next()
        assert job.next_run_at > time.time()


class TestOneSchedulerAtATime:
    def test_a_second_scheduler_is_refused(self, tmp_path):
        with exclusive(tmp_path / "lock"):
            with pytest.raises(SchedulerBusy):
                with exclusive(tmp_path / "lock"):
                    pass

    def test_the_lock_is_released_on_exit(self, tmp_path):
        with exclusive(tmp_path / "lock"):
            pass
        with exclusive(tmp_path / "lock"):
            pass

    def test_a_heartbeat_distinguishes_quiet_from_dead(self, tmp_path):
        assert heartbeat_age(tmp_path / "hb") is None
        heartbeat(tmp_path / "hb")
        assert heartbeat_age(tmp_path / "hb") < 5


# ---------------------------------------------------------------------------
# The runner: where it all composes
# ---------------------------------------------------------------------------


class Recorder:
    """Stands in for the agent. Records the prompt it was asked to answer."""

    def __init__(self, answer: str = "done", raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.prompts: list[str] = []

    def builder(self):
        def build(settings, workspace, job):
            recorder = self

            class _Turn:
                def send(self, prompt: str) -> str:
                    recorder.prompts.append(prompt)
                    if recorder.raises is not None:
                        raise recorder.raises
                    return recorder.answer

            return _Turn()

        return build


def run_job(job, schedule, home, agent, config=None):
    return runner.execute(
        job, schedule, config or {}, home, build=agent.builder(),
        notepad=notepad_module.Notepad(home / "notepad.json"),
    )


class TestRunner:
    def test_an_unchanged_monitor_never_reaches_the_agent(self, home, schedule):
        """The whole economic argument for monitor mode."""
        write_script(home, "m.sh", "echo steady")
        job = schedule.add(
            "every 1m", "report", "", monitor_kind="script", monitor_source="m.sh"
        )
        agent = Recorder()

        first = run_job(job, schedule, home, agent)
        assert first.status == "ok" and len(agent.prompts) == 1

        second = run_job(job, schedule, home, agent)
        assert second.status == "no_change"
        assert len(agent.prompts) == 1  # not called again
        assert second.used_model is False

    def test_a_changed_monitor_injects_the_diff(self, home, schedule):
        source = home / "scripts" / "m.sh"
        write_script(home, "m.sh", "echo one")
        job = schedule.add(
            "every 1m", "report", "", monitor_kind="script", monitor_source="m.sh"
        )
        agent = Recorder()
        run_job(job, schedule, home, agent)

        source.write_text("echo two", encoding="utf-8")
        run_job(job, schedule, home, agent)
        assert "CHANGE DETECTED" in agent.prompts[-1]
        assert "+two" in agent.prompts[-1]

    def test_a_failing_source_is_an_error_never_a_change(self, home, schedule):
        write_script(home, "m.sh", "echo one")
        job = schedule.add(
            "every 1m", "report", "", monitor_kind="script", monitor_source="m.sh"
        )
        agent = Recorder()
        run_job(job, schedule, home, agent)
        baseline = job.monitor_hash

        (home / "scripts" / "m.sh").write_text("exit 1", encoding="utf-8")
        broken = run_job(job, schedule, home, agent)
        assert broken.status == "failed"
        # Untouched, so a source that recovers to its previous output still
        # suppresses instead of announcing a change that never happened.
        assert job.monitor_hash == baseline

    def test_the_baseline_moves_only_on_a_run_that_worked(self, home, schedule):
        """A change that happens to make the job fail must not be swallowed."""
        write_script(home, "m.sh", "echo one")
        job = schedule.add(
            "every 1m", "report", "", monitor_kind="script", monitor_source="m.sh"
        )
        failing = Recorder(raises=RuntimeError("model down"))
        assert run_job(job, schedule, home, failing).status == "failed"
        assert job.monitor_hash == ""

        working = Recorder()
        assert run_job(job, schedule, home, working).status == "ok"
        assert job.monitor_hash

    def test_a_no_agent_job_with_no_output_is_silent(self, home, schedule):
        """A watchdog that reports every time it finds nothing gets muted."""
        write_script(home, "quiet.sh", "true")
        job = schedule.add("every 1m", "", "", script="quiet.sh", no_agent=True, name="q")
        agent = Recorder()
        run = run_job(job, schedule, home, agent)
        assert run.status == "silent" and run.ok and not agent.prompts

    def test_a_no_agent_job_with_output_reports_it_verbatim(self, home, schedule):
        write_script(home, "loud.sh", "echo 'disk is full'")
        job = schedule.add("every 1m", "", "", script="loud.sh", no_agent=True, name="l")
        run = run_job(job, schedule, home, Recorder())
        assert run.status == "ok" and "disk is full" in run.summary
        assert run.used_model is False

    def test_a_script_feeds_the_prompt(self, home, schedule):
        write_script(home, "facts.sh", "echo 'seventeen open PRs'")
        job = schedule.add("every 1m", "Summarise.", "", script="facts.sh")
        agent = Recorder()
        run_job(job, schedule, home, agent)
        assert "seventeen open PRs" in agent.prompts[0]
        assert "Summarise." in agent.prompts[0]

    def test_a_failing_feed_script_fails_the_job(self, home, schedule):
        write_script(home, "facts.sh", "exit 2")
        job = schedule.add("every 1m", "Summarise.", "", script="facts.sh")
        agent = Recorder()
        run = run_job(job, schedule, home, agent)
        assert run.status == "failed" and not agent.prompts

    def test_chaining_reads_the_other_job_s_output(self, home, schedule):
        first = schedule.add("every 1m", "collect", "", name="collector")
        first.record(run_job(first, schedule, home, Recorder(answer="42 widgets")))
        second = schedule.add("every 1m", "reason", "", context_from=[first.id])

        agent = Recorder()
        run_job(second, schedule, home, agent)
        assert "42 widgets" in agent.prompts[0]

    def test_a_missing_chained_job_is_said_out_loud(self, home, schedule):
        """Dropping it silently gives a job that reasons about less than it
        was told to, with nothing to show why."""
        job = schedule.add("every 1m", "reason", "", context_from=["job_gone"])
        agent = Recorder()
        run_job(job, schedule, home, agent)
        assert "job_gone" in agent.prompts[0]

    def test_the_notepad_reaches_the_prompt(self, home, schedule):
        job = schedule.add("every 1m", "carry on", "")
        pad = notepad_module.Notepad(home / "notepad.json")
        pad.set(job.id, "cursor", "issue-91")
        agent = Recorder()
        runner.execute(job, schedule, {}, home, build=agent.builder(), notepad=pad)
        assert "issue-91" in agent.prompts[0]

    def test_output_is_written_to_a_file(self, home, schedule):
        job = schedule.add("every 1m", "report", "")
        run = run_job(job, schedule, home, Recorder(answer="# Report\n\nAll good."))
        assert run.output_path and Path(run.output_path).exists()
        assert "All good." in Path(run.output_path).read_text()

    def test_a_suppressed_tick_writes_no_file(self, home, schedule):
        """Otherwise a monitor accumulates one empty file per tick, forever."""
        write_script(home, "m.sh", "echo steady")
        job = schedule.add(
            "every 1m", "report", "", monitor_kind="script", monitor_source="m.sh"
        )
        agent = Recorder()
        run_job(job, schedule, home, agent)
        run_job(job, schedule, home, agent)
        assert len(schedule.outputs(job)) == 1

    def test_the_job_s_approval_mode_beats_the_machine_s(self, home, schedule):
        """Consent belongs to the job, not to the machine it happens to be on."""
        job = schedule.add("every 1m", "x", "", approval_mode="deny")
        seen: dict = {}

        def build(settings, workspace, current):
            seen.update(settings)

            class _Turn:
                def send(self, prompt):
                    return "ok"

            return _Turn()

        runner.execute(
            job, schedule, {"approval_mode": "auto"}, home, build=build,
            notepad=notepad_module.Notepad(home / "n.json"),
        )
        assert seen["approval_mode"] == "deny"

    def test_a_model_failure_is_recorded_not_raised(self, home, schedule):
        job = schedule.add("every 1m", "x", "")
        run = run_job(job, schedule, home, Recorder(raises=RuntimeError("boom")))
        assert run.status == "failed" and "boom" in run.error


class TestTheCronTool:
    """What the agent can do about time."""

    def _tool(self, schedule):
        from andromeda_tools.scheduling import cron_spec

        return cron_spec(schedule, "/tmp/workspace")

    def test_a_created_job_is_read_only_and_says_so(self, schedule):
        result = self._tool(schedule).run(
            action="create", schedule="every 1h", prompt="check the build"
        )
        assert result.ok
        job = schedule.all()[0]
        assert job.origin == "agent" and job.approval_mode == "ask"
        assert "read-only" in result.content and "cron approve" in result.content

    def test_it_cannot_ask_for_auto(self, schedule):
        """There is no argument for the model to get wrong, and none for a
        prompt injection to set."""
        properties = self._tool(schedule).parameters["properties"]
        assert "approval" not in properties
        assert "auto" not in json.dumps(properties)

    def test_it_refuses_a_job_with_no_schedule(self, schedule):
        assert not self._tool(schedule).run(action="create", prompt="x").ok

    def test_it_refuses_a_job_with_no_prompt(self, schedule):
        assert not self._tool(schedule).run(action="create", schedule="every 1h").ok

    def test_a_bad_schedule_comes_back_as_a_result_not_a_crash(self, schedule):
        result = self._tool(schedule).run(
            action="create", schedule="whenever", prompt="x"
        )
        assert not result.ok and "neither" in result.content

    def test_it_can_create_the_cheap_shape(self, schedule):
        """A tool that can only create the expensive shape pushes every
        agent-made job into it. A watch source only ever removes runs."""
        result = self._tool(schedule).run(
            action="create",
            schedule="every 10m",
            prompt="Say what changed.",
            watch="probe.sh",
        )
        assert result.ok
        job = schedule.all()[0]
        assert job.is_monitored and job.monitor_source == "probe.sh"
        # Still read-only. Cheapness is not permission.
        assert job.approval_mode == "ask" and job.origin == "agent"

    def test_it_refuses_two_watch_sources(self, schedule):
        assert not self._tool(schedule).run(
            action="create", schedule="every 10m", prompt="x",
            watch="a.sh", watch_url="https://x",
        ).ok

    def test_it_cannot_deliver_to_an_arbitrary_target(self, schedule):
        """`webhook` needs a URL, and a URL from the model is a URL nobody
        agreed to send their job output to."""
        self._tool(schedule).run(
            action="create", schedule="every 10m", prompt="x", deliver="webhook"
        )
        assert schedule.all()[0].deliver == "none"

    def test_the_description_steers_toward_watching(self, schedule):
        assert "PREFER A WATCH SOURCE" in self._tool(schedule).description

    def test_it_cannot_run_a_job(self):
        """An agent turn nested inside an agent turn, with nothing supervising
        the pair. A person types `andromeda cron run`."""
        from andromeda_tools.scheduling import cron_spec

        actions = cron_spec(None, "/tmp").parameters["properties"]["action"]["enum"]
        assert "run" not in actions

    def test_the_summary_shows_the_schedule_and_the_prompt(self, schedule):
        spec = self._tool(schedule)
        summary = spec.summary(
            {"action": "create", "schedule": "every 1h", "prompt": "check the build"}
        )
        assert "every 1h" in summary and "check the build" in summary

    def test_it_is_gated(self, schedule):
        """Creating something that acts later, unwatched, is a decision a
        person should be shown before it is made."""
        assert self._tool(schedule).risk_tier == "destructive"


class TestTheNotepadTool:
    def _tool(self, tmp_path):
        from andromeda_tools.scheduling import notepad_spec

        return notepad_spec(notepad_module.Notepad(tmp_path / "n.json"), "job_1")

    def test_set_then_get(self, tmp_path):
        spec = self._tool(tmp_path)
        spec.run(action="set", key="cursor", value="42")
        assert "42" in spec.run(action="get", key="cursor").content

    def test_an_oversized_note_comes_back_as_a_result(self, tmp_path):
        spec = self._tool(tmp_path)
        result = spec.run(action="set", key="k", value="x" * 40_000)
        assert not result.ok and "cursor, not a cache" in result.content

    def test_it_is_not_gated(self, tmp_path):
        """Same tier as `memory_store`, and for the same reason: a job in the
        default mode runs on the narrowed belt, and gating this would mean no
        job could ever keep a cursor."""
        assert self._tool(tmp_path).risk_tier == "safe_local"

    def test_it_only_exists_where_there_is_a_job(self):
        from andromeda_tools import build_registry
        from andromeda_tools.todo import TodoList
        from andromeda_tools.workspace import Workspace

        assert "notepad" not in build_registry(Workspace(), TodoList())


class TestScheduleForms:
    @pytest.mark.parametrize(
        "expression",
        ["every 30m", "0 9 * * 1-5", "in 2h", "at 09:00", "at 2026-09-01T09:00"],
    )
    def test_accepted(self, expression):
        assert parse_schedule(expression)

    @pytest.mark.parametrize("expression", ["in 2x", "at nonsense", "nope", "every 5s"])
    def test_refused(self, expression):
        with pytest.raises(ScheduleError):
            parse_schedule(expression)

    def test_the_at_form_keeps_its_case(self):
        """`strptime`'s literal `T` is not reliably case-insensitive, so
        lowering the whole string leaves a schedule that parsed once and may
        not again."""
        assert parse_schedule("at 2026-09-01T09:00") == "at 2026-09-01T09:00"

    def test_a_bare_time_means_the_next_one(self):
        from andromeda_agent.schedule import next_fire

        assert next_fire(parse_schedule("at 00:01"), time.time()) > time.time()


class TestTheServiceFile:
    """The supervisor's file, which is the difference between "scheduled" and
    "actually runs"."""

    def _render(self, monkeypatch, tmp_path, system: str):
        from andromeda_cli.commands import service

        monkeypatch.setattr(service.platform, "system", lambda: system)
        monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "agent.plist")
        monkeypatch.setattr(service, "_unit_path", lambda: tmp_path / "unit.service")
        monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/systemctl")
        calls: list[list[str]] = []

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            service.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Done()
        )
        service.install()
        path = tmp_path / ("agent.plist" if system == "Darwin" else "unit.service")
        return path, path.read_text(encoding="utf-8"), calls

    def test_launchd_runs_the_daemon_in_the_foreground(self, monkeypatch, tmp_path):
        _path, body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert "<string>cron</string>" in body and "<string>daemon</string>" in body
        assert "KeepAlive" in body and "ThrottleInterval" in body

    def test_it_uses_this_interpreter_by_absolute_path(self, monkeypatch, tmp_path):
        """A user agent starts with almost none of a login shell's environment;
        `andromeda` on PATH works in a terminal and is not there at boot."""
        import sys

        _path, body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert sys.executable in body

    def test_reinstalling_unloads_the_old_agent_first(self, monkeypatch, tmp_path):
        """Otherwise the old one keeps running with the old arguments."""
        _path, _body, calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert any("bootout" in " ".join(call) for call in calls)
        assert [" ".join(c) for c in calls].index(
            next(" ".join(c) for c in calls if "bootout" in " ".join(c))
        ) < [" ".join(c) for c in calls].index(
            next(" ".join(c) for c in calls if "bootstrap" in " ".join(c))
        )

    def test_the_file_is_owner_only(self, monkeypatch, tmp_path):
        """It may hold a provider key, and LaunchAgents is not private."""
        path, _body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_only_the_named_variables_are_forwarded(self, monkeypatch, tmp_path):
        """A service file is world-readable on many systems, so this is a short
        list on purpose and not "the environment"."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-travel")
        monkeypatch.setenv("GITHUB_TOKEN", "also-must-not-travel")
        _path, body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert "sk-secret" in body
        assert "must-not-travel" not in body
        assert "also-must-not-travel" not in body

    def test_path_is_forwarded(self, monkeypatch, tmp_path):
        """A user agent's PATH is roughly /usr/bin:/bin. Without this, a job
        that calls `gh` works when you run it and fails when the scheduler
        does — which is the worst shape of bug available."""
        monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
        _path, body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert "/opt/homebrew/bin" in body

    def test_xml_special_characters_are_escaped(self, monkeypatch, tmp_path):
        """An unescaped `&` in a PATH entry makes a plist launchd refuses —
        after the install command has already said it worked."""
        from xml.dom import minidom

        monkeypatch.setenv("PATH", "/opt/a&b:/x<y>:/usr/bin")
        path, body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert "&amp;" in body and "&lt;" in body
        minidom.parseString(body)  # raises if the plist is malformed

    def test_a_systemd_value_with_a_quote_is_escaped(self):
        from andromeda_cli.commands import service

        assert service._systemd_value('a"b') == 'a\\"b'

    def test_the_installed_path_can_be_read_back(self, monkeypatch, tmp_path):
        """The file is the truth — it may predate the tool somebody is now
        wondering why a job cannot find."""
        from andromeda_cli.commands import service

        monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
        path, _body, _calls = self._render(monkeypatch, tmp_path, "Darwin")
        assert service._stored_path(path) == "/opt/homebrew/bin:/usr/bin"

    def test_systemd_restarts_always(self, monkeypatch, tmp_path):
        _path, body, _calls = self._render(monkeypatch, tmp_path, "Linux")
        assert "Restart=always" in body and "cron daemon" in body

    def test_uninstalling_leaves_the_jobs_alone(self, monkeypatch, tmp_path, capsys):
        """"Stop running these for now" is not "delete my automations"."""
        from andromeda_cli.commands import service

        monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "agent.plist")

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: _Done())
        (tmp_path / "agent.plist").write_text("x")
        assert service.uninstall() == 0
        assert not (tmp_path / "agent.plist").exists()
        assert "untouched" in capsys.readouterr().out

    def test_an_unsupported_platform_says_so_rather_than_pretending(
        self, monkeypatch, tmp_path
    ):
        from andromeda_cli.commands import service

        monkeypatch.setattr(service.platform, "system", lambda: "Plan9")
        assert service.install() == 2


# ---------------------------------------------------------------------------
# The ledger, suggestions, blueprints, per-job overrides
# ---------------------------------------------------------------------------


class TestTheLedger:
    """What was in flight when the machine went down."""

    def _ledger(self, tmp_path):
        from andromeda_agent.executions import Ledger

        return Ledger(tmp_path / "ex.db")

    def test_an_attempt_is_recorded_before_it_runs(self, tmp_path):
        """A row written after the work is a row that never exists for the
        attempts worth recording."""
        ledger = self._ledger(tmp_path)
        attempt = ledger.claim("job_1")
        assert ledger.recent()[0]["status"] == "claimed"
        ledger.running(attempt)
        assert ledger.recent()[0]["status"] == "running"

    def test_a_terminal_state_is_written_once(self, tmp_path):
        """A late process must not overwrite what actually happened."""
        ledger = self._ledger(tmp_path)
        attempt = ledger.claim("job_1")
        assert ledger.finish(attempt, ok=True)
        assert not ledger.finish(attempt, ok=False, error="too late")
        assert ledger.recent()[0]["status"] == "completed"

    def test_running_only_advances_from_claimed(self, tmp_path):
        ledger = self._ledger(tmp_path)
        attempt = ledger.claim("job_1")
        assert ledger.running(attempt)
        assert not ledger.running(attempt)

    def test_our_own_in_flight_attempt_is_never_recovered(self, tmp_path):
        """Otherwise a recovery sweep marks the run that started it abandoned."""
        ledger = self._ledger(tmp_path)
        ledger.claim("job_1")
        assert ledger.recover() == 0

    def test_an_attempt_from_a_dead_process_becomes_unknown(self, tmp_path, monkeypatch):
        from andromeda_agent import executions, liveness

        ledger = self._ledger(tmp_path)
        # Written as if by a previous scheduler, then that scheduler is gone.
        # Both halves are required: the process id is what stops a sweep
        # reaping its own in-flight rows, the liveness check is what stops it
        # reaping a scheduler that is still working.
        monkeypatch.setattr(executions, "_PROCESS_ID", "a-previous-scheduler")
        ledger.claim("job_1")
        monkeypatch.undo()
        monkeypatch.setattr(liveness, "pid_exists", lambda _pid: False)

        assert ledger.recover() == 1
        assert ledger.recent()[0]["status"] == "unknown"

    def test_a_sweep_never_reaps_its_own_rows(self, tmp_path, monkeypatch):
        """Even when the pid check would say the owner is gone. The id is the
        first gate for exactly this reason."""
        from andromeda_agent import executions, liveness

        ledger = self._ledger(tmp_path)
        ledger.claim("job_1")
        monkeypatch.setattr(liveness, "pid_exists", lambda _pid: False)
        assert ledger.recover() == 0

    def test_a_live_owner_is_left_alone(self, tmp_path, monkeypatch):
        """Pids are reused. Marking a live attempt abandoned is how you get
        two copies of the side effect."""
        from andromeda_agent import executions, liveness

        ledger = self._ledger(tmp_path)
        monkeypatch.setattr(executions, "_PROCESS_ID", "someone-else")
        ledger.claim("job_1")
        monkeypatch.setattr(liveness, "pid_exists", lambda _pid: True)
        monkeypatch.setattr(liveness, "process_start_time", lambda _pid: None)
        assert ledger.recover() == 0

    def test_unknown_is_not_a_retry_queue(self, tmp_path, monkeypatch):
        """It records that side effects may have run. Nothing re-runs."""
        from andromeda_agent import executions, liveness

        ledger = self._ledger(tmp_path)
        monkeypatch.setattr(executions, "_PROCESS_ID", "gone")
        attempt = ledger.claim("job_1")
        monkeypatch.undo()
        monkeypatch.setattr(liveness, "pid_exists", lambda _pid: False)
        assert ledger.recover() == 1
        # Terminal, so nothing can rewrite it into a fresh attempt.
        assert not ledger.finish(attempt, ok=True)
        assert executions.ABANDONED in ledger.recent()[0]["error"]


class TestSilence:
    """A job that says "nothing to report" every hour trains you to ignore it."""

    @pytest.mark.parametrize(
        "text", ["[SILENT]", "  [silent]  ", "SILENT", "[SILENT]\nnothing new", "ok\n[SILENT]"]
    )
    def test_recognised(self, text):
        from andromeda_agent.delivery import is_silence

        assert is_silence(text)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Real report here",
            "I considered replying [SILENT] but here is the summary you asked for",
        ],
    )
    def test_a_genuine_report_is_delivered(self, text):
        from andromeda_agent.delivery import is_silence

        assert not is_silence(text)

    def test_a_silent_run_is_still_recorded(self, home, schedule):
        """It suppresses delivery, never the record."""
        job = schedule.add("every 1m", "report", "", deliver="stdout")
        run = run_job(job, schedule, home, Recorder(answer="[SILENT]"))
        assert run.status == "silent"
        assert run.output_path and Path(run.output_path).exists()


class TestSuggestions:
    def _store(self, tmp_path):
        from andromeda_agent.suggestions import Suggestions

        return Suggestions(tmp_path / "s.json")

    def _spec(self):
        return {"schedule": "every 1h", "prompt": "check it", "name": "check"}

    def test_a_proposal_never_creates_a_job(self, tmp_path, schedule):
        store = self._store(tmp_path)
        store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        assert schedule.all() == []

    def test_accepting_creates_it_through_the_one_path(self, tmp_path, schedule):
        store = self._store(tmp_path)
        store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        _suggestion, job = store.accept("1", schedule, "/tmp")
        assert job is not None and schedule.all() == [job]

    def test_a_dismissal_latches(self, tmp_path):
        """Re-offering something somebody said no to is how a list stops
        being read."""
        store = self._store(tmp_path)
        store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        store.dismiss("1")
        again = store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        assert again is None and store.pending() == []

    def test_an_accepted_one_is_not_re_offered(self, tmp_path, schedule):
        store = self._store(tmp_path)
        store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        store.accept("1", schedule, "/tmp")
        assert store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        ) is None

    def test_the_backlog_is_capped(self, tmp_path):
        """Twenty things to decide about is a list nobody reads."""
        from andromeda_agent.suggestions import MAX_PENDING

        store = self._store(tmp_path)
        for index in range(MAX_PENDING + 3):
            store.propose(
                title=f"Check {index}", description="", source="catalog",
                spec=self._spec(), dedup_key=f"catalog:{index}",
            )
        assert len(store.pending()) == MAX_PENDING

    def test_an_unknown_source_is_refused(self, tmp_path):
        from andromeda_agent.suggestions import SuggestionError

        with pytest.raises(SuggestionError):
            self._store(tmp_path).propose(
                title="x", description="", source="telepathy",
                spec={}, dedup_key="x",
            )

    def test_a_spec_is_validated_only_when_accepted(self, tmp_path, schedule):
        """Storage is inert data. A suggestion cannot smuggle in an `auto` job
        by being written to disk."""
        store = self._store(tmp_path)
        store.propose(
            title="Bad", description="", source="usage",
            spec={"schedule": "whenever", "prompt": "x"}, dedup_key="usage:bad",
        )
        with pytest.raises(ScheduleError):
            store.accept("1", schedule, "/tmp")

    def test_it_survives_a_reload(self, tmp_path):
        from andromeda_agent.suggestions import Suggestions

        store = self._store(tmp_path)
        store.propose(
            title="Check", description="", source="catalog",
            spec=self._spec(), dedup_key="catalog:check",
        )
        assert len(Suggestions(tmp_path / "s.json").pending()) == 1

    def test_clearing_keeps_the_dismissals(self, tmp_path, schedule):
        """They are the latch. Forgetting them re-offers everything."""
        store = self._store(tmp_path)
        store.propose(
            title="A", description="", source="catalog", spec=self._spec(), dedup_key="a"
        )
        store.propose(
            title="B", description="", source="catalog", spec=self._spec(), dedup_key="b"
        )
        store.accept("1", schedule, "/tmp")
        store.dismiss("1")
        store.clear_resolved()
        assert [item.dedup_key for item in store.all()] == ["b"]


class TestSeeding:
    def _store(self, tmp_path):
        from andromeda_agent.suggestions import Suggestions

        return Suggestions(tmp_path / "s.json")

    def test_the_catalog_seeds_once(self, tmp_path):
        from andromeda_agent import seeding

        store = self._store(tmp_path)
        first = seeding.seed_catalog(store)
        assert first
        assert seeding.seed_catalog(store) == []

    def test_every_catalog_entry_fills_its_own_blueprint(self, tmp_path):
        """A catalog entry that cannot fill its blueprint is a bug in the
        catalog, and it would be invisible without this."""
        from andromeda_agent import blueprints, seeding

        for key, values in seeding.CATALOG:
            blueprint = blueprints.get(key)
            assert blueprint is not None, key
            assert blueprints.fill(blueprint, values)["schedule"]

    def test_a_skill_blueprint_becomes_a_suggestion_not_a_job(self, tmp_path):
        """Installing a skill must not schedule work on somebody's machine."""
        from andromeda_agent import seeding

        class _Skill:
            name = "triage"
            metadata = {
                "blueprint": {
                    "schedule": "0 9 * * 1-5",
                    "prompt": "Run the morning triage.",
                    "name": "Morning triage",
                }
            }

        store = self._store(tmp_path)
        made = seeding.seed_skill_blueprints(store, {"triage": _Skill()})
        assert len(made) == 1 and made[0].source == "blueprint"

    def test_a_skill_without_a_usable_block_is_skipped(self, tmp_path):
        from andromeda_agent import seeding

        class _Skill:
            name = "x"
            metadata = {"blueprint": {"name": "no schedule"}}

        assert seeding.seed_skill_blueprints(self._store(tmp_path), {"x": _Skill()}) == []

    def test_usage_needs_evidence_not_inference(self, tmp_path):
        """One mention is a question. Three is a habit."""
        from andromeda_agent import seeding

        store = self._store(tmp_path)
        assert seeding.seed_from_usage(store, ["every morning check the build"]) == []
        assert seeding.seed_from_usage(store, ["every morning check the build"] * 3)

    def test_an_unrelated_prompt_proposes_nothing(self, tmp_path):
        from andromeda_agent import seeding

        assert seeding.seed_from_usage(
            self._store(tmp_path), ["what is 2+2", "fix this bug", "read the file"] * 3
        ) == []


class TestBlueprints:
    def test_a_time_and_weekdays_become_cron(self):
        from andromeda_agent import blueprints

        spec = blueprints.fill(
            blueprints.get("repo-digest"),
            {"time": "08:30", "recurrence": "weekdays", "deliver": "none"},
        )
        assert spec["schedule"] == "30 8 * * 1-5"

    def test_a_typo_is_refused_not_defaulted(self):
        """A `--tiem 07:15` that silently creates a job at the default time is
        the worst outcome: it works, it is wrong, and nothing says so."""
        from andromeda_agent import blueprints

        with pytest.raises(blueprints.BlueprintError, match="unknown option"):
            blueprints.fill(blueprints.get("repo-digest"), {"tiem": "07:15"})

    def test_a_bad_time_is_refused(self):
        from andromeda_agent import blueprints

        with pytest.raises(blueprints.BlueprintError, match="invalid time"):
            blueprints.fill(
                blueprints.get("repo-digest"), {"time": "25:99", "recurrence": "everyday"}
            )

    def test_a_strict_enum_is_enforced(self):
        from andromeda_agent import blueprints

        with pytest.raises(blueprints.BlueprintError, match="not one of"):
            blueprints.fill(
                blueprints.get("watch-url"),
                {"url": "https://x", "interval_min": "7", "deliver": "none"},
            )

    def test_a_non_strict_enum_accepts_anything(self):
        """The deliverable set depends on what the machine has configured."""
        from andromeda_agent import blueprints

        spec = blueprints.fill(
            blueprints.get("watch-url"),
            {"url": "https://x", "interval_min": "30", "deliver": "webhook"},
        )
        assert spec["deliver"] == "webhook"

    def test_the_monitor_blueprints_produce_monitor_jobs(self):
        from andromeda_agent import blueprints

        spec = blueprints.fill(
            blueprints.get("watch-url"),
            {"url": "https://x", "interval_min": "30", "deliver": "none"},
        )
        assert spec["monitor_kind"] == "url" and spec["monitor_source"] == "https://x"

    def test_the_watchdog_blueprint_produces_a_no_agent_job(self):
        from andromeda_agent import blueprints

        spec = blueprints.fill(
            blueprints.get("watchdog"),
            {"script": "check.sh", "interval_min": "10", "deliver": "notify"},
        )
        assert spec["no_agent"] is True and spec["script"] == "check.sh"

    def test_a_free_text_schedule_passes_through(self):
        from andromeda_agent import blueprints

        spec = blueprints.fill(
            blueprints.get("follow-up"),
            {"task": "check the deploy", "schedule": "in 2h", "deliver": "none"},
        )
        assert spec["schedule"] == "in 2h"

    def test_every_blueprint_produces_a_spec_the_scheduler_accepts(self, schedule):
        """There is no second job schema. This is what says so."""
        from andromeda_agent import blueprints

        for blueprint in blueprints.CATALOG:
            # Every slot, and only this blueprint's slots — `fill` rejects a
            # name it does not know, which is the behaviour two tests up.
            filler = {
                "url": "https://example.com",
                "script": "check.sh",
                "task": "check something",
            }
            values = {
                slot.name: (
                    slot.default
                    if slot.default not in (None, "")
                    else filler.get(slot.name, "x")
                )
                for slot in blueprint.slots
            }
            spec = blueprints.fill(blueprint, values)
            expression = spec.pop("schedule")
            prompt = spec.pop("prompt")
            assert schedule.add(expression, prompt, "/tmp", **spec)

    def test_the_form_schema_covers_every_slot(self):
        from andromeda_agent import blueprints

        for blueprint in blueprints.CATALOG:
            schema = blueprints.form_schema(blueprint)
            assert len(schema["fields"]) == len(blueprint.slots)


class TestPerJobOverrides:
    def test_a_job_can_pin_its_own_thinking_level(self, home, schedule):
        job = schedule.add("every 1m", "x", "", thinking="high")
        seen: dict = {}

        def build(settings, workspace, current):
            seen.update(settings)

            class _Turn:
                def send(self, prompt):
                    return "ok"

            return _Turn()

        runner.execute(
            job, schedule, {"thinking": "off"}, home, build=build,
            notepad=notepad_module.Notepad(home / "n.json"),
        )
        assert seen["thinking"] == "high"

    def test_a_job_cannot_pin_a_model_this_build_does_not_serve(self, schedule):
        """The BYOK lane has no server-side backstop, so the allowlist has to
        be enforced where a job is written as well as where it is used."""
        with pytest.raises(ScheduleError, match="serves"):
            schedule.add("every 1m", "x", "/tmp", model="openai/gpt-4")

    def test_narrowing_the_toolbelt_can_only_subtract(self, home, schedule):
        """A job cannot name a tool the machine switched off and get it back."""
        job = schedule.add("every 1m", "x", "", enabled_tools=["read_file", "terminal"])
        seen: dict = {}

        def build(settings, workspace, current):
            seen.update(settings)

            class _Turn:
                def send(self, prompt):
                    return "ok"

            return _Turn()

        runner.execute(
            job, schedule, {"enabled_tools": ["read_file", "list_dir"]}, home,
            build=build, notepad=notepad_module.Notepad(home / "n.json"),
        )
        assert seen["enabled_tools"] == ["read_file"]

    def test_a_webhook_delivery_needs_somewhere_to_post(self, schedule):
        with pytest.raises(ScheduleError, match="needs a URL"):
            schedule.add("every 1m", "x", "/tmp", deliver="webhook")

    def test_named_skills_reach_the_prompt(self, home, schedule):
        job = schedule.add("every 1m", "do it", "", skills=["triage"])
        agent = Recorder()
        run_job(job, schedule, home, agent)
        assert "triage" in agent.prompts[0]


class TestTheProviderSeam:
    def test_the_built_in_is_always_available(self):
        from andromeda_agent import providers_cron

        assert providers_cron.get().name == "built-in"

    def test_an_unknown_provider_falls_back_rather_than_refusing(self):
        """A typo in a setting must not silently stop every scheduled job."""
        from andromeda_agent import providers_cron

        assert providers_cron.get("cloud-thing").name == "built-in"

    def test_a_provider_decides_timing_and_nothing_else(self):
        """It cannot widen a job — that would be a way to launder consent."""
        from andromeda_agent import providers_cron

        methods = set(dir(providers_cron.BuiltIn))
        assert "add" not in methods and "approve" not in methods


def test_reloading_a_store_drops_a_job_someone_removed(tmp_path):
    """`load` replaces what is held; it does not merge into it.

    The hosted runner re-reads on every fire, because jobs are created and
    removed by other processes while it sleeps. Merging would keep a deleted job
    alive in memory and let it fire after somebody removed it, which is the one
    thing a person expects `cron rm` to make impossible.
    """
    path = tmp_path / "cron.json"
    first = Schedule(path)
    kept = first.add("every 1h", "keep me", str(tmp_path))
    doomed = first.add("every 1h", "remove me", str(tmp_path))

    second = Schedule(path)
    assert second.resolve(doomed.id) is not None

    first.remove(doomed.id)
    second.load()

    assert second.resolve(doomed.id) is None
    assert second.resolve(kept.id) is not None


def test_a_corrupt_store_keeps_the_last_good_jobs_rather_than_emptying(tmp_path):
    """Running the last known-good set beats running nothing, and the file is
    left for a person to look at."""
    path = tmp_path / "cron.json"
    store = Schedule(path)
    job = store.add("every 1h", "do the thing", str(tmp_path))

    path.write_text("{ not json", encoding="utf-8")
    store.load()

    assert store.resolve(job.id) is not None


def test_an_unreadable_store_is_an_error_not_an_empty_schedule(tmp_path):
    """"I could not read the store" must never read as "there are no jobs".

    Found on a real deployment: a management command run over `fly ssh`
    executes as root and wrote `cron.json` 0600 root-owned. The runner runs
    unprivileged, its read raised `PermissionError`, and `load` swallowed it —
    so the fire endpoint answered "no such job" for a job the operator had seen
    in `cron list` one command earlier.
    """
    path = tmp_path / "cron.json"
    store = Schedule(path)
    job = store.add("every 1h", "do the thing", str(tmp_path))

    path.chmod(0o000)
    try:
        store.load()
        # The jobs already held are kept — running the last known-good set beats
        # running nothing.
        assert store.resolve(job.id) is not None
        assert "could not be read" in store.load_error
    finally:
        path.chmod(0o600)


def test_a_readable_store_clears_a_previous_error(tmp_path):
    """Otherwise one bad read poisons every surface until a restart."""
    path = tmp_path / "cron.json"
    store = Schedule(path)
    store.add("every 1h", "do the thing", str(tmp_path))

    path.chmod(0o000)
    store.load()
    assert store.load_error
    path.chmod(0o600)
    store.load()
    assert store.load_error == ""


def test_a_missing_store_is_genuinely_empty_and_not_an_error(tmp_path):
    """The one case where "no jobs" is the truth."""
    store = Schedule(tmp_path / "never-written.json")
    assert store.all() == []
    assert store.load_error == ""


def test_corrupt_json_says_so_and_keeps_the_last_good_set(tmp_path):
    path = tmp_path / "cron.json"
    store = Schedule(path)
    job = store.add("every 1h", "do the thing", str(tmp_path))

    path.write_text("{ not json", encoding="utf-8")
    store.load()

    assert store.resolve(job.id) is not None
    assert "not valid JSON" in store.load_error
