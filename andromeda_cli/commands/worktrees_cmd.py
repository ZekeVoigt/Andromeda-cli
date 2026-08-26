"""`andromeda worktrees` — what the lanes left behind, and reclaiming it.

Lanes remove their own working copy when it holds nothing. What survives is
what held something: a branch with commits, a tree with uncommitted edits,
untracked scratch. That is the right default — but over weeks it is also a
directory nobody looks in, so this is the attended pass.

`prune` deletes only what `list` calls reapable, and the classification fails
safe toward keeping in every direction: a git probe that does not answer is
not evidence that a tree is empty.
"""

from __future__ import annotations

from pathlib import Path

from andromeda_agent import worktrees

from .. import output


def _root(path: str = "") -> str | None:
    return worktrees.repo_root(path or Path.cwd())


def show_list(path: str = "") -> int:
    root = _root(path)
    if root is None:
        output.fail(
            "Not inside a git repository.",
            "Worktree isolation only applies to a git checkout.",
        )
        return 2

    records = worktrees.audit(root)
    if not records:
        output.info(f"  no lane worktrees under {root}/{worktrees.WORKTREES_DIRNAME}")
        return 0

    reapable = [record for record in records if record.verdict == "reap"]
    plural = "" if len(records) == 1 else "s"
    output.info(f"  {len(records)} worktree{plural}\n")

    for record in records:
        mark = "[red]✗[/red]" if record.verdict == "reap" else "[green]✓[/green]"
        output.console.print(f"  {mark} [cyan]{record.name}[/cyan] [dim]{record.branch}[/dim]")
        output.console.print(f"      [dim]{record.reason}[/dim]")

    if reapable:
        output.console.print()
        output.info(
            f"  {len(reapable)} can be removed — andromeda worktrees prune"
        )
    return 0


def prune(path: str = "", dry_run: bool = False) -> int:
    root = _root(path)
    if root is None:
        output.fail("Not inside a git repository.")
        return 2

    actions, kept = worktrees.reclaim(root, dry_run=dry_run)

    for line in actions:
        output.console.print(f"  [dim]{line}[/dim]")

    if kept:
        # Named, not counted. "Nothing to reclaim" without saying what is being
        # held onto is the answer that makes people delete the directory by hand.
        output.console.print()
        plural = "" if len(kept) == 1 else "s"
        output.info(f"  kept {len(kept)} worktree{plural} holding work:")
        for record in kept:
            output.console.print(f"      [dim]{record.name}: {record.reason}[/dim]")

    if not actions:
        output.info("  nothing to reclaim")
        return 0

    verb = "would be removed" if dry_run else "removed"
    plural = "" if len(actions) == 1 else "s"
    output.ok(f"{len(actions)} worktree{plural} {verb}.")
    return 0
