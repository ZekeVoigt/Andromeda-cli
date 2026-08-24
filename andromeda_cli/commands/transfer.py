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
# `history` is per-machine noise, `checkout/` is code, and a venv is not a thing
# you move.
PORTABLE = ("config.yaml", "mcp.json", "approvals.json", "memory", "sessions", "skills")
SECRETS = ("credentials.json",)
MANIFEST = "andromeda-manifest.json"


def _members(home: Path, include_secrets: bool) -> list[Path]:
    names = [*PORTABLE, *(SECRETS if include_secrets else ())]
    return [home / name for name in names if (home / name).exists()]


def _write(destination: Path, home: Path, include_secrets: bool) -> tuple[int, list[str]]:
    members = _members(home, include_secrets)
    included: list[str] = []

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for path in members:
            archive.add(path, arcname=path.name)
            included.append(path.name)

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
    if not manifest.get("includesCredentials", False):
        output.info("  No credentials in this archive — run `andromeda auth login`.")
    return 0
