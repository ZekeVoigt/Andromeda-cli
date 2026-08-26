"""Skills: instructions the agent loads on demand.

Reads the *existing* `skills/` directory rather than a Python-only variant —
`skills/<name>/SKILL.md`, YAML frontmatter, markdown body. Discovery mirrors the
TypeScript resolver: `ANDROMEDA_BUNDLED_SKILLS_DIR` wins, then a `skills/`
directory found by walking up from the workspace, then `$ANDROMEDA_HOME/skills`,
then the `skills/` that shipped with this install. Nearest to the task wins;
what we ship is the floor, not the ceiling.

Bodies are deliberately *not* preloaded. A manifest of names and one-line
descriptions costs a few hundred tokens; eleven skill bodies cost thousands on
every turn, most of them irrelevant. The model reads the manifest and calls
`skill_load` for the one it needs.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .spec import ToolResult, failure

ENV_SKILLS_DIR = "ANDROMEDA_BUNDLED_SKILLS_DIR"
SKILL_FILE = "SKILL.md"
MAX_WALK_UP = 6
DELIMITER = "---"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    requires_bins: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """Whether this skill's required binaries are actually on this machine.

        A skill whose `gh` is not installed is worse than a missing skill: the
        model follows instructions that cannot work and reports a failure the
        user has to decode.
        """
        return all(shutil.which(binary) for binary in self.requires_bins)

    @property
    def missing_bins(self) -> list[str]:
        return [binary for binary in self.requires_bins if not shutil.which(binary)]


def _looks_like_skills_dir(candidate: Path) -> bool:
    try:
        for entry in candidate.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                if (entry / SKILL_FILE).exists():
                    return True
    except OSError:
        return False
    return False


def resolve_skills_dir(start: Path | None = None) -> Path | None:
    override = os.environ.get(ENV_SKILLS_DIR, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    current = (start or Path.cwd()).resolve()
    for _ in range(MAX_WALK_UP):
        candidate = current / "skills"
        if _looks_like_skills_dir(candidate):
            return candidate
        if current.parent == current:
            break
        current = current.parent

    # Follows ANDROMEDA_HOME, so a relocated home takes its skills with it.
    # Read from the environment rather than importing the config module: this
    # package sits below `andromeda_cli` and must not depend upward.
    override = os.environ.get("ANDROMEDA_HOME", "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".andromeda-cli"
    personal = root / "skills"
    if _looks_like_skills_dir(personal):
        return personal

    return bundled_skills_dir()


def bundled_skills_dir() -> Path | None:
    """The `skills/` directory that shipped with this install.

    Last, and that ordering is the point: a project's own skills and then the
    user's own override what we ship, because a skill is instructions and the
    person closest to the task should win.

    But it has to exist at all, and until now it did not. Every earlier
    candidate is relative to where the user is standing or to their home, so
    installing the CLI and running it from an ordinary project directory
    resolved to nothing — the bundled skills were published, sat in the
    checkout, and were unreachable to every user who did not happen to be
    working inside it. Found by installing from the distribution repository and
    running from a neutral directory, which is what everybody does.

    Resolved by walking up from this file so it holds in both layouts: the
    package is the tree in a distribution checkout and one directory of it in
    the monorepo, and `skills/` sits above the package in each.
    """
    here = Path(__file__).resolve()
    for parent in here.parents[:MAX_WALK_UP]:
        candidate = parent / "skills"
        if _looks_like_skills_dir(candidate):
            return candidate
    return None


def parse_skill(path: Path) -> Skill | None:
    """Split frontmatter from body. A malformed skill is skipped, not fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    front: dict[str, Any] = {}
    body = text

    if text.startswith(DELIMITER):
        parts = text.split(DELIMITER, 2)
        if len(parts) == 3:
            try:
                loaded = yaml.safe_load(parts[1]) or {}
                if isinstance(loaded, dict):
                    front = loaded
                body = parts[2].lstrip("\n")
            except yaml.YAMLError:
                # Keep the body. Instructions with an unreadable header are
                # still instructions; a name falls back to the directory.
                body = parts[2].lstrip("\n") if len(parts) == 3 else text

    andromeda = ((front.get("metadata") or {}).get("andromeda") or {}) if isinstance(
        front.get("metadata"), dict
    ) else {}
    requires = andromeda.get("requires") or {}
    bins = requires.get("bins") if isinstance(requires, dict) else None

    return Skill(
        name=str(front.get("name") or path.parent.name),
        description=str(front.get("description") or "").strip(),
        path=path,
        body=body.strip(),
        requires_bins=[str(item) for item in bins] if isinstance(bins, list) else [],
        metadata=andromeda if isinstance(andromeda, dict) else {},
    )


def skills_dirs(start: Path | None = None) -> list[Path]:
    """Every directory skills are read from, furthest first.

    Layered rather than first-match, and that is a deliberate change from what
    this did originally. "A project's own skills override what we ship" was
    implemented as *replace*: one `skills/` directory in a repository hid the
    user's entire personal library, including anything the agent had written
    for itself. Override means per name — the nearest layer wins a collision,
    and everything else is still there.

    Furthest first so the nearest can overwrite it: bundled, then your own,
    then the workspace. `ANDROMEDA_BUNDLED_SKILLS_DIR` still replaces the lot,
    because a test or a packaging step that pins the directory means it.
    """
    override = os.environ.get(ENV_SKILLS_DIR, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return [candidate] if candidate.is_dir() else []

    roots: list[Path] = []

    bundled = bundled_skills_dir()
    if bundled is not None:
        roots.append(bundled)

    home_override = os.environ.get("ANDROMEDA_HOME", "").strip()
    root = Path(home_override).expanduser() if home_override else Path.home() / ".andromeda-cli"
    personal = root / "skills"
    if _looks_like_skills_dir(personal):
        roots.append(personal)

    current = (start or Path.cwd()).resolve()
    for _ in range(MAX_WALK_UP):
        candidate = current / "skills"
        if _looks_like_skills_dir(candidate):
            roots.append(candidate)
            break
        if current.parent == current:
            break
        current = current.parent

    # De-duplicated by resolved path, keeping the first occurrence: running
    # inside the install's own checkout makes the workspace and the bundled
    # directory the same place, and reading it twice would be harmless but
    # would report every skill as coming from wherever it was read last.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in roots:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def discover(start: Path | None = None) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for root in skills_dirs(start):
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_file = entry / SKILL_FILE
            if not skill_file.exists():
                continue
            skill = parse_skill(skill_file)
            if skill is not None:
                # Later layer wins the name; everything else survives.
                skills[skill.name] = skill
    return skills


def manifest(skills: dict[str, Skill]) -> str:
    """The line that goes in the system prompt. Names and descriptions only."""
    if not skills:
        return ""

    lines = ["Skills available. Call skill_load before following one."]
    for skill in sorted(skills.values(), key=lambda item: item.name):
        note = ""
        if not skill.available:
            note = f" [unavailable: needs {', '.join(skill.missing_bins)}]"
        lines.append(f"  {skill.name} — {skill.description}{note}")
    return "\n".join(lines)


def load_skill(
    skills: dict[str, Skill],
    name: str,
    resource: str | None = None,
    home: "Path | None" = None,
) -> ToolResult:
    skill = skills.get(name)
    if skill is None:
        known = ", ".join(sorted(skills)) or "none"
        return failure(f"No skill named {name!r}. Available: {known}")

    if resource:
        # Confined to the skill's own directory for the same reason the file
        # tools are confined to the workspace: `../../.ssh/id_rsa` is a path.
        skill_dir = skill.path.parent.resolve()
        target = (skill_dir / resource).resolve()
        if target != skill_dir and skill_dir not in target.parents:
            return failure(f"{resource} is outside the {name} skill directory.")
        if not target.is_file():
            return failure(f"{name} has no resource at {resource}.")
        try:
            return ToolResult(
                content=target.read_text(encoding="utf-8"),
                display=f"{name}/{resource}",
            )
        except (OSError, UnicodeDecodeError) as exc:
            return failure(f"Could not read {resource}: {exc}")

    # Recorded here, at the one place a skill is actually used. Best-effort by
    # contract — the curator's arithmetic is worth less than a skill that
    # loads, so a failure to write the record never fails the load.
    if home is not None:
        from . import skill_usage

        skill_usage.note_use(home, name)

    body = skill.body
    if not skill.available:
        # Stated up front, so the model does not follow instructions whose
        # tools are absent and then report a confusing failure.
        body = (
            f"[This skill needs {', '.join(skill.missing_bins)}, which "
            f"{'is' if len(skill.missing_bins) == 1 else 'are'} not installed on "
            f"this machine. Say so rather than attempting the steps below.]\n\n{body}"
        )

    return ToolResult(content=body, display=f"{name} skill loaded")
