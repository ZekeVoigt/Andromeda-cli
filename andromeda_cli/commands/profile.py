"""Creating, listing, switching and deleting profiles."""

from __future__ import annotations

from .. import config as config_module
from .. import output
from .. import profiles


def _size(path) -> str:
    try:
        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return ""
    if total > 1024 * 1024:
        return f"{total / 1024 / 1024:.0f} MB"
    return f"{total / 1024:.0f} KB"


def show_list() -> int:
    found = profiles.listing()
    for profile in found:
        mark = "[green]●[/green]" if profile.current else " "
        note = " [dim](this install's home)[/dim]" if profile.is_default else ""
        missing = "" if profile.exists else " [dim](not created yet)[/dim]"
        output.console.print(
            f" {mark} [cyan]{profile.name.ljust(16)}[/cyan] "
            f"[dim]{_size(profile.path).rjust(7)}  {profile.path}[/dim]{note}{missing}"
        )
    output.console.print()
    output.info("  andromeda -p <name> …        use one for a single command")
    output.info("  andromeda profile use <name> make it the default")
    return 0


def create(name: str, clone: bool = False, clone_all: bool = False) -> int:
    try:
        profile = profiles.create(name, clone=clone, clone_all=clone_all)
    except profiles.ProfileError as exc:
        output.fail(str(exc))
        return 2
    output.ok(f"Created {profile.name} at {profile.path}")
    if clone or clone_all:
        output.info("  Settings copied. Credentials were not — sign this profile in:")
    else:
        output.info("  Fresh and empty. Sign it in:")
    output.info(f"  andromeda -p {profile.name} auth login")
    return 0


def use(name: str) -> int:
    try:
        profile = profiles.use(name)
    except profiles.ProfileError as exc:
        output.fail(str(exc))
        return 2
    output.ok(f"Now using {profile.name}")
    output.console.print(f"  [dim]{profile.path}[/dim]", soft_wrap=True)
    return 0


def delete(name: str, force: bool = False) -> int:
    try:
        target = profiles.path_for(name)
    except profiles.ProfileError as exc:
        output.fail(str(exc))
        return 2

    if not force:
        # Named rather than counted: "deletes 1 profile" tells you nothing
        # about whether it is the one you meant.
        output.fail(
            f"This would permanently delete {target} ({_size(target)}) — "
            "sessions, memories, credentials and all.",
            "Pass --force if that is what you want.",
        )
        return 2

    try:
        removed = profiles.delete(name, force=force)
    except profiles.ProfileError as exc:
        output.fail(str(exc))
        return 2
    output.ok(f"Deleted {removed}")
    return 0


def current() -> int:
    name = profiles.selected()
    output.console.print(f"  [cyan]{name}[/cyan]")
    output.console.print(f"  [dim]{config_module.home()}[/dim]", soft_wrap=True)
    return 0
