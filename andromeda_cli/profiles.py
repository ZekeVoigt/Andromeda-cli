"""Several independent installs, one program.

A profile is a whole `ANDROMEDA_HOME` of its own: its own config, credentials,
sessions, memories, skills, scheduled jobs and state index. Nothing crosses
between them, which is the point — a work profile that has been paired to a
work account and a personal one that has not should not be able to read each
other's transcripts by accident.

**The default profile is the home directory itself**, not a directory called
`default` inside it. That is what makes this free to add: an existing install
is already the default profile, there is nothing to migrate, and a person who
never runs `andromeda profile` never sees one.

Resolution order, most explicit first:

1. ``ANDROMEDA_HOME``      — an absolute answer; no profile lookup happens.
2. ``--profile`` / ``ANDROMEDA_PROFILE``
3. the sticky choice in ``~/.andromeda-cli/profile``
4. the default profile, ``~/.andromeda-cli``

``ANDROMEDA_HOME`` winning outright matters: it is what a test, a container and
a scheduled job use to be certain which state they are touching, and a sticky
profile file quietly overriding it would make that guarantee false.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

ENV_PROFILE = "ANDROMEDA_PROFILE"
DEFAULT = "default"
STICKY = "profile"
PROFILES_DIR = "profiles"

# Lowercase, starts alphanumeric, no dots and no separators. The name becomes a
# directory under the home, so anything that could climb out of it — `..`, a
# slash, a backslash, a leading dash — is rejected here rather than sanitised,
# because a sanitised name silently addresses a different profile than the one
# that was typed.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Copied by `--clone`: the parts of an install that are a considered setup
# rather than accumulated history. Credentials are never among them — a clone
# is a new install, and it pairs itself.
CLONE_FILES = ("config.yaml", "SOUL.md", "mcp.json")
CLONE_DIRS = ("skills",)

# Never copied by `--clone-all` either. Runtime state that describes the
# machine at a moment, and a live device token.
NEVER_CLONE = (
    "credentials.json",
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "history",
    "checkout",
    PROFILES_DIR,
    STICKY,
)


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    path: Path
    current: bool = False
    exists: bool = True

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT


def default_root() -> Path:
    """The default profile's directory — also where profiles are kept.

    Read from the environment directly rather than through `config.home`,
    because `config.home` calls back into here and would close the cycle.
    """
    override = os.environ.get("ANDROMEDA_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".andromeda-cli"


def profiles_dir() -> Path:
    return default_root() / PROFILES_DIR


def validate(name: str) -> str:
    candidate = (name or "").strip().lower()
    if not candidate:
        raise ProfileError("A profile needs a name.")
    if candidate == DEFAULT:
        return DEFAULT
    if not NAME.match(candidate):
        raise ProfileError(
            f"{name!r} is not a usable profile name. Lowercase letters, digits, "
            "`-` and `_`, starting with a letter or digit."
        )
    return candidate


def path_for(name: str) -> Path:
    """Where a named profile's home is. Does not create it."""
    resolved = validate(name)
    if resolved == DEFAULT:
        return default_root()
    return profiles_dir() / resolved


def sticky_path() -> Path:
    return default_root() / STICKY


def sticky() -> str:
    """The profile chosen by `andromeda profile use`, or "" for none."""
    try:
        raw = sticky_path().read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    try:
        return validate(raw) if raw else ""
    except ProfileError:
        # A hand-edited file naming something impossible reads as "no sticky
        # choice" rather than breaking every command until it is fixed.
        return ""


def selected() -> str:
    """The profile this process is using, by name."""
    if os.environ.get("ANDROMEDA_HOME", "").strip():
        # An explicit home is an explicit home. It is not a profile, and
        # calling it one would let a sticky choice appear to redirect it.
        return DEFAULT
    from_env = os.environ.get(ENV_PROFILE, "").strip().lower()
    if from_env:
        try:
            return validate(from_env)
        except ProfileError:
            return DEFAULT
    return sticky() or DEFAULT


def home() -> Path:
    """The home directory for the profile this process is using."""
    override = os.environ.get("ANDROMEDA_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return path_for(selected())


def exists(name: str) -> bool:
    return path_for(name).is_dir()


def listing() -> list[Profile]:
    active = selected()
    found = [
        Profile(
            name=DEFAULT,
            path=default_root(),
            current=active == DEFAULT,
            exists=default_root().is_dir(),
        )
    ]
    directory = profiles_dir()
    if directory.is_dir():
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            try:
                name = validate(child.name)
            except ProfileError:
                continue
            found.append(
                Profile(name=name, path=child, current=active == name, exists=True)
            )
    return found


def create(name: str, *, clone: bool = False, clone_all: bool = False) -> Profile:
    resolved = validate(name)
    if resolved == DEFAULT:
        raise ProfileError("`default` is this install's own home; it always exists.")
    target = path_for(resolved)
    if target.exists():
        raise ProfileError(f"Profile {resolved!r} already exists at {target}.")

    target.mkdir(parents=True)
    for directory in ("sessions", "memory", "skills", "cron"):
        (target / directory).mkdir(exist_ok=True)

    source = default_root()
    if clone_all:
        for child in source.iterdir():
            if child.name in NEVER_CLONE:
                continue
            destination = target / child.name
            if child.is_dir():
                shutil.copytree(child, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(child, destination)
    elif clone:
        for filename in CLONE_FILES:
            candidate = source / filename
            if candidate.is_file():
                shutil.copy2(candidate, target / filename)
        for directory in CLONE_DIRS:
            candidate = source / directory
            if candidate.is_dir():
                shutil.copytree(candidate, target / directory, dirs_exist_ok=True)

    return Profile(name=resolved, path=target, current=False, exists=True)


def use(name: str) -> Profile:
    resolved = validate(name)
    if resolved != DEFAULT and not exists(resolved):
        raise ProfileError(
            f"No profile {resolved!r}. `andromeda profile create {resolved}` makes one."
        )
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    if resolved == DEFAULT:
        # Choosing the default is the absence of a choice, so the file goes
        # away rather than holding the string "default" — one representation
        # of "no profile selected", not two.
        sticky_path().unlink(missing_ok=True)
    else:
        sticky_path().write_text(resolved + "\n", encoding="utf-8")
    return Profile(name=resolved, path=path_for(resolved), current=True)


def delete(name: str, *, force: bool = False) -> Path:
    """Remove a profile and everything in it.

    Refuses the default and refuses the one in use. Deleting the profile you
    are standing in leaves a running session writing into a directory that no
    longer exists, and the error it produces names neither cause.
    """
    resolved = validate(name)
    if resolved == DEFAULT:
        raise ProfileError("The default profile is this install; it cannot be deleted.")
    target = path_for(resolved)
    if not target.is_dir():
        raise ProfileError(f"No profile {resolved!r}.")
    if selected() == resolved and not force:
        raise ProfileError(
            f"{resolved!r} is the profile in use. Switch first "
            "(`andromeda profile use default`), or pass --force."
        )

    # Belt and braces on a recursive delete: the path must still be under the
    # profiles directory after resolution, so a symlinked profile directory
    # cannot redirect this at somebody's home folder.
    root = profiles_dir().resolve()
    resolved_target = target.resolve()
    if root not in resolved_target.parents:
        raise ProfileError(f"{target} is not inside {root}; refusing to delete it.")

    shutil.rmtree(resolved_target)
    if sticky() == resolved:
        sticky_path().unlink(missing_ok=True)
    return resolved_target
