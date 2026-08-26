"""Keeping the skill library honest.

Two passes, and the split between them is the design.

**The sweep** is arithmetic. It compares each agent-written skill's last use
against two thresholds and moves it between active, stale and archived. No
model, no cost, and every move is reversible — archiving relocates a directory
and writes down where it came from. It runs on its own, because a decision a
person could check by looking at a date is not one worth interrupting them for.

**The review** reads the skills and proposes changes to their *content*:
two skills that should be one, instructions that describe a tool that no
longer exists, a skill that never fires because its description does not say
when to use it. That costs a model call and it edits work somebody may care
about, so it does not apply anything. It writes proposals, and a person
accepts them.

That division is the standing rule in this program, applied here: an agent may
propose, only a person grants. A curator that quietly rewrites a skill library
is a program you cannot leave running.

Four invariants:

  * only skills the agent wrote, in your own skills directory;
  * pinned is never touched, whatever the dates say;
  * nothing is deleted, ever — archive is a move;
  * a skill that has never been used is not stale. It may just be waiting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from andromeda_tools import skill_usage

STATE_FILENAME = "curator-state.json"
PROPOSALS_FILENAME = "curator-proposals.json"

# A week between sweeps. The thing being measured moves in weeks, and a daily
# pass would spend attention on a number that has not changed.
DEFAULT_INTERVAL_DAYS = 7
# Untouched for a month: listed, but say so.
DEFAULT_STALE_AFTER_DAYS = 30
# Untouched for a quarter: out of the way.
DEFAULT_ARCHIVE_AFTER_DAYS = 90


@dataclass
class Settings:
    enabled: bool = True
    interval_days: int = DEFAULT_INTERVAL_DAYS
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "Settings":
        config = config or {}

        def whole(key: str, fallback: int) -> int:
            try:
                value = int(config.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value > 0 else fallback

        stale = whole("curator_stale_days", DEFAULT_STALE_AFTER_DAYS)
        archive = whole("curator_archive_days", DEFAULT_ARCHIVE_AFTER_DAYS)
        if archive <= stale:
            # Archiving sooner than a skill can even be called stale would skip
            # the warning state entirely. The setting is taken as a mistake and
            # the two are separated rather than refused — a bad number here
            # must not stop the sweep running at all.
            archive = stale * 3

        return cls(
            enabled=bool(config.get("curator", True)),
            interval_days=whole("curator_interval_days", DEFAULT_INTERVAL_DAYS),
            stale_after_days=stale,
            archive_after_days=archive,
        )


@dataclass
class Sweep:
    """What one arithmetic pass did."""

    checked: int = 0
    seeded: int = 0
    marked_stale: int = 0
    archived: int = 0
    revived: int = 0
    skipped_pinned: int = 0
    moved: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.marked_stale + self.archived + self.revived

    def summary(self) -> str:
        if not self.checked:
            return "nothing to curate"
        if not self.changed and not self.seeded:
            return f"{self.checked} skill(s) checked, nothing to do"
        parts = []
        if self.seeded:
            parts.append(f"{self.seeded} started tracking")
        if self.marked_stale:
            parts.append(f"{self.marked_stale} marked stale")
        if self.archived:
            parts.append(f"{self.archived} archived")
        if self.revived:
            parts.append(f"{self.revived} back in use")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# When it runs
# ---------------------------------------------------------------------------


def state_path(home: Path) -> Path:
    return Path(home) / STATE_FILENAME


def load_state(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(home: Path, data: dict[str, Any]) -> None:
    try:
        path = state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def set_paused(home: Path, paused: bool) -> None:
    state = load_state(home)
    state["paused"] = bool(paused)
    save_state(home, state)


def is_paused(home: Path) -> bool:
    return bool(load_state(home).get("paused"))


def due(home: Path, settings: Settings, now: datetime | None = None) -> bool:
    """Whether a sweep should happen now.

    **The first sight of an install never sweeps.** It stamps the clock and
    waits a full interval. A library that predates this feature has no usage
    history at all, so an immediate pass would read every skill as untouched
    since the epoch and archive the lot — on the first run after an update,
    which is precisely when nobody is watching for it.
    """
    if not settings.enabled or is_paused(home):
        return False

    now = now or datetime.now(tz=timezone.utc)
    state = load_state(home)
    last = skill_usage.parse_iso(state.get("last_run_at"))

    if last is None:
        state["last_run_at"] = now.isoformat().replace("+00:00", "Z")
        state["last_summary"] = "first seen — the first sweep is one interval away"
        save_state(home, state)
        return False

    return (now - last).days >= settings.interval_days


def record_run(home: Path, sweep: Sweep, now: datetime | None = None) -> None:
    now = now or datetime.now(tz=timezone.utc)
    state = load_state(home)
    state["last_run_at"] = now.isoformat().replace("+00:00", "Z")
    state["last_summary"] = sweep.summary()
    save_state(home, state)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def sweep(
    skills: dict[str, Any],
    home: Path,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> Sweep:
    """Move each curatable skill to the state its dates say it is in."""
    result = Sweep()

    for row in skill_usage.report(skills, home):
        result.checked += 1
        name = row["name"]

        if row["pinned"]:
            result.skipped_pinned += 1
            continue

        if not row["recorded"]:
            # First sight. Anchor the clock to now and decide next time — a
            # skill that predates the record is not evidence of neglect.
            if not dry_run:
                skill_usage.seed(home, name, created_by=row["created_by"])
            result.seeded += 1
            continue

        idle = row["idle_days"]
        state = row["state"]

        if state == skill_usage.ARCHIVED:
            continue

        # Never used and still young: absence of evidence. Its trigger may
        # simply not have come up yet.
        if row["uses"] == 0 and idle < settings.stale_after_days:
            if state == skill_usage.STALE and not dry_run:
                skill_usage.set_state(home, name, skill_usage.ACTIVE)
                result.revived += 1
            continue

        if idle >= settings.archive_after_days:
            if not dry_run:
                ok, where = skill_usage.archive(home, name, Path(row["path"]))
                if not ok:
                    continue
                result.moved.append(f"{name} → {where}")
            else:
                result.moved.append(f"{name} → would be archived")
            result.archived += 1
        elif idle >= settings.stale_after_days and state == skill_usage.ACTIVE:
            if not dry_run:
                skill_usage.set_state(home, name, skill_usage.STALE)
            result.marked_stale += 1
        elif idle < settings.stale_after_days and state == skill_usage.STALE:
            if not dry_run:
                skill_usage.set_state(home, name, skill_usage.ACTIVE)
            result.revived += 1

    if not dry_run:
        record_run(home, result)
    return result


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------

REVIEW_INSTRUCTION = """You are reviewing a library of skills — files of \
instructions this agent wrote for itself, so that a job done once can be done \
the same way again.

Read what follows and answer with proposals. Do not describe the library back \
to me; every line you write should be something to do or nothing at all.

Look for exactly four things:

1. **Duplicates.** Two skills that would be loaded for the same request. Say \
which one to keep and what to take from the other.
2. **A description that does not say when to use it.** The description is the \
only thing in the prompt; a skill described as "helper utilities" is never \
loaded because nothing tells the agent to load it. Propose the replacement \
line, written as a trigger: "when the user asks to ...".
3. **Instructions that have gone stale.** A skill naming a command, a file or \
a tool that is not there any more.
4. **Skills that should be one.** Three skills that are three steps of one job.

Answer as JSON and nothing else:

{"proposals": [
  {"skill": "<name>", "kind": "merge|describe|update|split|drop",
   "what": "<one sentence: the change>",
   "why": "<one sentence: the evidence in what you read>"}
]}

An empty list is a complete answer, and the right one when the library is \
fine. Do not invent work."""


@dataclass
class Proposal:
    skill: str
    kind: str
    what: str
    why: str

    def as_dict(self) -> dict[str, str]:
        return {"skill": self.skill, "kind": self.kind, "what": self.what, "why": self.why}


def review_context(skills: dict[str, Any], home: Path, limit: int = 40) -> str:
    """What the reviewing model is shown.

    Bodies are included but clipped: the review is about whether a skill earns
    its place and says when to use itself, and neither question needs the last
    two thousand words of a long one.
    """
    rows = {row["name"]: row for row in skill_usage.report(skills, home)}
    if not rows:
        return ""

    blocks: list[str] = []
    for name in sorted(rows)[:limit]:
        skill = skills[name]
        row = rows[name]
        used = (
            f"used {row['uses']} time(s), last {row['last_used_at'] or 'never'}"
            if row["uses"]
            else "never used"
        )
        body = (skill.body or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "\n… (clipped)"
        blocks.append(
            f"## {name}\n"
            f"description: {skill.description}\n"
            f"state: {row['state']} · {used} · idle {row['idle_days']:.0f} day(s)\n"
            f"---\n{body}"
        )
    return "\n\n".join(blocks)


def parse_proposals(text: str) -> list[Proposal]:
    """Read the model's answer, forgiving the wrappers it puts around JSON."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []

    raw = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    proposals: list[Proposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "").strip()
        if not what:
            # A proposal with no action is a sentence about the library, which
            # is the one thing the instruction asked for none of.
            continue
        proposals.append(
            Proposal(
                skill=str(item.get("skill") or "").strip(),
                kind=str(item.get("kind") or "update").strip(),
                what=what,
                why=str(item.get("why") or "").strip(),
            )
        )
    return proposals


def review(
    skills: dict[str, Any],
    home: Path,
    ask: Callable[[str], str],
    *,
    limit: int = 40,
) -> list[Proposal]:
    """Ask a model what it would change, and write down the answer.

    `ask` takes a prompt and returns text; the caller supplies the model, so
    this module never learns what a provider is. Nothing here edits a skill —
    the proposals are read by a person.
    """
    context = review_context(skills, home, limit)
    if not context:
        return []

    try:
        answer = ask(f"{REVIEW_INSTRUCTION}\n\n---\n\n{context}")
    except Exception:  # noqa: BLE001 - a failed review is not a failed session
        return []

    proposals = parse_proposals(answer)
    save_proposals(home, proposals)
    return proposals


def proposals_path(home: Path) -> Path:
    return Path(home) / PROPOSALS_FILENAME


def save_proposals(home: Path, proposals: list[Proposal]) -> None:
    payload = {
        "written_at": skill_usage.now_iso(),
        "proposals": [item.as_dict() for item in proposals],
    }
    try:
        path = proposals_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_proposals(home: Path) -> tuple[str, list[Proposal]]:
    try:
        data = json.loads(proposals_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", []
    if not isinstance(data, dict):
        return "", []
    raw = data.get("proposals")
    if not isinstance(raw, list):
        return "", []
    return str(data.get("written_at", "")), [
        Proposal(
            skill=str(item.get("skill", "")),
            kind=str(item.get("kind", "")),
            what=str(item.get("what", "")),
            why=str(item.get("why", "")),
        )
        for item in raw
        if isinstance(item, dict)
    ]


def clear_proposals(home: Path) -> None:
    try:
        proposals_path(home).unlink()
    except OSError:
        pass
