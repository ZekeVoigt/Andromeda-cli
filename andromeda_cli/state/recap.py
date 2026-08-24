"""What happened while you were looking somewhere else.

Computed from the transcript, never from a model call. A recap you have to
wait for and pay for is a recap nobody runs — and it would also invalidate the
prompt cache the next real turn is about to use, which makes re-orienting cost
more than the thing you were re-orienting to do.

It answers four questions, in the order somebody coming back to a terminal
asks them: what did I ask, what did it say, what did it touch, and is anything
still open.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# How much of the last exchange to quote. Long enough to recognise, short
# enough that a recap never scrolls.
PROMPT_PREVIEW = 160
ANSWER_PREVIEW = 240
MAX_FILES = 6
MAX_TOOLS = 6

# Tool names whose arguments name a file, and the argument that holds it.
# Anything not listed still counts as activity; it just does not contribute a
# filename, which is better than guessing one out of an arbitrary string.
FILE_ARGUMENTS: dict[str, str] = {
    "read_file": "path",
    "write_file": "path",
    "patch": "path",
    "list_dir": "path",
    "search_files": "path",
}


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


def _trim(text: str, width: int) -> str:
    flattened = " ".join(_text(text).split())
    return flattened[:width] + ("…" if len(flattened) > width else "")


@dataclass
class Recap:
    turns: int = 0
    tool_calls: int = 0
    tools: list[tuple[str, int]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    last_prompt: str = ""
    last_answer: str = ""
    open_todos: list[str] = field(default_factory=list)
    errors: int = 0

    @property
    def empty(self) -> bool:
        return self.turns == 0 and self.tool_calls == 0

    def lines(self) -> list[str]:
        """The recap as text, one point per line, in reading order."""
        out: list[str] = []
        if self.empty:
            return ["Nothing has happened in this session yet."]
        out.append(
            f"{self.turns} turn{'' if self.turns == 1 else 's'}"
            f" · {self.tool_calls} tool call{'' if self.tool_calls == 1 else 's'}"
            + (f" · {self.errors} failed" if self.errors else "")
        )
        if self.tools:
            out.append(
                "used: "
                + ", ".join(f"{name}×{count}" for name, count in self.tools)
            )
        if self.files:
            out.append("touched: " + ", ".join(self.files))
        if self.last_prompt:
            out.append(f"you asked: {self.last_prompt}")
        if self.last_answer:
            out.append(f"it said: {self.last_answer}")
        if self.open_todos:
            out.append("still open: " + "; ".join(self.open_todos))
        return out


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _call_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(call.get("name") or "")


def _call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    import json

    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else call.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build(messages: list[dict[str, Any]], todos: Any = None) -> Recap:
    recap = Recap()
    used: Counter[str] = Counter()
    files: list[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            recap.turns += 1
            recap.last_prompt = _trim(message.get("content"), PROMPT_PREVIEW)
        elif role == "assistant":
            text = _text(message.get("content")).strip()
            if text:
                recap.last_answer = _trim(text, ANSWER_PREVIEW)
            for call in _tool_calls(message):
                name = _call_name(call)
                if not name:
                    continue
                recap.tool_calls += 1
                used[name] += 1
                argument = FILE_ARGUMENTS.get(name)
                if argument:
                    value = _call_arguments(call).get(argument)
                    if isinstance(value, str) and value.strip() and value not in files:
                        files.append(value.strip())
        elif role == "tool":
            body = _text(message.get("content"))
            # The tool layer prefixes a failure this way; counting them is the
            # difference between "it did twelve things" and "it tried twelve
            # things". No parsing beyond the prefix — a result that merely
            # mentions an error is not a failed call.
            if body.lstrip().lower().startswith(("error:", "failed:")):
                recap.errors += 1

    recap.tools = used.most_common(MAX_TOOLS)
    recap.files = files[-MAX_FILES:]

    if todos is not None:
        try:
            recap.open_todos = [
                str(item.get("task") or "")
                for item in getattr(todos, "items", [])
                if str(item.get("status") or "") in {"pending", "in_progress"}
            ][:MAX_FILES]
        except Exception:  # noqa: BLE001 — a recap must never be the thing that fails
            recap.open_todos = []

    return recap
