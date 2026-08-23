"""The agent's own task list.

Session-scoped and deliberately not persisted: a plan is about the turn it was
made in, and a stale list read back on a later run is worse than no list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .spec import ToolResult, failure

STATUSES = ("pending", "in_progress", "done")
MARKS = {"pending": "○", "in_progress": "◐", "done": "●"}


@dataclass
class TodoList:
    items: list[dict[str, str]] = field(default_factory=list)

    def replace(self, items: list[dict[str, Any]]) -> ToolResult:
        cleaned: list[dict[str, str]] = []
        for raw in items:
            if not isinstance(raw, dict):
                return failure("Each todo must be an object with `task` and `status`.")
            task = str(raw.get("task") or "").strip()
            if not task:
                return failure("Every todo needs a non-empty `task`.")
            status = str(raw.get("status") or "pending").strip()
            if status not in STATUSES:
                return failure(f"status must be one of {', '.join(STATUSES)} — got {status!r}.")
            cleaned.append({"task": task, "status": status})

        in_progress = [item for item in cleaned if item["status"] == "in_progress"]
        if len(in_progress) > 1:
            # More than one thing "in progress" is how a plan stops describing
            # what is actually happening.
            return failure("Only one todo may be in_progress at a time.")

        self.items = cleaned
        return ToolResult(content=self.render(), display=self.summary())

    def render(self) -> str:
        if not self.items:
            return "(no todos)"
        return "\n".join(
            f"{MARKS[item['status']]} {item['task']}" for item in self.items
        )

    def summary(self) -> str:
        if not self.items:
            return "todo list cleared"
        done = sum(1 for item in self.items if item["status"] == "done")
        return f"{done}/{len(self.items)} done"
