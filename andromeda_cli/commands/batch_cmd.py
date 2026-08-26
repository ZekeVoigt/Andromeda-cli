"""`andromeda batch` — one prompt over many inputs.

```
andromeda batch tickets.jsonl --prompt "Classify this ticket: {body}"
andromeda batch tickets.jsonl --resume
```

Answers land in a JSONL as they arrive, so a run that stops half-way is a run
that can be resumed rather than repeated.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from andromeda_agent import Callbacks, batch as batch_module, build_provider
from andromeda_agent.errors import AgentError

from .. import config as config_module
from .. import output
from ..session import build_conversation


def default_ledger(source: Path) -> Path:
    return source.with_suffix(source.suffix + ".results.jsonl")


def run(
    path: str,
    prompt: str = "",
    out: str = "",
    jobs: int = 1,
    resume: bool = False,
    workspace: str = "",
    dry_run: bool = False,
) -> int:
    source = Path(path).expanduser()
    if not source.is_file():
        output.fail(f"No such file: {source}")
        return 2

    try:
        rows = batch_module.read_rows(source, prompt)
    except batch_module.BatchError as exc:
        output.fail(str(exc))
        return 2

    ledger_path = Path(out).expanduser() if out else default_ledger(source)

    skipped = 0
    if resume:
        done = batch_module.already_done(ledger_path)
        before = len(rows)
        rows = [row for row in rows if row.identifier not in done]
        skipped = before - len(rows)

    if dry_run:
        output.info(f"  {len(rows)} row(s) would run · results to {ledger_path}")
        if skipped:
            output.info(f"  {skipped} already recorded")
        for row in rows[:3]:
            output.console.print(f"      [dim]{row.identifier}: {row.prompt[:100]}[/dim]")
        if len(rows) > 3:
            output.console.print(f"      [dim]… and {len(rows) - 3} more[/dim]")
        return 0

    if not rows:
        output.ok(f"Nothing left to do — {skipped} row(s) already recorded.")
        return 0

    try:
        provider = build_provider(config_module.load())
    except AgentError as exc:
        output.agent_error(exc)
        return 1

    config = config_module.load()

    def runner(text: str) -> tuple[str, list[str]]:
        # A fresh conversation per row. Nothing one row says can reach the
        # next, which is what makes two rows' answers comparable at all.
        conversation, _record = build_conversation(
            config,
            provider,
            interactive=False,
            workspace_root=workspace or None,
            surface="batch",
        )
        used: list[str] = []
        answer = conversation.send(
            text, Callbacks(on_tool_start=lambda spec, _args: used.append(spec.name))
        )
        return answer, used

    lanes = f", {jobs} at a time" if jobs > 1 else ""
    output.info(f"  {len(rows)} row(s){lanes} · results to {ledger_path}")
    if skipped:
        output.info(f"  {skipped} already recorded, skipping")
    output.console.print()

    ledger = batch_module.Ledger(ledger_path)
    started = time.time()
    done = 0

    def announce(result) -> None:
        nonlocal done
        done += 1
        mark = "[green]ok[/green]" if result.ok else "[red]fail[/red]"
        detail = result.error if result.error else result.answer.replace("\n", " ")[:70]
        output.console.print(
            f"  {mark} [dim]{done}/{len(rows)}[/dim] "
            f"[cyan]{result.identifier}[/cyan] [dim]{detail}[/dim]"
        )

    try:
        results = batch_module.run_batch(
            rows, runner, ledger, jobs=max(1, jobs), on_result=announce
        )
    except KeyboardInterrupt:
        output.console.print()
        output.info(
            f"  stopped after {done} row(s) — andromeda batch {path} --resume"
        )
        return 130

    summary = batch_module.summarise(results)
    output.console.print()
    output.info(
        f"  {summary['ok']}/{summary['rows']} ok · {summary['seconds']:.0f}s "
        f"· {summary['average']:.0f}s each"
    )
    if summary["failed"]:
        output.info(f"  {summary['failed']} failed — they are in {ledger_path}")
        output.info(f"  andromeda batch {path} --resume   retries only those")
    return 1 if summary["failed"] else 0


def show(path: str, failures_only: bool = False) -> int:
    """Read a results file back."""
    ledger = Path(path).expanduser()
    if not ledger.is_file():
        output.fail(f"No such file: {ledger}")
        return 2

    shown = 0
    for entry in batch_module.read_results(ledger):
        if failures_only and entry.get("ok"):
            continue
        shown += 1
        mark = "[green]ok[/green]" if entry.get("ok") else "[red]fail[/red]"
        body = entry.get("error") or str(entry.get("answer", "")).replace("\n", " ")
        output.console.print(
            f"  {mark} [cyan]{entry.get('id')}[/cyan] [dim]{body[:100]}[/dim]"
        )

    if not shown:
        output.info("  nothing to show")
    return 0
