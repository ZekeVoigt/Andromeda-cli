"""What each skill is actually used for, and whether it still is.

A skill library grows and never shrinks. The agent writes one for a job it did
once, the job never comes back, and a year later the manifest in every system
prompt lists forty skills of which six are ever loaded. The cost is paid on
every turn, and the noise makes the six harder to find.

This is the record that makes that visible: when a skill was created, when it
was last loaded, and how often. From that, three states:

    active     used, or new enough that not being used yet means nothing
    stale      untouched for a while — still listed, still loadable
    archived   moved aside. Not offered, not deleted, and restorable

Four rules hold the whole thing up:

**Only what the agent wrote is curated.** A skill you wrote by hand is yours;
this file does not get an opinion about it. Provenance is a marker the creator
leaves in the skill's own frontmatter, so it survives a copy and cannot be
inferred wrongly from a path.

**Nothing is ever deleted.** Archiving moves a directory and writes down where
it came from. The worst outcome of a wrong decision is `curator restore`.

**A pinned skill is never touched.** Not stale, not archived, whatever the
dates say.

**Never used is not the same as stale.** A skill created last week that has not
been loaded is absence of evidence — its trigger may simply not have come up.
It gets a grace period before its clock starts counting against it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACTIVE = "active"
STALE = "stale"
ARCHIVED = "archived"
STATES = (ACTIVE, STALE, ARCHIVED)

USAGE_FILENAME = "skill-usage.json"
ARCHIVE_DIRNAME = ".archive"

# Written into a skill's frontmatter by whatever created it. The marker is the
# provenance — not the directory, which a copy would get wrong.
CREATED_BY_KEY = "created_by"
AGENT = "agent"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def usage_path(home: Path) -> Path:
    return Path(home) / USAGE_FILENAME


def archive_dir(home: Path) -> Path:
    return Path(home) / "skills" / ARCHIVE_DIRNAME


def load(home: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(usage_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: record
        for name, record in data.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def save(home: Path, data: dict[str, dict[str, Any]]) -> None:
    """Write the whole file, atomically.

    Losing this file costs the history, never a skill: every state it holds is
    recomputed from timestamps, and a missing record reads as "new".
    """
    path = usage_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def blank_record() -> dict[str, Any]:
    return {
        "created_at": now_iso(),
        "last_used_at": "",
        "uses": 0,
        "state": ACTIVE,
        "pinned": False,
    }


def record_for(home: Path, name: str) -> dict[str, Any]:
    return load(home).get(name, blank_record())


def note_use(home: Path, name: str) -> None:
    """One `skill_load`. The only thing that writes evidence of use.

    Deliberately cheap and best-effort: this sits on the path of a tool the
    model calls, and a failure to record a use must never fail the load.
    """
    try:
        data = load(home)
        record = data.get(name) or blank_record()
        record["uses"] = int(record.get("uses", 0) or 0) + 1
        record["last_used_at"] = now_iso()
        if record.get("state") == STALE:
            # Used again. A skill that came back is active by definition, and
            # waiting for the next curator pass to say so would leave the
            # library describing something that is no longer true.
            record["state"] = ACTIVE
        data[name] = record
        save(home, data)
    except Exception:  # noqa: BLE001 - never fail a skill load over bookkeeping
        pass


def seed(home: Path, name: str, *, created_by: str = "") -> None:
    """Start a skill's clock now, rather than at the epoch.

    Called the first time a curatable skill is seen. Without it, a skill that
    predates the record looks infinitely old and is archived on the first pass
    — which is exactly the surprise that would make somebody turn this off.
    """
    data = load(home)
    if name in data:
        return
    record = blank_record()
    if created_by:
        record["created_by"] = created_by
    data[name] = record
    save(home, data)


def set_state(home: Path, name: str, state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    data = load(home)
    record = data.get(name) or blank_record()
    record["state"] = state
    data[name] = record
    save(home, data)


def set_pinned(home: Path, name: str, pinned: bool) -> None:
    data = load(home)
    record = data.get(name) or blank_record()
    record["pinned"] = bool(pinned)
    if pinned and record.get("state") == STALE:
        record["state"] = ACTIVE
    data[name] = record
    save(home, data)


def forget(home: Path, name: str) -> None:
    data = load(home)
    if data.pop(name, None) is not None:
        save(home, data)


# ---------------------------------------------------------------------------
# Who wrote it
# ---------------------------------------------------------------------------


def created_by(skill: Any) -> str:
    """Read the provenance marker out of a parsed skill's metadata."""
    metadata = getattr(skill, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get(CREATED_BY_KEY)
        if isinstance(value, str):
            return value.strip()
    return ""


def is_agent_created(skill: Any) -> bool:
    return created_by(skill) == AGENT


def is_curatable(skill: Any, home: Path) -> bool:
    """Whether this file's opinions apply to a skill.

    Two conditions, both required: the agent wrote it, and it lives in the
    user's own skills directory. A skill inside a workspace belongs to that
    repository — moving it out from under a checkout would be this program
    editing somebody else's project.
    """
    if not is_agent_created(skill):
        return False
    try:
        path = Path(skill.path).resolve()
        root = (Path(home) / "skills").resolve()
    except OSError:
        return False
    return root == path or root in path.parents


def mark_created_by_agent(skill_file: Path) -> bool:
    """Stamp a skill as agent-written, in its own frontmatter.

    Returns whether the file was changed. Idempotent, and it leaves a skill
    that already carries a different marker alone — an author who wrote one by
    hand and said so is not overruled by this.
    """
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if f"{CREATED_BY_KEY}: {AGENT}" in text:
        return False
    if not text.startswith("---"):
        return False

    parts = text.split("---", 2)
    if len(parts) != 3:
        return False

    front = parts[1].rstrip("\n")
    if "metadata:" in front:
        # Left alone rather than merged. Editing somebody's YAML by string
        # surgery is how a skill stops parsing, and the cost of not marking one
        # is that it is not curated — which is the safe direction.
        return False

    front += f"\nmetadata:\n  andromeda:\n    {CREATED_BY_KEY}: {AGENT}\n"
    try:
        skill_file.write_text(f"---{front}\n---{parts[2]}", encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# The report the curator works from
# ---------------------------------------------------------------------------


def report(
    skills: dict[str, Any], home: Path, *, include_uncurated: bool = False
) -> list[dict[str, Any]]:
    """One row per skill: its record, its provenance, and how idle it is."""
    data = load(home)
    rows: list[dict[str, Any]] = []
    now = datetime.now(tz=timezone.utc)

    for name in sorted(skills):
        skill = skills[name]
        curatable = is_curatable(skill, home)
        if not curatable and not include_uncurated:
            continue

        stored = data.get(name)
        record = stored or blank_record()
        anchor = parse_iso(record.get("last_used_at")) or parse_iso(
            record.get("created_at")
        ) or now
        rows.append(
            {
                "name": name,
                "state": record.get("state", ACTIVE),
                "pinned": bool(record.get("pinned")),
                "uses": int(record.get("uses", 0) or 0),
                "created_at": record.get("created_at", ""),
                "last_used_at": record.get("last_used_at", ""),
                "idle_days": max(0.0, (now - anchor).total_seconds() / 86400),
                "curatable": curatable,
                "created_by": created_by(skill) or "you",
                "recorded": stored is not None,
                "path": str(Path(skill.path).parent),
            }
        )
    return rows


def archived_names(home: Path) -> list[str]:
    directory = archive_dir(home)
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if entry.is_dir())


def archive(home: Path, name: str, skill_dir: Path) -> tuple[bool, str]:
    """Move a skill out of the way. Never delete it.

    The directory keeps its name inside `.archive/`, which is what makes
    `restore` a move back rather than a reconstruction.
    """
    destination = archive_dir(home) / name
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            # A second archive of the same name. Keeping both matters more
            # than a tidy directory: the older one may be the one wanted.
            stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
            destination = destination.with_name(f"{name}.{stamp}")
        shutil.move(str(skill_dir), str(destination))
    except (OSError, shutil.Error) as exc:
        return False, str(exc)

    set_state(home, name, ARCHIVED)
    return True, str(destination)


def restore(home: Path, name: str) -> tuple[bool, str]:
    """Bring an archived skill back to where it was."""
    source = archive_dir(home) / name
    if not source.is_dir():
        return False, f"nothing archived under {name!r}"

    destination = Path(home) / "skills" / name
    if destination.exists():
        return False, f"{destination} already exists"

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    except (OSError, shutil.Error) as exc:
        return False, str(exc)

    set_state(home, name, ACTIVE)
    return True, str(destination)


def idle_beyond(row: dict[str, Any], days: int) -> bool:
    return row["idle_days"] >= days


def since(days: int) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=days)
