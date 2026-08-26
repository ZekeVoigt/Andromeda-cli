"""One prompt over many inputs: rows, the ledger, resume."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from andromeda_agent import batch
from andromeda_cli.commands import batch_cmd


def write_rows(tmp_path: Path, lines: list[str], name: str = "rows.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


# ---------------------------------------------------------------------------
# reading rows
# ---------------------------------------------------------------------------


def test_a_jsonl_row_becomes_a_row(tmp_path):
    path = write_rows(tmp_path, ['{"id": "a", "prompt": "do a thing"}'])
    rows = batch.read_rows(path)
    assert rows[0].identifier == "a"
    assert rows[0].prompt == "do a thing"


def test_a_plain_line_becomes_a_row(tmp_path):
    """Refusing this would mean explaining a file format to somebody who has a
    list of names."""
    path = write_rows(tmp_path, ["alpha", "beta"], name="names.txt")
    rows = batch.read_rows(path, "Say hello to {input}")
    assert [row.prompt for row in rows] == ["Say hello to alpha", "Say hello to beta"]


def test_a_row_without_an_id_is_numbered(tmp_path):
    path = write_rows(tmp_path, ['{"prompt": "one"}', '{"prompt": "two"}'])
    assert [row.identifier for row in batch.read_rows(path)] == ["1", "2"]


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"prompt": "one"}\n\n  \n{"prompt": "two"}\n', encoding="utf-8")
    assert len(batch.read_rows(path)) == 2


def test_broken_json_names_the_line(tmp_path):
    path = write_rows(tmp_path, ['{"prompt": "fine"}', "{not json"])
    with pytest.raises(batch.BatchError) as caught:
        batch.read_rows(path)
    assert ":2" in str(caught.value)


def test_a_json_array_row_is_refused(tmp_path):
    path = write_rows(tmp_path, ["[1, 2, 3]"])
    with pytest.raises(batch.BatchError):
        batch.read_rows(path)


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(batch.BatchError) as caught:
        batch.read_rows(path)
    assert "no rows" in str(caught.value)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(batch.BatchError):
        batch.read_rows(tmp_path / "nope.jsonl")


def test_a_row_that_makes_no_prompt_names_its_fields(tmp_path):
    path = write_rows(tmp_path, ['{"subject": "hi"}'])
    with pytest.raises(batch.BatchError) as caught:
        batch.read_rows(path)
    assert "subject" in str(caught.value)


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_a_template_is_filled_from_the_row():
    assert batch.build_prompt({"name": "Ada", "day": "Tuesday"}, "{name} on {day}") == (
        "Ada on Tuesday"
    )


def test_an_unknown_placeholder_is_left_alone():
    """A dataset with one row missing a field should cost that row, not the
    run — so this does not raise the way a format string would."""
    assert batch.build_prompt({"name": "Ada"}, "{name} and {missing}") == (
        "Ada and {missing}"
    )


def test_without_a_template_the_row_speaks_for_itself():
    assert batch.build_prompt({"prompt": "written out"}, "") == "written out"
    assert batch.build_prompt({"input": "a line"}, "") == "a line"


def test_a_non_string_field_is_stringified():
    assert batch.build_prompt({"count": 3}, "there are {count}") == "there are 3"


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def test_each_result_lands_immediately(tmp_path):
    """A buffer holds the last however-many answers hostage to a clean exit
    that never came."""
    ledger = batch.Ledger(tmp_path / "out.jsonl")
    ledger.write(batch.Result(identifier="a", answer="one"))

    assert (tmp_path / "out.jsonl").read_text().count("\n") == 1

    ledger.write(batch.Result(identifier="b", answer="two"))
    assert (tmp_path / "out.jsonl").read_text().count("\n") == 2


def test_a_result_records_what_happened(tmp_path):
    ledger = batch.Ledger(tmp_path / "out.jsonl")
    ledger.write(
        batch.Result(identifier="a", answer="done", tools=["read_file"], seconds=1.234)
    )

    entry = json.loads((tmp_path / "out.jsonl").read_text())

    assert entry["id"] == "a"
    assert entry["ok"] is True
    assert entry["tools"] == ["read_file"]
    assert entry["seconds"] == 1.23


def test_a_failure_is_recorded_as_one(tmp_path):
    ledger = batch.Ledger(tmp_path / "out.jsonl")
    ledger.write(batch.Result(identifier="a", error="RuntimeError: nope"))
    entry = json.loads((tmp_path / "out.jsonl").read_text())
    assert entry["ok"] is False
    assert "nope" in entry["error"]


def test_the_ledger_is_appended_not_rewritten(tmp_path):
    path = tmp_path / "out.jsonl"
    batch.Ledger(path).write(batch.Result(identifier="a"))
    batch.Ledger(path).write(batch.Result(identifier="b"))
    assert len(path.read_text().splitlines()) == 2


def test_concurrent_writers_do_not_interleave(tmp_path):
    """Every line has to parse on its own, or a resume silently loses rows."""
    ledger = batch.Ledger(tmp_path / "out.jsonl")
    barrier = threading.Barrier(8)

    def write(index: int) -> None:
        barrier.wait()
        ledger.write(batch.Result(identifier=str(index), answer="x" * 500))

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = (tmp_path / "out.jsonl").read_text().splitlines()
    assert len(lines) == 8
    assert {json.loads(line)["id"] for line in lines} == {str(i) for i in range(8)}


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_recorded_ids_are_found(tmp_path):
    path = tmp_path / "out.jsonl"
    batch.Ledger(path).write(batch.Result(identifier="a"))
    batch.Ledger(path).write(batch.Result(identifier="b"))
    assert batch.already_done(path) == {"a", "b"}


def test_a_half_written_line_is_not_counted(tmp_path):
    """A half-written line is a row that did not finish, and the right response
    is to run it again."""
    path = tmp_path / "out.jsonl"
    path.write_text('{"id": "a", "ok": true}\n{"id": "b", "ok"', encoding="utf-8")
    assert batch.already_done(path) == {"a"}


def test_a_missing_ledger_has_nothing_done(tmp_path):
    assert batch.already_done(tmp_path / "nope.jsonl") == set()


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def rows(*prompts: str) -> list[batch.Row]:
    return [
        batch.Row(identifier=str(index), prompt=prompt)
        for index, prompt in enumerate(prompts, start=1)
    ]


def test_every_row_runs(tmp_path):
    seen: list[str] = []
    ledger = batch.Ledger(tmp_path / "out.jsonl")

    results = batch.run_batch(
        rows("one", "two", "three"),
        lambda prompt: (seen.append(prompt) or f"answered {prompt}", []),
        ledger,
    )

    assert seen == ["one", "two", "three"]
    assert [result.answer for result in results] == [
        "answered one",
        "answered two",
        "answered three",
    ]


def test_a_failing_row_does_not_stop_the_batch(tmp_path):
    """Two hundred rows where one has a bad path should produce a hundred and
    ninety-nine answers and one recorded failure."""

    def runner(prompt: str):
        if prompt == "two":
            raise RuntimeError("that one is broken")
        return f"answered {prompt}", []

    results = batch.run_batch(
        rows("one", "two", "three"), runner, batch.Ledger(tmp_path / "out.jsonl")
    )

    assert [result.ok for result in results] == [True, False, True]
    assert "that one is broken" in results[1].error
    assert len((tmp_path / "out.jsonl").read_text().splitlines()) == 3


def test_rows_can_run_at_once(tmp_path):
    import time

    def slow(prompt: str):
        time.sleep(0.1)
        return prompt, []

    started = time.time()
    batch.run_batch(
        rows(*[str(index) for index in range(6)]),
        slow,
        batch.Ledger(tmp_path / "out.jsonl"),
        jobs=6,
    )
    assert time.time() - started < 0.5


def test_results_come_back_in_row_order_however_they_finish(tmp_path):
    import time

    def uneven(prompt: str):
        time.sleep(0.05 if prompt == "one" else 0.0)
        return prompt, []

    results = batch.run_batch(
        rows("one", "two", "three"),
        uneven,
        batch.Ledger(tmp_path / "out.jsonl"),
        jobs=3,
    )
    assert [result.answer for result in results] == ["one", "two", "three"]


def test_the_summary_counts_what_happened(tmp_path):
    def runner(prompt: str):
        if prompt == "two":
            raise RuntimeError("no")
        return prompt, []

    results = batch.run_batch(
        rows("one", "two"), runner, batch.Ledger(tmp_path / "out.jsonl")
    )
    summary = batch.summarise(results)

    assert summary["rows"] == 2
    assert summary["ok"] == 1
    assert summary["failed"] == 1


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def test_a_dry_run_spends_nothing(tmp_path, capsys):
    path = write_rows(tmp_path, ['{"id": "a", "prompt": "one"}'])
    assert batch_cmd.run(str(path), dry_run=True) == 0

    out = capsys.readouterr().out
    assert "1 row(s) would run" in out
    assert not batch_cmd.default_ledger(path).exists()


def test_a_missing_file_is_reported(tmp_path, capsys):
    assert batch_cmd.run(str(tmp_path / "nope.jsonl")) == 2
    assert "No such file" in capsys.readouterr().err


def test_a_bad_row_is_reported_before_anything_runs(tmp_path, capsys):
    path = write_rows(tmp_path, ["{not json"])
    assert batch_cmd.run(str(path)) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_resume_skips_what_is_recorded(tmp_path, capsys):
    path = write_rows(
        tmp_path, ['{"id": "a", "prompt": "one"}', '{"id": "b", "prompt": "two"}']
    )
    batch.Ledger(batch_cmd.default_ledger(path)).write(batch.Result(identifier="a"))

    assert batch_cmd.run(str(path), resume=True, dry_run=True) == 0

    out = capsys.readouterr().out
    assert "1 row(s) would run" in out
    assert "1 already recorded" in out


def test_a_fully_recorded_batch_has_nothing_to_do(tmp_path, capsys):
    path = write_rows(tmp_path, ['{"id": "a", "prompt": "one"}'])
    batch.Ledger(batch_cmd.default_ledger(path)).write(batch.Result(identifier="a"))

    assert batch_cmd.run(str(path), resume=True) == 0
    assert "Nothing left to do" in capsys.readouterr().out


def test_results_can_be_read_back(tmp_path, capsys):
    ledger = tmp_path / "out.jsonl"
    batch.Ledger(ledger).write(batch.Result(identifier="a", answer="fine"))
    batch.Ledger(ledger).write(batch.Result(identifier="b", error="broke"))

    assert batch_cmd.show(str(ledger)) == 0

    out = capsys.readouterr().out
    assert "fine" in out
    assert "broke" in out


def test_only_the_failures_can_be_read_back(tmp_path, capsys):
    ledger = tmp_path / "out.jsonl"
    batch.Ledger(ledger).write(batch.Result(identifier="a", answer="fine"))
    batch.Ledger(ledger).write(batch.Result(identifier="b", error="broke"))

    batch_cmd.show(str(ledger), failures_only=True)

    out = capsys.readouterr().out
    assert "broke" in out
    assert "fine" not in out


def test_showing_a_missing_file_is_reported(tmp_path, capsys):
    assert batch_cmd.show(str(tmp_path / "nope.jsonl")) == 2
    assert "No such file" in capsys.readouterr().err


def test_the_verb_is_reachable_from_argv(tmp_path, capsys):
    path = write_rows(tmp_path, ['{"id": "a", "prompt": "one"}'])
    assert run(["batch", str(path), "--dry-run"]) == 0
    assert "would run" in capsys.readouterr().out


def test_show_does_not_need_a_path(tmp_path, capsys):
    ledger = tmp_path / "out.jsonl"
    batch.Ledger(ledger).write(batch.Result(identifier="a", answer="fine"))
    assert run(["batch", "--show", str(ledger)]) == 0
    assert "fine" in capsys.readouterr().out


def test_without_a_file_or_show_it_says_what_it_needs(capsys):
    assert run(["batch"]) == 2
    assert "needs a file" in capsys.readouterr().err
