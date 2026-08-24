"""The one check worth doing before a session starts.

A stale or unopenable index fails *silently*: `session_search` returns nothing,
which reads exactly like "that was never discussed". Of everything `sessions
doctor` reports, that is the one failure a person cannot notice, so it is the
one thing checked automatically.

**Cheap, and once a day.** Three things only — can the index be opened, is it
behind the transcripts, and is there unreviewed salvage sitting in quarantine.
Deliberately *not* the whole of `sessions doctor`: parsing every transcript and
running `PRAGMA integrity_check` are both O(everything), and a startup path
that gets slower the longer you have used the program is a startup path people
work around.

**Silent when healthy.** A check that prints on every launch is a check nobody
reads, and it trains people to ignore the one time it says something.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config as config_module
from . import index as index_module
from . import recovery as recovery_module

MARKER = "state-check"
INTERVAL = 24 * 3600

# Above this, the reindex is reported rather than performed. A backlog this
# size only happens on a first run after an upgrade, and spending a minute on
# it before the first prompt appears is worse than saying so in one line.
REINDEX_BUDGET = 200


@dataclass
class Findings:
    reindexed: int = 0
    stale: int = 0
    quarantined: int = 0
    error: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.lines


def marker_path() -> Path:
    return config_module.home() / MARKER


def _due(version: str, now: float) -> bool:
    """Whether to check. A new version always checks.

    Version rather than time alone, because a schema addition arrives with an
    upgrade — and the run right after one is exactly when the index is behind.
    """
    try:
        raw = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    if not isinstance(raw, dict):
        return True
    if str(raw.get("version") or "") != version:
        return True
    return (now - float(raw.get("at") or 0)) >= INTERVAL


def _stamp(version: str, now: float) -> None:
    try:
        root = config_module.home()
        root.mkdir(parents=True, exist_ok=True)
        marker_path().write_text(
            json.dumps({"version": version, "at": now}), encoding="utf-8"
        )
    except OSError:
        # A home that cannot be written is a bigger problem than a missed
        # check, and it is not this function's to report.
        pass


def check(version: str, now: float | None = None, force: bool = False) -> Findings:
    """Run the cheap checks if they are due. Returns what to say, if anything."""
    findings = Findings()
    moment = time.time() if now is None else now
    if not force and not _due(version, moment):
        return findings

    # Stamped before the work, not after: a check that crashes must not run
    # again on every single launch from then on.
    _stamp(version, moment)

    try:
        stale = index_module.stale_count()
    except (sqlite3.Error, OSError) as exc:
        findings.error = str(exc)
        findings.lines.append(
            "The session index could not be opened — search will find nothing. "
            "Repair it with `andromeda sessions recover --rebuild-index`."
        )
        return findings

    findings.stale = stale
    if stale:
        if stale > REINDEX_BUDGET:
            findings.lines.append(
                f"{stale} sessions are not indexed yet, so searching them will "
                "miss. Run `andromeda sessions reindex` once."
            )
        else:
            counts = index_module.reindex()
            findings.reindexed = counts["rebuilt"] + counts["appended"]
            if counts["unreadable"]:
                findings.lines.append(
                    f"{counts['unreadable']} transcript(s) could not be read — "
                    "`andromeda sessions doctor` names them."
                )

    held = recovery_module.quarantine_dir()
    if held.is_dir():
        findings.quarantined = len(list(held.glob("*.json")))
        if findings.quarantined:
            findings.lines.append(
                f"{findings.quarantined} recovered transcript(s) are still in "
                f"{held} — delete them once you are happy with the salvage."
            )

    return findings


def announce(findings: Findings, say) -> None:
    """Print anything worth saying. `say` takes one line of text."""
    for line in findings.lines:
        say(line)
