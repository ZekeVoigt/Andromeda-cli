"""Approvals that accumulate.

After the twentieth `git status` prompt, a gate has stopped being a safety
feature and started being a reflex the user clicks through — which is worse than
no gate, because it trains the habit of approving without reading.

Mirrors the TypeScript `AllowlistEntry`: a trust level per tool, the counts
behind it, and the rule that makes the whole thing safe —

  **An entry is bound to the tier it was learned at.** Trust `terminal` while
  it is `destructive` and it stays trusted at `destructive`. If a tool's tier
  ever rises, its entry no longer applies and the gate is back. Without this,
  a permission granted for one thing silently covers a more dangerous version
  of it later.

Two deliberate deviations from the hosted implementation, both stated:

  - A learned entry never widens itself. The hosted side can promote on
    accumulated approvals; here promotion is always an explicit answer at the
    prompt. Counts only drive a *suggestion*.
  - An explicit config override beats a learned entry. The hosted order is the
    other way round. An override is a person's deliberate statement and an
    entry is an accumulation; when they disagree, the deliberate one wins.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TrustLevel = Literal["always_allow", "always_deny"]

# How many plain approvals before offering to stop asking. High enough that it
# is a settled habit, low enough to arrive before irritation does.
SUGGEST_AFTER = 5


@dataclass
class AllowlistEntry:
    tool_name: str
    trust_level: TrustLevel
    risk_tier: str
    approval_count: int = 0
    denial_count: int = 0
    last_approved_at: float = 0.0
    last_denied_at: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "toolName": self.tool_name,
            "trustLevel": self.trust_level,
            "riskTier": self.risk_tier,
            "approvalCount": self.approval_count,
            "denialCount": self.denial_count,
            "lastApprovedAt": self.last_approved_at,
            "lastDeniedAt": self.last_denied_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "AllowlistEntry | None":
        name = str(raw.get("toolName") or "").strip()
        trust = raw.get("trustLevel")
        if not name or trust not in ("always_allow", "always_deny"):
            return None
        return cls(
            tool_name=name,
            trust_level=trust,
            risk_tier=str(raw.get("riskTier") or ""),
            approval_count=int(raw.get("approvalCount") or 0),
            denial_count=int(raw.get("denialCount") or 0),
            last_approved_at=float(raw.get("lastApprovedAt") or 0),
            last_denied_at=float(raw.get("lastDeniedAt") or 0),
            updated_at=float(raw.get("updatedAt") or 0),
        )


class Allowlist:
    """Learned trust, persisted next to the rest of the CLI's state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, AllowlistEntry] = {}
        self._counts: dict[str, int] = {}
        self.load()

    # ---- persistence ------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt allowlist reads as no learned trust. Failing closed is
            # the only safe direction for a file that grants permissions.
            return
        for item in raw.get("entries", []) if isinstance(raw, dict) else []:
            entry = AllowlistEntry.from_json(item) if isinstance(item, dict) else None
            if entry is not None:
                self._entries[entry.tool_name] = entry
        counts = raw.get("counts") if isinstance(raw, dict) else None
        if isinstance(counts, dict):
            self._counts = {str(k): int(v) for k, v in counts.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "entries": [entry.to_json() for entry in self._entries.values()],
                "counts": self._counts,
            },
            indent=2,
        )
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        temporary.replace(self.path)

    # ---- reading ----------------------------------------------------------

    def entry_for(self, tool_name: str, risk_tier: str) -> AllowlistEntry | None:
        """The entry that applies, if any.

        Returns nothing when the tool's tier has moved since the entry was
        written: the trust was granted for a specific level of danger.
        """
        entry = self._entries.get(tool_name)
        if entry is None:
            return None
        if entry.risk_tier and entry.risk_tier != risk_tier:
            return None
        return entry

    def all(self) -> list[AllowlistEntry]:
        return sorted(self._entries.values(), key=lambda entry: entry.tool_name)

    def approvals_of(self, tool_name: str) -> int:
        return self._counts.get(tool_name, 0)

    def should_suggest(self, tool_name: str) -> bool:
        return (
            tool_name not in self._entries
            and self._counts.get(tool_name, 0) >= SUGGEST_AFTER
        )

    # ---- writing ----------------------------------------------------------

    def record(self, tool_name: str, risk_tier: str, approved: bool) -> None:
        """Note one answer at the prompt. Never changes a trust level."""
        if approved:
            self._counts[tool_name] = self._counts.get(tool_name, 0) + 1
        else:
            # A denial resets the run. Four approvals then a refusal is not a
            # settled habit, and offering to stop asking right after someone
            # said no is exactly backwards.
            self._counts[tool_name] = 0
        entry = self._entries.get(tool_name)
        if entry is not None:
            if approved:
                entry.approval_count += 1
                entry.last_approved_at = time.time()
            else:
                entry.denial_count += 1
                entry.last_denied_at = time.time()
            entry.updated_at = time.time()
        self.save()

    def trust(self, tool_name: str, risk_tier: str, level: TrustLevel) -> AllowlistEntry:
        entry = AllowlistEntry(
            tool_name=tool_name,
            trust_level=level,
            risk_tier=risk_tier,
            approval_count=self._counts.get(tool_name, 0),
            last_approved_at=time.time() if level == "always_allow" else 0.0,
            last_denied_at=time.time() if level == "always_deny" else 0.0,
        )
        self._entries[tool_name] = entry
        self.save()
        return entry

    def forget(self, tool_name: str) -> bool:
        removed = self._entries.pop(tool_name, None) is not None
        self._counts.pop(tool_name, None)
        if removed:
            self.save()
        return removed

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._counts.clear()
        self.save()
        return count
