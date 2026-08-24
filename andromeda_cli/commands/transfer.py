"""Moving this install's state somewhere else.

Two verbs, deliberately, because they differ in exactly one way and that way is
the whole point:

  `backup`  — everything, **including the device token**. For moving your own
              install to your own new machine. Treat the file like a password.
  `export`  — everything except credentials. For sharing a setup, checking it
              into a dotfiles repo, or handing someone your skills and config.

Two verbs rather than one flag: collapsing them into a single flagged command
is how a file that quietly contains a live credential ends up in a git
repository.
"""

from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

from .. import config as config_module
from .. import output

# Relative to ANDROMEDA_HOME. Anything not listed is not portable state:
# `history` is per-machine noise, `checkout/` is code, `profiles/` are separate
# installs that back themselves up, and a venv is not a thing you move.
#
# `state.db` is deliberately absent. It is a derived index over `sessions/`,
# so carrying it would add the largest file in the home to every archive to
# save one reindex on the other machine — and a half-restored index is worse
# than none. `restore` rebuilds it.
PORTABLE = ("config.yaml", "mcp.json", "approvals.json", "memory", "sessions", "skills")
SECRETS = ("credentials.json",)
MANIFEST = "andromeda-manifest.json"
# Where a portable snapshot of the memories goes inside the archive, whichever
# backend they actually live in. See `_memory_snapshot`.
MEMORY_SNAPSHOT = "memory/memories.json"


def _members(home: Path, include_secrets: bool) -> list[Path]:
    names = [*PORTABLE, *(SECRETS if include_secrets else ())]
    return [home / name for name in names if (home / name).exists()]


def _memory_snapshot(home: Path) -> str:
    """Every memory, as the portable JSON shape, whatever backend holds them.

    The sqlite backend keeps memories in `state.db`, which is not portable —
    so without this an export from a sqlite-backend install would silently
    carry no memories at all, and the person would find that out on the other
    machine. Read through the store rather than the file, so the archive says
    the same thing on both backends.
    """
    from andromeda_tools import MemoryStore

    try:
        config = config_module.load()
    except config_module.ConfigError:
        config = {}
    store = MemoryStore(home / "memory", config.get("memory_backend"))
    return json.dumps([memory.to_json() for memory in store.load()], indent=2)


def _write(destination: Path, home: Path, include_secrets: bool) -> tuple[int, list[str]]:
    members = _members(home, include_secrets)
    included: list[str] = []

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        carried_memory = False
        for path in members:
            archive.add(path, arcname=path.name)
            included.append(path.name)
            carried_memory = carried_memory or path.name == "memory"

        snapshot = _memory_snapshot(home)
        if snapshot != "[]" and not carried_memory:
            # Only when the memory directory did not already go in: the json
            # backend's own file is the snapshot, and adding a second copy
            # under the same name would depend on tar member ordering to
            # decide which one a restore sees.
            staged = destination.parent / "memories.json"
            staged.write_text(snapshot, encoding="utf-8")
            archive.add(staged, arcname=MEMORY_SNAPSHOT)
            staged.unlink()
            included.append("memory")

        manifest = destination.parent / MANIFEST
        manifest.write_text(
            json.dumps(
                {
                    "created": time.time(),
                    "includesCredentials": any(
                        name in SECRETS for name in included
                    ),
                    "contents": included,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        archive.add(manifest, arcname=MANIFEST)
        manifest.unlink()

    return destination.stat().st_size, included


def _report(destination: Path, size: int, included: list[str], secrets: bool) -> None:
    output.ok(f"Wrote {destination} ({size / 1024:.0f} KB)")
    output.info(f"  {', '.join(included) or 'nothing to include'}")

    # Keyed on what actually went in, not on which verb was used. A backup of an
    # unpaired install holds no token, and warning about one anyway is how a
    # person learns that this warning means nothing.
    carries_credentials = any(name in SECRETS for name in included)
    if carries_credentials:
        output.console.print(
            "  [yellow]This archive contains your device token. "
            "Treat it like a password.[/yellow]"
        )
    elif secrets:
        output.info("  This install is not signed in, so there was no token to include.")
    else:
        output.info("  Credentials excluded — pair again on the other machine.")


def backup(path: str) -> int:
    destination = Path(path).expanduser()
    size, included = _write(destination, config_module.home(), include_secrets=True)
    _report(destination, size, included, secrets=True)
    return 0


def export(path: str) -> int:
    destination = Path(path).expanduser()
    size, included = _write(destination, config_module.home(), include_secrets=False)
    _report(destination, size, included, secrets=False)
    return 0


def _safe_members(archive: tarfile.TarFile, home: Path):
    """Yield only members that land inside the destination.

    A tar entry may name `../../.ssh/authorized_keys`, and extracting an
    archive someone handed you is exactly the situation where that matters.
    Symlinks and devices are dropped for the same reason — nothing this command
    writes needs to be either.
    """
    root = home.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            continue
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            continue
        yield member


def restore(path: str, force: bool = False) -> int:
    source = Path(path).expanduser()
    if not source.exists():
        output.fail(f"{source} does not exist.")
        return 2

    home = config_module.home()
    existing = [name for name in (*PORTABLE, *SECRETS) if (home / name).exists()]
    if existing and not force:
        output.fail(
            f"{home} already has {', '.join(existing)}.",
            "Move it aside, or pass --force to overwrite.",
        )
        return 2

    try:
        with tarfile.open(source, "r:gz") as archive:
            manifest = {}
            try:
                handle = archive.extractfile(MANIFEST)
                if handle is not None:
                    manifest = json.loads(handle.read().decode("utf-8"))
            except (KeyError, json.JSONDecodeError, OSError):
                pass

            home.mkdir(parents=True, exist_ok=True)
            members = [m for m in _safe_members(archive, home) if m.name != MANIFEST]
            # `filter="data"` is belt and braces alongside `_safe_members`: it
            # is what Python 3.14 will do by default, it strips ownership and
            # permission surprises, and two independent checks on an archive
            # someone else produced is the right number.
            archive.extractall(home, members=members, filter="data")
    except (tarfile.TarError, OSError) as exc:
        output.fail(f"Could not read {source}: {exc}")
        return 1

    output.ok(f"Restored into {home}")
    output.info(f"  {', '.join(sorted({m.name.split('/')[0] for m in members}))}")

    # The index is derived and was not in the archive, so it now describes a
    # different set of transcripts than the ones on disk. Rebuilt here rather
    # than left for the first search to notice, because a restore that ends
    # with "nothing found" reads as a restore that lost the sessions.
    from .. import state

    counts = state.rebuild_index()
    output.info(f"  indexed {counts['scanned']} transcript(s)")

    _import_memories(home)

    if not manifest.get("includesCredentials", False):
        output.info("  No credentials in this archive — run `andromeda auth login`.")
    return 0


def _import_memories(home: Path) -> None:
    """Load the archive's memory snapshot into whichever backend is configured.

    A no-op on the json backend, whose file *is* the snapshot. On sqlite it is
    the whole point: the rows live in a database the archive never carried.
    """
    from andromeda_tools import MemoryStore
    from andromeda_tools.memory import Memory

    try:
        config = config_module.load()
    except config_module.ConfigError:
        return
    if (config.get("memory_backend") or "json") == "json":
        return

    snapshot = home / MEMORY_SNAPSHOT
    if not snapshot.exists():
        return
    try:
        raw = json.loads(snapshot.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        output.info("  The archive's memories could not be read; left as-is.")
        return
    if not isinstance(raw, list):
        return

    parsed = [Memory.from_json(item) for item in raw if isinstance(item, dict)]
    memories = [memory for memory in parsed if memory is not None]
    if not memories:
        return
    MemoryStore(home / "memory", config["memory_backend"]).save(memories)
    output.info(f"  imported {len(memories)} memor{'y' if len(memories) == 1 else 'ies'}")
