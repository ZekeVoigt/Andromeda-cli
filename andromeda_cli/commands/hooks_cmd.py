"""`andromeda hooks` — see, test and withdraw shell hooks.

Four verbs, and the split between them is the point:

  ``list``    what is configured, and whether it is allowed to run
  ``test``    fire one event now, against a synthetic payload
  ``revoke``  withdraw an approval
  ``doctor``  check every configured hook without waiting for a session

``test`` and ``doctor`` both execute the script, which is why neither will
touch a hook that has not been approved yet — the whole reason to run
``doctor`` after pulling someone else's config is to see what is *about to*
register, and a command that ran those scripts to tell you about them would
have already done the thing you were checking for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.text import Text

from andromeda_agent import hooks as hooks_module
from andromeda_agent import shell_hooks

from .. import config as config_module
from .. import output

# Synthetic payloads, in the exact shape each fire site passes. Routed through
# the same serialiser a live firing uses, so a script that works under
# `hooks test` works in a session — anything less makes this command a second
# implementation that drifts from the first.
PAYLOADS: dict[str, dict[str, Any]] = {
    "pre_tool_call": {
        "tool_name": "terminal",
        "args": {"command": "echo hello"},
        "session_id": "test-session",
        "tool_call_id": "test-call",
        "risk_tier": "destructive",
        "step": 0,
    },
    "post_tool_call": {
        "tool_name": "terminal",
        "args": {"command": "echo hello"},
        "session_id": "test-session",
        "tool_call_id": "test-call",
        "risk_tier": "destructive",
        "step": 0,
        "status": "ok",
        "result": "hello",
        "error_message": None,
        "duration_ms": 12.5,
    },
    "transform_tool_result": {
        "tool_name": "read_file",
        "args": {"path": "README.md"},
        "session_id": "test-session",
        "tool_call_id": "test-call",
        "risk_tier": "safe_local",
        "step": 0,
        "status": "ok",
        "text": "the tool's output",
    },
    "pre_llm_call": {
        "session_id": "test-session",
        "model": "test-model",
        "message_count": 4,
        "step": 0,
        "user_message": "What changed today?",
    },
    "post_llm_call": {
        "session_id": "test-session",
        "model": "test-model",
        "step": 0,
        "content_chars": 120,
        "tool_call_count": 1,
    },
    "transform_llm_output": {
        "session_id": "test-session",
        "model": "test-model",
        "steps_taken": 3,
        "text": "All done — the change is applied.",
    },
    "on_session_start": {
        "session_id": "test-session",
        "model": "test-model",
        "surface": "repl",
    },
    "on_session_end": {
        "session_id": "test-session",
        "model": "test-model",
        "surface": "repl",
        "turn_count": 7,
        "completed": True,
    },
    "on_session_reset": {
        "session_id": "test-session",
        "model": "test-model",
        "surface": "repl",
        "turn_count": 7,
    },
    "on_compaction": {
        "session_id": "test-session",
        "stage": "summarise",
        "before_tokens": 98_000,
        "after_tokens": 24_000,
        "pruned_results": 12,
        "summarised_messages": 40,
    },
    "pre_approval_request": {
        "tool_name": "terminal",
        "summary": "rm -rf build",
        "risk_tier": "destructive",
        "session_id": "test-session",
        "surface": "repl",
    },
    "post_approval_response": {
        "tool_name": "terminal",
        "summary": "rm -rf build",
        "risk_tier": "destructive",
        "session_id": "test-session",
        "surface": "repl",
        "answer": "once",
    },
    "subagent_start": {
        "parent_session_id": "test-session",
        "specialist_id": "scout",
        "run_id": "lane-1",
        "task": "Find every caller of build_registry.",
    },
    "subagent_stop": {
        "parent_session_id": "test-session",
        "specialist_id": "scout",
        "run_id": "lane-1",
        "task": "Find every caller of build_registry.",
        "status": "completed",
        "summary": "Four callers, all in andromeda_cli.",
        "tool_call_history": ["search_files", "read_file"],
        "duration_ms": 4210.0,
    },
    "pre_command": {
        "surface": "repl",
        "command": "tools",
        "args_raw": "",
    },
    "on_job_start": {
        "job_id": "job-1",
        "job_name": "morning digest",
        "kind": "agent",
        "scheduled_for": 0.0,
    },
    "on_job_end": {
        "job_id": "job-1",
        "job_name": "morning digest",
        "kind": "agent",
        "scheduled_for": 0.0,
        "status": "ok",
        "duration_ms": 8100.0,
        "output_chars": 640,
    },
}


def _specs() -> list[shell_hooks.ShellHookSpec]:
    return shell_hooks.iter_configured_hooks(config_module.load())


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def show_list() -> int:
    specs = _specs()
    if not specs:
        output.info(f"No hooks configured in {config_module.config_path()}")
        output.info("  andromeda hooks --help   # the config shape, with examples")
        return 0

    approvals = {
        (entry.get("event"), entry.get("command"))
        for entry in shell_hooks.load_allowlist().get("approvals", [])
        if isinstance(entry, dict)
    }

    by_event: dict[str, list[shell_hooks.ShellHookSpec]] = {}
    for spec in specs:
        by_event.setdefault(spec.event, []).append(spec)

    plural = "" if len(specs) == 1 else "s"
    output.info(f"  {len(specs)} hook{plural} configured\n")

    for event in sorted(by_event):
        output.console.print(f"  [cyan]{event}[/cyan]")
        for spec in by_event[event]:
            allowed = (spec.event, spec.command) in approvals
            mark = "[green]✓[/green]" if allowed else "[red]✗[/red]"
            state = "allowed" if allowed else "not approved — will not fire"
            matcher = f" matcher={spec.matcher!r}" if spec.matcher else ""
            closed = " fail_closed" if spec.fail_closed else ""
            output.console.print(f"    {mark} {spec.command}")
            output.console.print(
                f"      [dim]{state} · timeout {spec.timeout}s{matcher}{closed}[/dim]"
            )
            entry = shell_hooks.entry_for(spec.event, spec.command) if allowed else None
            if entry and entry.get("approved_at"):
                output.console.print(
                    f"      [dim]approved {entry['approved_at']}[/dim]"
                )
                if _drifted(spec.command, entry):
                    output.console.print(
                        "      [warn]the script changed after it was approved — "
                        "andromeda hooks doctor[/warn]"
                    )
        output.console.print()

    return 0


def _drifted(command: str, entry: dict[str, Any]) -> bool:
    """Whether the file moved on since a person read it.

    Approval is for the script that was reviewed, not for the path it sits at.
    A comparison of ISO-8601 strings is a comparison of instants here, because
    both are written in UTC with the same suffix.
    """
    at_approval = entry.get("script_mtime_at_approval")
    now = shell_hooks.script_mtime_iso(command)
    return bool(at_approval and now and now > at_approval)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test(event: str, for_tool: str = "", payload_file: str = "") -> int:
    if event not in hooks_module.VALID_HOOKS:
        output.fail(
            f"No hook event named {event!r}.",
            "andromeda hooks test <event> — see `andromeda hooks --help`",
        )
        return 2

    payload = dict(PAYLOADS.get(event, {"session_id": "test-session"}))
    if for_tool:
        payload["tool_name"] = for_tool

    if payload_file:
        try:
            custom = json.loads(Path(payload_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            output.fail(f"Could not read {payload_file}: {exc}")
            return 2
        if not isinstance(custom, dict):
            output.fail(f"{payload_file} must hold a JSON object.")
            return 2
        payload.update(custom)

    specs = [spec for spec in _specs() if spec.event == event]
    if for_tool:
        specs = [
            spec
            for spec in specs
            if spec.event not in hooks_module.TOOL_SCOPED_EVENTS
            or spec.matches_tool(for_tool)
        ]

    if not specs:
        detail = f" matching --for-tool {for_tool}" if for_tool else ""
        output.info(f"No hooks configured for {event}{detail}.")
        return 0

    plural = "" if len(specs) == 1 else "s"
    output.info(f"  firing {len(specs)} hook{plural} for {event}\n")

    failures = 0
    for spec in specs:
        output.console.print(f"  [cyan]{spec.command}[/cyan]")
        result = shell_hooks.run_once(spec, payload)
        if not _print_result(result):
            failures += 1
        output.console.print()

    return 1 if failures else 0


def _print_result(result: dict[str, Any]) -> bool:
    """Print one run. Returns whether it went cleanly."""
    if result.get("error"):
        output.console.print(f"    [red]✗[/red] {result['error']}")
        return False
    if result.get("timed_out"):
        output.console.print(
            f"    [red]✗[/red] timed out after {result['elapsed_seconds']}s"
        )
        return False

    output.console.print(
        f"    [dim]exit {result.get('returncode')} · "
        f"{result.get('elapsed_seconds', 0)}s[/dim]"
    )
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    # A `Text` rather than a markup string: this is the script's own output,
    # and a hook that prints `[dim]` or a bracketed path would otherwise have
    # it parsed as styling and shown wrong — in the one command whose job is
    # to show exactly what the script said.
    if stdout:
        output.console.print(
            Text(f"    stdout {_truncate(stdout, 400)}", style="dim")
        )
    if stderr:
        output.console.print(
            Text(f"    stderr {_truncate(stderr, 400)}", style="dim")
        )

    parsed = result.get("parsed")
    if parsed:
        output.console.print(f"    [green]→[/green] {json.dumps(parsed)}")
    else:
        # Said plainly, because "it ran and printed something" and "it changed
        # what the agent does" are different outcomes and look identical
        # otherwise.
        output.console.print("    [dim]→ nothing passed to the agent[/dim]")
    return result.get("returncode") == 0


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def revoke(command: str) -> int:
    removed = shell_hooks.revoke(command)
    if not removed:
        output.info(f"No approval on record for {command}")
        return 0
    plural = "" if removed == 1 else "s"
    output.ok(f"Withdrew {removed} approval{plural} for {command}")
    output.info("  Sessions already running keep it until they restart.")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def doctor() -> int:
    specs = _specs()
    if not specs:
        output.info("No hooks configured — nothing to check.")
        return 0

    plural = "" if len(specs) == 1 else "s"
    output.info(f"  checking {len(specs)} hook{plural}\n")

    problems = 0
    for spec in specs:
        output.console.print(f"  [cyan]{spec.event}[/cyan] [dim]{spec.command}[/dim]")
        problems += _check(spec)
        output.console.print()

    if problems:
        thing = "issue" if problems == 1 else "issues"
        output.fail(f"{problems} {thing} — these hooks are not doing what they say.")
        return 1
    output.ok("Every configured hook is runnable and approved.")
    return 0


def _check(spec: shell_hooks.ShellHookSpec) -> int:
    problems = 0

    runnable = shell_hooks.script_is_executable(spec.command)
    if runnable:
        output.console.print("    [green]✓[/green] [dim]the script exists and runs[/dim]")
    else:
        problems += 1
        output.console.print(
            "    [red]✗[/red] [dim]missing, or not executable — check the path, "
            "or chmod +x it[/dim]"
        )

    entry = shell_hooks.entry_for(spec.event, spec.command)
    if entry:
        output.console.print(
            f"    [green]✓[/green] [dim]approved {entry.get('approved_at', '?')}[/dim]"
        )
    else:
        problems += 1
        output.console.print(
            "    [red]✗[/red] [dim]not approved, so it will not fire — answer the "
            "prompt next start, or start once with --accept-hooks[/dim]"
        )

    if entry and _drifted(spec.command, entry):
        problems += 1
        output.console.print(
            f"    [warn]![/warn] [dim]the script changed after approval "
            f"(was {entry.get('script_mtime_at_approval')}, now "
            f"{shell_hooks.script_mtime_iso(spec.command)}) — read it, then "
            f"revoke and re-approve[/dim]"
        )
    elif entry:
        output.console.print(
            "    [green]✓[/green] [dim]unchanged since it was approved[/dim]"
        )

    if not entry:
        # Deliberately not run. `doctor` is what you run on a config you have
        # just pulled, to see what is about to register — running those scripts
        # to tell you about them defeats the whole exercise.
        output.console.print(
            "    [dim]· not run: a hook is only exercised here once approved[/dim]"
        )
        return problems

    if not runnable:
        return problems

    result = shell_hooks.run_once(spec, PAYLOADS.get(spec.event, {}))
    if result.get("timed_out"):
        problems += 1
        output.console.print(
            f"    [red]✗[/red] [dim]timed out after {result['elapsed_seconds']}s "
            f"on a synthetic payload (timeout {spec.timeout}s)[/dim]"
        )
        return problems
    if result.get("error"):
        problems += 1
        output.console.print(f"    [red]✗[/red] [dim]{result['error']}[/dim]")
        return problems

    stdout = (result.get("stdout") or "").strip()
    if not stdout:
        output.console.print(
            f"    [green]✓[/green] [dim]ran clean, said nothing "
            f"(exit {result.get('returncode')}) — an observer[/dim]"
        )
        return problems

    try:
        json.loads(stdout)
    except json.JSONDecodeError:
        problems += 1
        output.console.print(
            f"    [red]✗[/red] [dim]stdout was not JSON, so the agent ignores it: "
            f"{_truncate(stdout, 120)}[/dim]"
        )
        return problems

    output.console.print(
        f"    [green]✓[/green] [dim]returned valid JSON "
        f"(exit {result.get('returncode')}, {result.get('elapsed_seconds')}s)[/dim]"
    )
    if result.get("parsed") is None:
        # Valid JSON that means nothing here is the failure people spend an
        # afternoon on: the script looks healthy from every angle except the
        # one that matters.
        output.console.print(
            "    [warn]![/warn] [dim]…but nothing in it is a directive this "
            "event understands — see `andromeda hooks --help`[/dim]"
        )
        problems += 1

    return problems
