"""The same job over many inputs.

One prompt against two hundred rows: classify these tickets, summarise these
files, draft a reply to each of these. A shell loop does it too, and then the
laptop sleeps at item 137 and you have no idea which ones finished.

So the unit here is not the loop, it is the **ledger**. Every row's answer is
appended to a JSONL as soon as it exists, keyed by the row's own id, and a
`--resume` reads that file and skips what is already in it. The failure mode
this is built around is the ordinary one: it stopped, and you want the other
sixty without paying for the hundred and forty again.

Three rules:

**A row that fails does not stop the batch.** Its error is written to the
ledger like any other result, and the run continues. Two hundred rows where
one has a bad path should produce a hundred and ninety-nine answers and one
recorded failure.

**Rows are independent.** Each gets its own conversation, so nothing one row
says can change what the next one sees. That costs the prompt cache and buys
the only property that makes the results comparable.

**The ledger is append-only.** A resumed run appends; it never rewrites. A
crash mid-write costs one row, and the row is retried on the next resume
because a half-written line will not parse as a result.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


@dataclass
class Row:
    """One input, with whatever came with it."""

    identifier: str
    prompt: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    identifier: str
    answer: str = ""
    tools: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "ok": self.ok,
            "answer": self.answer,
            "tools": self.tools,
            "seconds": round(self.seconds, 2),
            "error": self.error,
        }


class BatchError(ValueError):
    pass


def read_rows(path: Path, template: str = "") -> list[Row]:
    """Read a JSONL or a plain text file into rows.

    JSONL is the shape a dataset arrives in; a text file with one item per line
    is the shape a person types. Both are accepted because refusing the second
    would mean explaining a file format to somebody who has a list of names.

    A row's id is its `id` field, then its line number. Ids matter more than
    they look: they are what `--resume` matches on, so a file whose rows are
    reordered between runs still resumes correctly when they carry ids, and
    resumes wrongly when they do not. Said here because it is the one thing a
    caller can get wrong invisibly.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatchError(f"could not read {path}: {exc}") from exc

    rows: list[Row] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        fields: dict[str, Any] = {}
        if line.startswith("["):
            # Refused rather than taken as text. A line of JSON that is not an
            # object is a mistake in a dataset, and treating it as a literal
            # prompt would send `[1, 2, 3]` to the model without a word.
            raise BatchError(
                f"{path}:{number} is a JSON array — each row must be an object "
                f"or a plain line of text"
            )
        if line.startswith("{"):
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BatchError(f"{path}:{number} is not valid JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise BatchError(f"{path}:{number} must be a JSON object")
            fields = loaded
        else:
            fields = {"input": line}

        identifier = str(fields.get("id") or number)
        prompt = build_prompt(fields, template)
        if not prompt.strip():
            raise BatchError(
                f"{path}:{number} produced an empty prompt — give the row a "
                f"`prompt` field, or pass a template naming one of "
                f"{', '.join(sorted(fields)) or 'its fields'}"
            )
        rows.append(Row(identifier=identifier, prompt=prompt, fields=fields))

    if not rows:
        raise BatchError(f"{path} has no rows")
    return rows


def build_prompt(fields: dict[str, Any], template: str) -> str:
    """Fill a template from a row, or use the row's own prompt.

    `{name}` substitution rather than a format string: an unknown placeholder
    is left alone instead of raising, because a dataset with one row missing a
    field should cost that row, not the run.
    """
    if not template:
        return str(fields.get("prompt") or fields.get("input") or "")

    out = template
    for key, value in fields.items():
        out = out.replace("{" + str(key) + "}", str(value))
    return out


def already_done(ledger: Path) -> set[str]:
    """Ids with a result already on record.

    Unparseable lines are ignored rather than repaired: a half-written line is
    a row that did not finish, and the right response is to run it again.
    """
    done: set[str] = set()
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return done

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id") is not None:
            done.add(str(entry["id"]))
    return done


class Ledger:
    """Append-only results, flushed per row.

    Flushed rather than buffered, and that is the whole point: a batch is worth
    resuming exactly when it did not finish, and a buffer holds the last
    however-many answers hostage to a clean exit that never came.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, result: Result) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result.as_dict(), ensure_ascii=False) + "\n")
                handle.flush()


def run_batch(
    rows: list[Row],
    runner: Callable[[str], tuple[str, list[str]]],
    ledger: Ledger,
    *,
    jobs: int = 1,
    on_result: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Run every row, recording each one as it lands."""
    results: list[Result] = []

    def one(row: Row) -> Result:
        started = time.time()
        try:
            answer, tools = runner(row.prompt)
            result = Result(
                identifier=row.identifier, answer=answer, tools=list(tools)
            )
        except Exception as exc:  # noqa: BLE001 - one bad row is not a bad batch
            result = Result(
                identifier=row.identifier, error=f"{type(exc).__name__}: {exc}"[:500]
            )
        result.seconds = time.time() - started
        ledger.write(result)
        if on_result is not None:
            on_result(result)
        return result

    if jobs <= 1:
        for row in rows:
            results.append(one(row))
        return results

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, row) for row in rows]
        for future in futures:
            results.append(future.result())
    return results


def summarise(results: list[Result]) -> dict[str, Any]:
    done = len(results)
    failed = sum(1 for result in results if not result.ok)
    seconds = sum(result.seconds for result in results)
    return {
        "rows": done,
        "ok": done - failed,
        "failed": failed,
        "seconds": round(seconds, 1),
        "average": round(seconds / done, 1) if done else 0.0,
    }


def read_results(ledger: Path) -> Iterator[dict[str, Any]]:
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry
