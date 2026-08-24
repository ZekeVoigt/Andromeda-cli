"""Updating an installed checkout, transactionally.

The failure this exists to prevent: `git reset` lands new code, then the
dependency install fails, and the next `andromeda` is a new tree against old
packages — usually an ImportError before the CLI can even print a message. The
reference implementation carries a whole `_install_repair` module because of
it.

So the revision is recorded before anything moves, and rolled back if the
install does not complete. A failed update leaves the install exactly where it
was, working.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .. import output

GIT_TIMEOUT = 300
INSTALL_TIMEOUT = 900


def install_root() -> Path | None:
    """The checkout this CLI is running out of, if it is one.

    Resolved from the package rather than from `cwd`: the whole point of an
    update command is that it works from wherever the user happens to be.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate
    return None


def package_dir(root: Path) -> Path:
    """Where `pyproject.toml` sits inside a checkout.

    Two layouts are real and both must install. In the monorepo the CLI is one
    directory of a larger tree (`<root>/cli`); in the standalone distribution
    repo it *is* the tree (`<root>`). Probing for the marker beats recording
    which kind an install came from, because a recorded flag is a thing that
    can be wrong — a checkout that was re-cloned from the other source would
    still claim its original shape.
    """
    nested = root / "cli"
    if (nested / "pyproject.toml").is_file():
        return nested
    return root


def _install_command(cli_dir: Path) -> list[str]:
    """How to reinstall this checkout into its own venv.

    **`uv` first, and this is not a preference.** The installer builds the venv
    with `uv venv`, which does not put pip in it — so `python -m pip` fails with
    "No module named pip" on every install this project produces. `update` used
    it anyway, which meant the command could never succeed: it reset to the new
    revision, failed to install, rolled back, and reported that the install
    still worked. It did — at the old version, forever.

    Found by running `andromeda update` on a real install rather than on a
    development checkout, where pip happens to be present.

    Falls back to `python -m pip` for a venv that was built by hand and does
    have it, so both shapes update.
    """
    uv = shutil.which("uv")
    if uv:
        return [
            uv, "pip", "install",
            "--python", sys.executable,
            "--quiet", "-e", str(cli_dir),
        ]
    return [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(cli_dir)]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def _revision(root: Path) -> str | None:
    result = _git(root, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _is_dirty(root: Path) -> bool:
    result = _git(root, "status", "--porcelain")
    return bool(result.stdout.strip())


def run(check_only: bool = False) -> int:
    root = install_root()
    if root is None:
        output.fail(
            "This does not look like a git checkout, so there is nothing to update.",
            "Reinstall with the installer to get updates.",
        )
        return 1

    cli_dir = package_dir(root)
    before = _revision(root)
    if before is None:
        output.fail(f"Could not read the revision at {root}.")
        return 1

    if _is_dirty(root):
        # Resetting over someone's edits is not an update, it is data loss.
        output.fail(
            "The checkout has uncommitted changes.",
            "Commit or stash them first — updating would discard them.",
        )
        return 1

    output.info("Fetching…")
    fetched = _git(root, "fetch", "--quiet", "origin")
    if fetched.returncode != 0:
        output.fail(f"Could not fetch: {fetched.stderr.strip()[:200]}")
        return 1

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    target = _git(root, "rev-parse", f"origin/{branch}").stdout.strip()
    if not target:
        output.fail(f"origin/{branch} does not exist.")
        return 1

    if target == before:
        output.ok("Already up to date.")
        return 0

    behind = _git(root, "rev-list", "--count", f"{before}..{target}").stdout.strip()
    if check_only:
        output.info(f"{behind} commit(s) available on origin/{branch}.")
        output.info("  andromeda update")
        return 0

    output.info(f"Updating {behind} commit(s)…")
    reset = _git(root, "reset", "--hard", "--quiet", target)
    if reset.returncode != 0:
        output.fail(f"Could not update: {reset.stderr.strip()[:200]}")
        return 1

    output.info("Installing dependencies…")
    installed = subprocess.run(
        _install_command(cli_dir),
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
    )

    if installed.returncode != 0:
        # The half-updated state is the dangerous one, so undo it rather than
        # leaving a tree the interpreter cannot import.
        output.fail("Dependency install failed — rolling back.")
        rolled = _git(root, "reset", "--hard", "--quiet", before)
        if rolled.returncode == 0:
            output.ok(f"Rolled back to {before[:8]}. The install still works.")
        else:
            output.fail(
                f"Rollback also failed. The checkout is at {target[:8]} with old "
                f"dependencies. Run: pip install -e {cli_dir}"
            )
        output.console.print(f"[dim]{installed.stderr.strip()[:400]}[/dim]")
        return 1

    output.ok(f"Updated to {target[:8]}.")
    return 0
