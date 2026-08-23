"""Automations proposed, not created.

A *suggestion* is a ready-to-run job spec that Andromeda offers and a person
accepts or dismisses. It is the single surface every proposal flows through,
whatever produced it:

  `catalog`      a curated starter automation
  `blueprint`    a skill that ships a `blueprint:` block
  `usage`        something noticed you keep asking for by hand
  `integration`  a capability that became available

Three rules hold it together:

- **Accepting is always explicit.** Nothing here ever creates a job. Accepting
  calls `Schedule.add` with the stored spec, which is the same function every
  other path uses — there is no second job engine and no second schema.
- **A dismissal latches.** Keyed on a stable `dedup_key`, so the same proposal
  is never re-offered after somebody says no. A suggestion engine that forgets
  is a suggestion engine people turn off.
- **The backlog is capped.** Past `MAX_PENDING` new proposals are dropped
  rather than queued. A list of twenty things to decide about is a list nobody
  reads, and the twenty-first is not the important one.

And one rule that is Andromeda's, following §14's consent doctrine: a spec
stored here is **inert data**. It is validated only when accepted, by the same
`Schedule.add` that validates everything else, so a suggestion cannot smuggle
in an `auto` job by being written to disk by something other than a person.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCES = ("catalog", "blueprint", "usage", "integration")
PENDING, ACCEPTED, DISMISSED = "pending", "accepted", "dismissed"

MAX_PENDING = 5


class SuggestionError(ValueError):
    pass


@dataclass
class Suggestion:
    id: str
    title: str
    description: str
    source: str
    dedup_key: str
    spec: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    job_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "dedupKey": self.dedup_key,
            "spec": self.spec,
            "status": self.status,
            "createdAt": self.created_at,
            "jobId": self.job_id,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Suggestion | None":
        identifier = str(raw.get("id") or "").strip()
        dedup = str(raw.get("dedupKey") or "").strip()
        if not identifier or not dedup:
            return None
        status = str(raw.get("status") or PENDING)
        source = str(raw.get("source") or "")
        return cls(
            id=identifier,
            title=str(raw.get("title") or dedup),
            description=str(raw.get("description") or ""),
            # An unrecognised source reads as `usage` rather than being
            # dropped: the proposal is still a proposal, and losing it because
            # a future version added a category would silently shrink the list.
            source=source if source in SOURCES else "usage",
            dedup_key=dedup,
            spec=raw.get("spec") if isinstance(raw.get("spec"), dict) else {},
            status=status if status in (PENDING, ACCEPTED, DISMISSED) else PENDING,
            created_at=float(raw.get("createdAt") or 0),
            job_id=str(raw.get("jobId") or ""),
        )


class Suggestions:
    """Everything proposed on this machine, in one readable file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: list[Suggestion] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw.get("suggestions", []) if isinstance(raw, dict) else []:
            suggestion = Suggestion.from_json(item) if isinstance(item, dict) else None
            if suggestion is not None:
                self._items.append(suggestion)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"suggestions": [item.to_json() for item in self._items]}, indent=2
        )
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        temporary.replace(self.path)

    # ---- proposing --------------------------------------------------------

    def propose(
        self,
        *,
        title: str,
        description: str,
        source: str,
        spec: dict[str, Any],
        dedup_key: str,
    ) -> Suggestion | None:
        """Register a proposal. None when it was deliberately skipped.

        None rather than an exception, because every caller here is a seeder
        running in the background — "already offered and dismissed" is the
        normal case, not an error anybody needs to handle.
        """
        if source not in SOURCES:
            raise SuggestionError(f"unknown suggestion source {source!r}.")
        if not title.strip() or not dedup_key.strip():
            raise SuggestionError("a suggestion needs a title and a dedup key.")

        for existing in self._items:
            if existing.dedup_key == dedup_key:
                # Pending, accepted or dismissed — all three mean "do not offer
                # this again". Dismissed is the one that matters: re-offering
                # something somebody said no to is how a list stops being read.
                return None

        if sum(1 for item in self._items if item.status == PENDING) >= MAX_PENDING:
            return None

        suggestion = Suggestion(
            id=f"sug_{uuid.uuid4().hex[:8]}",
            title=title.strip(),
            description=description.strip(),
            source=source,
            dedup_key=dedup_key.strip(),
            spec=dict(spec),
        )
        self._items.append(suggestion)
        self.save()
        return suggestion

    # ---- reading ----------------------------------------------------------

    def all(self) -> list[Suggestion]:
        return list(self._items)

    def pending(self) -> list[Suggestion]:
        return [item for item in self._items if item.status == PENDING]

    def resolve(self, reference: str) -> Suggestion | None:
        """By id, by 1-based position in the pending list, or by exact title."""
        wanted = (reference or "").strip()
        if not wanted:
            return None
        for item in self._items:
            if item.id == wanted:
                return item
        if wanted.isdigit():
            pending = self.pending()
            index = int(wanted) - 1
            if 0 <= index < len(pending):
                return pending[index]
        for item in self._items:
            if item.title.lower() == wanted.lower():
                return item
        return None

    # ---- deciding ---------------------------------------------------------

    def accept(self, reference: str, schedule, workspace: str):
        """Create the job. Returns `(suggestion, job)`, or `(None, None)`.

        The spec goes straight into `Schedule.add`, which is what validates it
        — including the consent rules. A suggestion is a proposal, and it is
        only ever a person who turns one into a job.
        """
        suggestion = self.resolve(reference)
        if suggestion is None:
            return None, None

        spec = dict(suggestion.spec)
        spec.setdefault("workspace", workspace)
        spec.setdefault("origin", "user")
        schedule_expression = spec.pop("schedule", "")
        prompt = spec.pop("prompt", "")
        job = schedule.add(schedule_expression, prompt, spec.pop("workspace"), **spec)

        suggestion.status = ACCEPTED
        suggestion.job_id = job.id
        self.save()
        return suggestion, job

    def dismiss(self, reference: str) -> Suggestion | None:
        suggestion = self.resolve(reference)
        if suggestion is None:
            return None
        suggestion.status = DISMISSED
        self.save()
        return suggestion

    def clear_resolved(self) -> int:
        """Forget accepted ones; keep dismissals, because they are the latch."""
        before = len(self._items)
        self._items = [item for item in self._items if item.status != ACCEPTED]
        if len(self._items) != before:
            self.save()
        return before - len(self._items)
