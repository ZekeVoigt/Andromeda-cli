"""Scheduled jobs.

The property that matters most is the consent one: a job carries the approval
mode it was created with, and nothing that happens later widens it. Everything
else is bookkeeping.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from andromeda_agent.schedule import (
    APPROVAL_MODES,
    MAX_RUNS_KEPT,
    MIN_INTERVAL_SECONDS,
    Job,
    Run,
    Schedule,
    ScheduleError,
    next_fire,
    parse_schedule,
)


@pytest.fixture
def schedule(tmp_path):
    return Schedule(tmp_path / "cron.json")


class TestParsing:
    def test_a_cron_expression_is_accepted(self):
        assert parse_schedule("0 9 * * 1-5") == "0 9 * * 1-5"

    def test_whitespace_is_normalised(self):
        assert parse_schedule("  0   9 * * *  ") == "0 9 * * *"

    @pytest.mark.parametrize("interval", ["every 30m", "every 2h", "every 1d", "every 90s"])
    def test_intervals_are_accepted(self, interval):
        assert parse_schedule(interval) == interval

    def test_a_too_short_interval_is_refused(self):
        """A job firing faster than it finishes piles up."""
        with pytest.raises(ScheduleError, match=str(MIN_INTERVAL_SECONDS)):
            parse_schedule("every 5s")

    def test_nonsense_is_refused_with_examples(self):
        with pytest.raises(ScheduleError, match="every 30m"):
            parse_schedule("whenever")

    def test_an_empty_schedule_is_refused(self):
        with pytest.raises(ScheduleError):
            parse_schedule("   ")

    def test_an_unknown_unit_is_refused(self):
        with pytest.raises(ScheduleError, match="Unknown unit"):
            parse_schedule("every 5w")

    def test_a_non_numeric_interval_is_refused(self):
        with pytest.raises(ScheduleError):
            parse_schedule("every manym")

    def test_a_zero_interval_is_refused(self):
        with pytest.raises(ScheduleError):
            parse_schedule("every 0m")


class TestNextFire:
    def test_an_interval_advances_by_its_length(self):
        now = time.time()
        assert round(next_fire("every 30m", now) - now) == 1800

    def test_a_cron_expression_lands_in_the_future(self):
        now = time.time()
        assert next_fire("0 9 * * *", now) > now


class TestConsent:
    """The rule that makes an unattended run safe."""

    def test_the_mode_is_recorded_at_creation(self, schedule):
        job = schedule.add("every 1h", "do it", "/tmp", approval_mode="auto")
        assert job.approval_mode == "auto"

    def test_it_survives_a_reload(self, schedule, tmp_path):
        schedule.add("every 1h", "do it", "/tmp", approval_mode="auto")
        assert Schedule(tmp_path / "cron.json").all()[0].approval_mode == "auto"

    def test_an_unknown_mode_is_refused_at_creation(self, schedule):
        with pytest.raises(ScheduleError):
            schedule.add("every 1h", "do it", "/tmp", approval_mode="whatever")

    def test_a_corrupt_mode_reads_as_the_narrow_one(self):
        """A damaged field must never widen what a job may do."""
        job = Job.from_json(
            {"id": "j", "schedule": "every 1h", "prompt": "x", "approvalMode": "anything"}
        )
        assert job.approval_mode == "ask"

    def test_a_missing_mode_reads_as_the_narrow_one(self):
        job = Job.from_json({"id": "j", "schedule": "every 1h", "prompt": "x"})
        assert job.approval_mode == "ask"

    def test_the_job_mode_overrides_the_machine_setting(self, schedule, monkeypatch):
        """Consent belongs to the job, not to whatever the user set since."""
        from andromeda_cli.commands import cron as cron_cmd

        job = schedule.add("every 1h", "x", "/tmp", approval_mode="ask")
        seen = {}

        def fake_build_conversation(settings, provider, **kwargs):
            seen.update(settings)
            raise RuntimeError("stop here")

        monkeypatch.setattr(cron_cmd, "build_provider", lambda s: object())
        monkeypatch.setattr(cron_cmd, "build_conversation", fake_build_conversation)

        cron_cmd.execute(job, {"approval_mode": "auto", "model": "x"})
        assert seen["approval_mode"] == "ask"


class TestManaging:
    def test_a_job_needs_something_to_do(self, schedule):
        with pytest.raises(ScheduleError):
            schedule.add("every 1h", "   ", "/tmp")

    def test_the_name_falls_back_to_the_prompt(self, schedule):
        job = schedule.add("every 1h", "count the lines in data.txt", "/tmp")
        assert job.name.startswith("count the lines")

    def test_a_long_name_is_trimmed(self, schedule):
        job = schedule.add("every 1h", "x" * 200, "/tmp")
        assert len(job.name) <= 60

    def test_a_new_job_is_scheduled(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        assert job.next_run_at > time.time()

    def test_resolve_accepts_a_prefix(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        assert schedule.resolve(job.id[:8]) is job

    def test_resolve_accepts_a_name(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp", name="nightly")
        assert schedule.resolve("nightly") is job

    def test_an_ambiguous_prefix_resolves_to_nothing(self, schedule):
        schedule.add("every 1h", "a", "/tmp")
        schedule.add("every 1h", "b", "/tmp")
        assert schedule.resolve("job_") is None

    def test_remove_deletes_it(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        assert schedule.remove(job.id) is job
        assert schedule.all() == []

    def test_removing_something_unknown_returns_nothing(self, schedule):
        assert schedule.remove("nope") is None

    def test_disable_stops_it_being_due(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        job.next_run_at = time.time() - 1
        assert job.due() is True

        schedule.set_enabled(job.id, False)
        assert job.due() is False

    def test_enabling_reschedules_an_overdue_job(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        schedule.set_enabled(job.id, False)
        job.next_run_at = time.time() - 10_000

        schedule.set_enabled(job.id, True)
        assert job.next_run_at > time.time()


class TestDue:
    def test_only_due_jobs_are_returned(self, schedule):
        soon = schedule.add("every 1h", "soon", "/tmp")
        schedule.add("every 1h", "later", "/tmp")
        soon.next_run_at = time.time() - 1
        assert [job.id for job in schedule.due()] == [soon.id]

    def test_a_job_with_no_next_run_is_not_due(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        job.next_run_at = 0
        assert job.due() is False


class TestRuns:
    def test_a_run_is_recorded_and_the_job_rescheduled(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        job.next_run_at = time.time() - 1

        schedule.record(job, Run(started_at=time.time(), ok=True, summary="done"))
        assert job.last_run.ok is True
        assert job.next_run_at > time.time()

    def test_failures_are_recorded_too(self, schedule):
        """Otherwise 'ran and found nothing' and 'never ran' look the same."""
        job = schedule.add("every 1h", "x", "/tmp")
        schedule.record(job, Run(started_at=time.time(), ok=False, error="boom"))
        assert job.last_run.ok is False and job.last_run.error == "boom"

    def test_history_is_bounded(self, schedule):
        job = schedule.add("every 1h", "x", "/tmp")
        for _ in range(MAX_RUNS_KEPT + 20):
            job.record(Run(started_at=time.time()))
        assert len(job.runs) == MAX_RUNS_KEPT


class TestPersistence:
    def test_the_file_is_owner_only(self, schedule):
        schedule.add("every 1h", "x", "/tmp")
        assert stat.S_IMODE(os.stat(schedule.path).st_mode) == 0o600

    def test_jobs_round_trip(self, schedule, tmp_path):
        original = schedule.add("0 9 * * *", "the prompt", "/some/dir", name="nine")
        schedule.record(original, Run(started_at=time.time(), ok=True, summary="s"))

        reloaded = Schedule(tmp_path / "cron.json").all()[0]
        assert reloaded.schedule == "0 9 * * *"
        assert reloaded.prompt == "the prompt"
        assert reloaded.workspace == "/some/dir"
        assert reloaded.last_run.summary == "s"

    def test_a_corrupt_file_runs_nothing(self, tmp_path):
        path = tmp_path / "cron.json"
        path.write_text("{not json", encoding="utf-8")
        assert Schedule(path).all() == []

    def test_an_entry_missing_its_prompt_is_dropped(self, tmp_path):
        path = tmp_path / "cron.json"
        path.write_text(
            json.dumps({"jobs": [{"id": "j", "schedule": "every 1h"}]}), encoding="utf-8"
        )
        assert Schedule(path).all() == []

    def test_saving_leaves_no_temporary_file(self, schedule):
        schedule.add("every 1h", "x", "/tmp")
        assert not schedule.path.with_suffix(".json.tmp").exists()


def test_every_approval_mode_is_known_to_the_config():
    from andromeda_cli.config import VALID_VALUES

    assert set(APPROVAL_MODES) == set(VALID_VALUES["approval_mode"])
