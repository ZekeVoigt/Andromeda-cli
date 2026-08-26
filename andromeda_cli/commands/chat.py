"""One-shot, non-interactive turns.

`andromeda "..."` and `... | andromeda` exist so the harness is scriptable:
output on stdout, diagnostics on stderr, and an exit code that means something.

There is nobody at the keyboard here, so the policy is narrowed to what can run
without a person — see `session.build_policy`. `--approval auto` is the explicit
way to hand a script more than that.
"""

from __future__ import annotations

import sys
from typing import Any

from andromeda_agent import AgentError, Callbacks, OutOfCredit, build_provider
from andromeda_tools import ToolResult, ToolSpec

from .. import output, render
from ..session import build_conversation, ended as session_ended, set_lane_announcer

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_OUT_OF_CREDIT = 3
EXIT_INTERRUPTED = 130


def one_shot(prompt: str, config: dict[str, Any], workspace_root: str | None = None) -> int:
    try:
        provider = build_provider(config)
    except AgentError as exc:
        output.agent_error(exc)
        return EXIT_ERROR

    set_lane_announcer(
        lambda specialist, label: output.err_console.print(
            f"[dim]⇢ {specialist} lane · {label}[/dim]"
        )
    )

    conversation, _record = build_conversation(
        config, provider, interactive=False, workspace_root=workspace_root
    )

    completed = False
    try:
        # Rendered when a person is watching; raw text when redirected, so
        # `andromeda "..." > out.md` produces markdown rather than a
        # screenshot of markdown.
        with render.AnswerStream() as stream:
            conversation.send(prompt, _callbacks(stream))
        completed = True
    except OutOfCredit as exc:
        sys.stdout.flush()
        output.agent_error(exc)
        return EXIT_OUT_OF_CREDIT
    except AgentError as exc:
        sys.stdout.flush()
        output.agent_error(exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stdout.flush()
        return EXIT_INTERRUPTED
    finally:
        # In `finally`, so an interrupted or failed one-shot still reports its
        # ending — with `completed` saying which it was.
        session_ended(conversation, completed=completed)

    if not render.rendering_enabled():
        sys.stdout.write("\n")
        sys.stdout.flush()
    return EXIT_OK


def _callbacks(stream: "render.AnswerStream") -> Callbacks:
    # Tool activity goes to stderr so `andromeda "..." > out.txt` captures the
    # answer and nothing else, while a person watching the terminal still sees
    # what ran. `ask_approval` is left unset: no prompt is possible, and the
    # loop refuses rather than auto-approving.
    return Callbacks(
        on_text=stream.feed,
        on_tool_start=lambda spec, arguments: output.err_console.print(
            f"[dim]⚙ {spec.summary(arguments)}[/dim]"
        ),
        on_tool_result=_tool_result,
        on_tool_denied=lambda spec, reason: output.err_console.print(
            f"[dim]✗ {spec.name}: {reason}[/dim]"
        ),
        # Also stderr: a pipe that waited thirty seconds should say why in the
        # place a person is watching, without putting it in the captured answer.
        on_retry=lambda reason: output.err_console.print(f"[dim]… {reason}[/dim]"),
    )


def _tool_result(spec: ToolSpec, result: ToolResult) -> None:
    mark = "✓" if result.ok else "!"
    first = result.display.splitlines()[0] if result.display else ""
    output.err_console.print(f"[dim]{mark} {first[:120]}[/dim]")


