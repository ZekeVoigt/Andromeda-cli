"""The live view of a scheduled run.

What is worth pinning here is not that a file gets written. It is the four
things that make a journal usable by a surface that did not write it:

1. **A reader started after the fact sees nothing by default**, because those
   runs are already in the durable session copy and showing them again doubles
   them — while a reader that explicitly asks for history gets all of it.
2. **A record belongs to exactly one session**, so a job attached to one
   conversation never paints into another.
3. **A half-written line is not a corrupt read.** The writer appends while the
   reader polls; that race happens on every busy run.
4. **It never raises.** The work is worth more than the log of it.
"""

from __future__ import annotations

import json
import time

import pytest

from andromeda_agent import live


def _writer(home, session="s1", job="job_x"):
    return live.Writer(home, job_id=job, job_name="PR watch", session=session)


class TestTheHorizon:
    def test_a_new_surface_does_not_replay_the_day(self, tmp_path):
        """The default is "from now on".

        A surface opening at 3pm has no business painting the 6am run into a
        live transcript. That run reached the person through the durable copy
        already, and a second showing reads as the job having fired twice.
        """
        writer = _writer(tmp_path)
        writer.started()
        writer.finished("ok", summary="done")

        tail = live.Tail(tmp_path, session="s1")
        assert tail.poll() == []

        # But it sees what happens next.
        writer.note("something new")
        assert [record["kind"] for record in tail.poll()] == ["note"]

    def test_asking_for_history_gets_history(self, tmp_path):
        """`since=0` means "everything", and once meant "nothing".

        The bug: the first poll seeked to end of file *and* applied the
        caller's `since`, so a tail explicitly asking for the whole journal
        received an empty list. The two are now separate decisions —
        `from_start` is set by whether `since` was passed at all.
        """
        writer = _writer(tmp_path)
        writer.started()
        writer.text("hello")
        writer.finished("ok")

        found = live.Tail(tmp_path, since=0, session="s1").poll()
        assert [record["kind"] for record in found] == [
            "run.started",
            "text",
            "run.finished",
        ]

    def test_a_tail_opened_before_the_first_run_of_the_day_sees_it(self, tmp_path):
        """The race that decided the horizon lazily.

        A surface opened before any job had run that day created no file to
        measure. The first poll then arrived *after* the day's first run had
        written one, treated its records as history, and skipped them — so the
        run most worth showing was the one guaranteed to be dropped. The
        horizon is now fixed when the tail is constructed.
        """
        tail = live.Tail(tmp_path, session="s1")  # nothing on disk yet
        assert not live.journal_dir(tmp_path).exists()

        writer = _writer(tmp_path)
        writer.started(reason="scheduled")
        writer.finished("ok", summary="the first run of the day")

        kinds = [record["kind"] for record in tail.poll()]
        assert kinds == ["run.started", "run.finished"]

    def test_records_before_the_horizon_are_dropped(self, tmp_path):
        writer = _writer(tmp_path)
        writer.note("old")
        cutoff = time.time() + 0.01
        time.sleep(0.02)
        writer.note("new")

        found = live.Tail(tmp_path, since=cutoff, session="s1").poll()
        assert [record["text"] for record in found] == ["new"]


class TestWhoseRunItIs:
    def test_a_run_paints_only_into_its_own_session(self, tmp_path):
        _writer(tmp_path, session="s1").note("for one")
        _writer(tmp_path, session="s2").note("for two")

        one = live.Tail(tmp_path, since=0, session="s1").poll()
        two = live.Tail(tmp_path, since=0, session="s2").poll()

        assert [record["text"] for record in one] == ["for one"]
        assert [record["text"] for record in two] == ["for two"]

    def test_an_unfiltered_tail_sees_every_session(self, tmp_path):
        """For `cron runs --live`, which is about jobs rather than a session."""
        _writer(tmp_path, session="s1").note("one")
        _writer(tmp_path, session="s2").note("two")

        found = live.Tail(tmp_path, since=0).poll()
        assert len(found) == 2

    def test_a_job_with_no_session_is_still_journalled(self, tmp_path):
        """`attach_to` is optional. A job created from a shell has none.

        It must still be readable — `cron runs` wants it — and it must not leak
        into a session's live view, which the empty-string filter gives for
        free only because a session id is never empty.
        """
        _writer(tmp_path, session="").note("orphan")

        assert len(live.Tail(tmp_path, since=0).poll()) == 1
        assert live.Tail(tmp_path, since=0, session="s1").poll() == []


class TestTheRace:
    def test_a_partial_line_is_picked_up_whole_on_the_next_poll(self, tmp_path):
        """The writer appends while the reader polls. Every busy run does this.

        A reader that parsed the half-line would drop the record; one that
        advanced past it would drop it permanently. It rewinds instead.
        """
        writer = _writer(tmp_path)
        writer.note("complete")

        path = live.journal_dir(tmp_path) / sorted(
            entry.name for entry in live.journal_dir(tmp_path).iterdir()
        )[0]
        whole = json.dumps({"at": time.time(), "kind": "note", "session": "s1", "text": "torn"})
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(whole[: len(whole) // 2])

        tail = live.Tail(tmp_path, since=0, session="s1")
        assert [record["text"] for record in tail.poll()] == ["complete"]

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(whole[len(whole) // 2 :] + "\n")

        assert [record["text"] for record in tail.poll()] == ["torn"]

    def test_a_truncated_journal_is_re_read_rather_than_seeked_past(self, tmp_path):
        writer = _writer(tmp_path)
        writer.note("first")
        tail = live.Tail(tmp_path, since=0, session="s1")
        assert len(tail.poll()) == 1

        path = next(live.journal_dir(tmp_path).iterdir())
        path.write_text("", encoding="utf-8")
        writer.note("after the reap")

        assert [record["text"] for record in tail.poll()] == ["after the reap"]

    def test_polling_a_journal_that_does_not_exist_is_empty_not_an_error(self, tmp_path):
        assert live.Tail(tmp_path, since=0).poll() == []


class TestItNeverCostsTheRun:
    def test_an_unwritable_journal_does_not_raise(self, tmp_path):
        """A run whose journal failed is a run that happened."""
        blocker = tmp_path / "cron"
        blocker.write_text("not a directory", encoding="utf-8")

        writer = _writer(tmp_path)
        writer.started()
        writer.text("some output")
        writer.finished("ok", summary="done")  # no exception

    def test_an_unserialisable_field_does_not_raise(self, tmp_path):
        assert live.append(tmp_path, {"kind": "note", "text": object()}) is True


class TestBounds:
    def test_text_is_chunked_rather_than_truncated(self, tmp_path):
        """A long answer arrives in pieces; none of it is lost."""
        writer = _writer(tmp_path)
        writer.text("x" * (live.MAX_TEXT * 2 + 5))
        writer.flush()

        found = live.Tail(tmp_path, since=0, session="s1").poll()
        assert len(found) == 3
        assert sum(len(record["text"]) for record in found) == live.MAX_TEXT * 2 + 5

    def test_finishing_flushes_the_tail_of_the_answer(self, tmp_path):
        """The last words sit in the buffer until something flushes them."""
        writer = _writer(tmp_path)
        writer.text("the last words")
        writer.finished("ok")

        kinds = [record["kind"] for record in live.Tail(tmp_path, since=0, session="s1").poll()]
        assert kinds == ["text", "run.finished"]

    def test_old_journals_are_reaped(self, tmp_path):
        directory = live.journal_dir(tmp_path)
        directory.mkdir(parents=True)
        (directory / "20200101.jsonl").write_text("{}\n", encoding="utf-8")
        (directory / "notes.txt").write_text("keep me", encoding="utf-8")
        _writer(tmp_path).note("today")

        assert live.reap(tmp_path) == 1
        remaining = {entry.name for entry in directory.iterdir()}
        assert "20200101.jsonl" not in remaining
        assert "notes.txt" in remaining
        assert len(live.Tail(tmp_path, since=0, session="s1").poll()) == 1
