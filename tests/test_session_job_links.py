"""Sessions, and the jobs they left running.

The rail groups past conversations by what each one spawned. Three separate
mechanisms have to agree for that to be true, and each is tested here:

- `Schedule.session_kinds` derives the grouping from the live job store.
- The `cron` tool binds a job it creates to the conversation that asked.
- `_fold_cloud_runs` brings a cloud run's report home, where the container that
  produced it could not.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from andromeda_agent.schedule import Schedule
from andromeda_tools import scheduling


@pytest.fixture
def schedule(tmp_path):
    return Schedule(tmp_path / "cron.json")


class TestSessionKinds:
    def test_a_session_is_labelled_by_where_its_job_runs(self, schedule):
        schedule.add("every 1h", "watch", "", name="Local", attach_to="s1")
        schedule.add(
            "every 1h", "digest", "", name="Cloud", attach_to="s2",
            runs_on="cloud", workspace_kind="detached",
        )
        assert schedule.session_kinds() == {"s1": "local", "s2": "cloud"}

    def test_a_job_with_no_session_labels_nothing(self, schedule):
        schedule.add("every 1h", "orphan", "", name="Orphan")
        assert schedule.session_kinds() == {}

    def test_cloud_wins_a_tie(self, schedule):
        """A session that spawned both is filed under cloud.

        Cloud is the surprising one. Someone scanning for "what runs while I am
        away" must not have it hidden behind the ordinary case.
        """
        schedule.add("every 1h", "a", "", name="Local", attach_to="s1")
        schedule.add(
            "every 1h", "b", "", name="Cloud", attach_to="s1",
            runs_on="cloud", workspace_kind="detached",
        )
        assert schedule.session_kinds()["s1"] == "cloud"

    def test_the_grouping_follows_the_job_store(self, schedule):
        """Derived, not stored — so removing a job unlabels its session at once.

        This is the whole reason the kind is not written onto the session when
        it is created: a stored flag would go on claiming a job that no longer
        exists and nothing would ever correct it.
        """
        job = schedule.add("every 1h", "watch", "", name="Local", attach_to="s1")
        assert schedule.session_kinds() == {"s1": "local"}
        schedule.remove(job.id)
        assert schedule.session_kinds() == {}


class TestAgentCreatedJobsGetTheirOwnConversation:
    """Jobs used to attach to the conversation that created them. It read well
    in the design and badly in use: a job polling every five minutes wrote a
    message pair into a *live* chat every five minutes, interleaved with what
    the person was actually saying."""

    def test_the_job_does_not_write_into_the_chat_that_made_it(self, schedule):
        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        spec.run(action="create", schedule="every 1h", prompt="watch", name="W")
        job = schedule.all()[0]

        assert job.attach_to
        assert job.attach_to != "sess-abc"

    def test_it_remembers_where_it_came_from(self, schedule):
        """Recorded, never written to — it is what lets the job's own
        transcript say where it came from."""
        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        spec.run(action="create", schedule="every 1h", prompt="watch", name="W")
        assert schedule.all()[0].created_in == "sess-abc"

    def test_the_new_session_exists_and_says_what_it_is(self, schedule):
        from andromeda_cli import sessions as store

        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        spec.run(action="create", schedule="every 1h", prompt="watch", name="W")

        record = store.load(schedule.all()[0].attach_to)
        assert record is not None
        assert "W" in record.messages[0]["content"]
        assert "sess-abc" in record.messages[1]["content"]

    def test_the_caller_is_handed_the_link(self, schedule):
        """This is the only moment the person can be given the thread their job
        will talk in."""
        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        result = spec.run(
            action="create", schedule="every 1h", prompt="watch", name="W"
        )
        assert f"andromeda --resume {schedule.all()[0].attach_to}" in result.content
        assert "not this one" in result.content

    def test_a_job_still_gets_one_with_no_calling_session(self, schedule):
        """A lane or a piped run has no conversation to report into, but the
        job's output still has to land somewhere reachable."""
        spec = scheduling.cron_spec(schedule, "/tmp")
        spec.run(action="create", schedule="every 1h", prompt="watch", name="W")
        job = schedule.all()[0]
        assert job.attach_to
        assert job.created_in == ""

    def test_a_broken_session_store_does_not_fail_the_job(self, schedule, monkeypatch):
        """Runs are still reachable through `cron runs`. Falling back to the
        *creating* session would be worse than falling back to none."""
        from andromeda_cli import sessions as store

        def explode(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(store, "for_job", explode)
        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        result = spec.run(
            action="create", schedule="every 1h", prompt="watch", name="W"
        )
        assert result.ok
        assert schedule.all()[0].attach_to == ""

    def test_the_session_is_not_a_tool_parameter(self, schedule):
        """The model does not choose which conversation it is in.

        A session id the model could pass would be a way to write into somebody
        else's transcript, so it is bound at registration instead.
        """
        spec = scheduling.cron_spec(schedule, "/tmp", session_id="sess-abc")
        assert "session_id" not in spec.parameters["properties"]
        assert "attach_to" not in spec.parameters["properties"]


class TestCloudRunsComeHome:
    """A container cannot write to a session directory it does not have."""

    @pytest.fixture
    def cron(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
        from andromeda_cli.commands import cron as module

        return module

    def _session(self, text="watch my PRs"):
        from andromeda_cli import sessions as store

        record = store.Session(
            provider="p", model="m", workspace="/tmp",
            messages=[{"role": "user", "content": text}],
        )
        record.save()
        return record

    def test_a_finished_cloud_run_lands_in_its_session(self, cron):
        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="PR watch", attach_to=record.id,
            runs_on="cloud", workspace_kind="detached",
        )
        folded = cron._fold_cloud_runs(
            [{"jobId": job.id, "fireAt": "t1", "status": "ok", "summary": "PR #47 failed"}]
        )
        assert folded == 1

        from andromeda_cli import sessions as store

        again = store.load(record.id)
        assert again.messages[-1]["content"] == "PR #47 failed"
        assert "cloud" in again.messages[-2]["content"]

    def test_folding_twice_appends_once(self, cron):
        """Keyed on job AND fire time.

        A job id alone would fold only the first run of a repeating job; a
        summary alone would drop two genuinely identical reports from a watcher
        that found the same thing twice.
        """
        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to=record.id,
            runs_on="cloud", workspace_kind="detached",
        )
        rows = [{"jobId": job.id, "fireAt": "t1", "status": "ok", "summary": "one"}]
        assert cron._fold_cloud_runs(rows) == 1
        assert cron._fold_cloud_runs(rows) == 0

        rows.append({"jobId": job.id, "fireAt": "t2", "status": "ok", "summary": "one"})
        assert cron._fold_cloud_runs(rows) == 1

        from andromeda_cli import sessions as store

        assert len(store.load(record.id).messages) == 5

    def test_unfinished_and_quiet_runs_say_nothing(self, cron):
        """`fired` has not finished; `silent` and `no_change` chose not to speak."""
        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to=record.id,
            runs_on="cloud", workspace_kind="detached",
        )
        folded = cron._fold_cloud_runs([
            {"jobId": job.id, "fireAt": "a", "status": "fired", "summary": "running"},
            {"jobId": job.id, "fireAt": "b", "status": "silent", "summary": "[silent]"},
            {"jobId": job.id, "fireAt": "c", "status": "no_change", "summary": ""},
        ])
        assert folded == 0

        from andromeda_cli import sessions as store

        assert len(store.load(record.id).messages) == 1

    def test_a_failure_reports_its_error(self, cron):
        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to=record.id,
            runs_on="cloud", workspace_kind="detached",
        )
        cron._fold_cloud_runs(
            [{"jobId": job.id, "fireAt": "t1", "status": "failed", "error": "github: 401"}]
        )

        from andromeda_cli import sessions as store

        assert store.load(record.id).messages[-1]["content"] == "github: 401"

    def test_an_unwritable_session_is_not_retried_forever(self, cron):
        """Marked before the write is attempted, not after.

        A session that was pruned, or belongs to another machine, would
        otherwise be retried on every single launch for the life of the install.
        """
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to="gone",
            runs_on="cloud", workspace_kind="detached",
        )
        rows = [{"jobId": job.id, "fireAt": "t1", "status": "ok", "summary": "x"}]
        assert cron._fold_cloud_runs(rows) == 0
        assert json.loads(cron._folded_path().read_text()) == [f"{job.id}:t1"]

    def test_a_run_for_an_unknown_job_is_skipped(self, cron):
        assert cron._fold_cloud_runs(
            [{"jobId": "ghost", "fireAt": "t1", "status": "ok", "summary": "x"}]
        ) == 0

    def test_a_cloud_job_does_not_attach_on_the_container(self, cron):
        """`_attach` runs on the machine that did the work.

        For a cloud job that machine is a container whose session directory
        nobody can reach, so writing there would file the run in an unreachable
        transcript and mark the work done.
        """
        from andromeda_agent.schedule import Run

        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to=record.id,
            runs_on="cloud", workspace_kind="detached",
        )
        cron._attach(job, Run(started_at=0, finished_at=1, ok=True,
                              status="ok", summary="written on the container"))

        from andromeda_cli import sessions as store

        assert len(store.load(record.id).messages) == 1

    def test_a_local_job_still_attaches_directly(self, cron):
        from andromeda_agent.schedule import Run

        record = self._session()
        job = cron._schedule().add(
            "every 1h", "watch", "", name="W", attach_to=record.id
        )
        cron._attach(job, Run(started_at=0, finished_at=1, ok=True,
                              status="ok", summary="done here"))

        from andromeda_cli import sessions as store

        assert store.load(record.id).messages[-1]["content"] == "done here"
