"""What a job remembers between wake-ups.

A scheduled job starts from nothing every time it fires — that is deliberate,
and it is what makes a job reproducible. But a job that *polls* needs one thing
carried forward: the cursor. "Everything since the last issue I saw", "the
watermark I stopped at", "the three hosts I am still watching". Without it,
every run either re-reports what it already reported or has to re-derive the
boundary from scratch.

So each job gets a small key/value scratchpad, injected into its prompt on
every run and written through a tool bound to that job.

**Two design choices worth stating.**

The obvious shape is to store this in SQLite and give the agent no tool for it,
leaving a running job to shell out to `andromeda cron notepad ... set`. That
cannot work here: a job created with the default `ask` mode is *narrowed to read-only*
precisely because nobody is watching it, so it has no shell to write with. A
job that can be trusted to remember its own cursor but not to run arbitrary
commands is the common case, not the exception. So the notepad is a tool, tiered
`safe_local` / category `write` — exactly the reasoning `memory_store` records.

And it is JSON, not SQLite. The scheduler's whole design note is that state you
cannot read with `cat` is state you cannot debug at 3am.

The caps matter because this text is prepended to every prompt the job ever
sends, so unbounded growth is a bill that compounds silently.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_VALUE_BYTES = 16 * 1024
MAX_JOB_BYTES = 64 * 1024
MAX_KEYS = 64


class NotepadError(ValueError):
    pass


class Notepad:
    """Every job's scratchpad, in one file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._pages: dict[str, dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt notepad is an empty notepad, not a crashed job. The
            # job's own prompt says what to do with no cursor: start over.
            return
        if not isinstance(raw, dict):
            return
        for job_id, page in raw.items():
            if isinstance(page, dict):
                self._pages[str(job_id)] = {
                    str(k): str(v) for k, v in page.items() if v is not None
                }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self._pages, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(self.path)

    # ---- reading ----------------------------------------------------------

    def page(self, job_id: str) -> dict[str, str]:
        return dict(self._pages.get(job_id, {}))

    def get(self, job_id: str, key: str) -> str:
        return self._pages.get(job_id, {}).get(key, "")

    def render(self, job_id: str) -> str:
        """The block that goes into the job's prompt, or nothing."""
        page = self._pages.get(job_id) or {}
        if not page:
            return ""
        lines = "\n".join(f"  {key}: {value}" for key, value in sorted(page.items()))
        return (
            "Your notepad from previous runs of this job — this is the only "
            "thing that carries over, so update it before you finish:\n" + lines
        )

    # ---- writing ----------------------------------------------------------

    def set(self, job_id: str, key: str, value: str) -> str:
        key = (key or "").strip()
        if not key:
            raise NotepadError("A key is required.")
        value = "" if value is None else str(value)

        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise NotepadError(
                f"That value is over the {MAX_VALUE_BYTES // 1024}KB limit for one "
                "note. The notepad is a cursor, not a cache — store the marker, "
                "not the data it points at."
            )

        page = dict(self._pages.get(job_id, {}))
        page[key] = value
        if len(page) > MAX_KEYS:
            raise NotepadError(f"A job may keep at most {MAX_KEYS} notes.")

        total = sum(len(k.encode()) + len(v.encode()) for k, v in page.items())
        if total > MAX_JOB_BYTES:
            # Refused rather than evicted. Silently dropping the oldest key
            # would lose exactly the cursor the job is depending on, and it
            # would lose it quietly.
            raise NotepadError(
                f"This job's notepad would exceed {MAX_JOB_BYTES // 1024}KB. "
                "Remove a note first — every note here is prepended to every "
                "run's prompt."
            )

        self._pages[job_id] = page
        self.save()
        return value

    def forget(self, job_id: str, key: str) -> bool:
        page = self._pages.get(job_id)
        if not page or key not in page:
            return False
        del page[key]
        if not page:
            del self._pages[job_id]
        self.save()
        return True

    def clear(self, job_id: str) -> int:
        page = self._pages.pop(job_id, {})
        if page:
            self.save()
        return len(page)
