"""One git worktree per delegated lane.

Lanes run concurrently. Without this they run concurrently *in the same
directory*: two of them editing the same file interleave their writes, and a
third reads a tree that is half-way through somebody else's change. The
symptom is not a crash — it is a lane that reports success against a file that
no longer says what it read.

Turned on with `worktree_isolation: true`. Each lane gets a worktree branched
from the parent's current `HEAD` at `<repo>/.worktrees/lane-<id>` on branch
`andromeda/lane-<id>`, and its `Workspace` root is that directory, so the
confinement check does the enforcing rather than an instruction in a prompt.

Four rules, and the fourth is the one that matters:

1. **Opt-in, and git-only.** Outside a git repository the setting is ignored
   and lanes share the working directory exactly as they did before. A
   half-applied isolation is worse than none.
2. **The parent reviews.** A lane commits on its own branch. The parent is
   told the path, the branch, how many commits, and whether the tree is
   dirty — enough to merge it or look at it, without guessing.
3. **A worktree holding nothing is removed.** Zero commits and a clean tree
   means the lane read and reported; keeping that is how a repository
   accumulates a hundred empty directories.
4. **Pruning requires proof.** If a git probe fails, the state is *unknown*,
   and unknown is not "clean". The worktree is kept and the report says the
   numbers are unproven — because the parent only ever sees this payload, and
   a default of "0 commits, clean" reads as "the lane did nothing" for a tree
   that may hold an afternoon of work.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 30
WORKTREES_DIRNAME = ".worktrees"
BRANCH_NAMESPACE = "andromeda"


def git(args: list[str], cwd: str | Path, timeout: int = GIT_TIMEOUT):
    """Run git and capture it. Never raises on a non-zero exit — every caller
    here treats failure as "unknown", which is a decision, not an error."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def repo_root(path: str | Path | None) -> str | None:
    """The git top level containing `path`, or None if there is not one."""
    if not path:
        return None
    try:
        candidate = os.path.abspath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return None
    if not os.path.isdir(candidate):
        return None
    try:
        result = git(["rev-parse", "--show-toplevel"], cwd=candidate)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("worktree: rev-parse failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ensure_ignored(root: str) -> None:
    """Keep `.worktrees/` out of `git status`.

    Best-effort: a repository whose .gitignore cannot be written still gets
    isolation, it just also gets a noisy status.
    """
    ignore = Path(root) / ".gitignore"
    entry = f"{WORKTREES_DIRNAME}/"
    try:
        existing = (
            ignore.read_text(encoding="utf-8-sig", errors="replace")
            if ignore.exists()
            else ""
        )
        if entry in existing.splitlines():
            return
        with ignore.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"{entry}\n")
    except OSError as exc:
        logger.debug("worktree: could not update .gitignore: %s", exc)


@dataclass
class Worktree:
    """A lane's isolated checkout, as created."""

    path: str
    branch: str
    root: str
    base_commit: str


@dataclass
class Outcome:
    """What a lane left behind, as the parent reads it."""

    path: str
    branch: str
    commits: int = 0
    dirty: bool = False
    pruned: bool = False
    # True when a probe failed. `commits` and `dirty` are then defaults, not
    # measurements, and nothing may be deleted on the strength of them.
    unproven: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "commits": self.commits,
            "dirty": self.dirty,
            "pruned": self.pruned,
            "unproven": self.unproven,
            "note": self.note,
        }

    def summary(self) -> str:
        """One line for the lane's report header."""
        if self.pruned:
            return "worktree discarded (no changes)"
        if self.unproven:
            return f"worktree {self.path} (branch {self.branch}) — {self.note}"
        parts = []
        if self.commits:
            plural = "" if self.commits == 1 else "s"
            parts.append(f"{self.commits} commit{plural}")
        if self.dirty:
            parts.append("uncommitted changes")
        state = ", ".join(parts) if parts else "nothing committed"
        return f"worktree {self.path} on branch {self.branch} — {state}"


def create(parent_root: str | Path | None, lane_id: str = "") -> Worktree | None:
    """Branch a worktree for one lane, or return None and share the tree.

    Every failure here degrades to shared-workspace behaviour rather than
    stopping the lane: a repository with no commits yet, a read-only parent
    directory, a git that is not installed. Isolation is an improvement on the
    default, never a precondition for running.
    """
    root = repo_root(parent_root)
    if not root:
        return None

    short = (lane_id or uuid.uuid4().hex[:8]).replace("/", "-")
    name = f"lane-{short}"
    branch = f"{BRANCH_NAMESPACE}/{name}"
    path = Path(root) / WORKTREES_DIRNAME / name

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("worktree: cannot create %s: %s", path.parent, exc)
        return None

    _ensure_ignored(root)

    try:
        head = git(["rev-parse", "HEAD"], cwd=root)
        base_commit = head.stdout.strip() if head.returncode == 0 else ""
        added = git(["worktree", "add", str(path), "-b", branch, "HEAD"], cwd=root)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("worktree: creation failed: %s", exc)
        return None

    if added.returncode != 0:
        # Most often an unborn HEAD — a repository with no commits has nothing
        # to branch from. Nothing is wrong; there is just no isolation to give.
        logger.warning("worktree: git worktree add failed: %s", added.stderr.strip())
        return None

    logger.info("worktree created: %s (branch %s)", path, branch)
    return Worktree(path=str(path), branch=branch, root=root, base_commit=base_commit)


def _unproven(outcome: Outcome, reason: str, unmeasured: str = "commits and dirty") -> Outcome:
    """Mark an outcome as un-inspected, and say what was not measured.

    Named precisely, because one probe can succeed while the other fails: a
    bad base commit breaks the commit count while `status` still reports a
    real dirty flag, and calling a measured value unknown is its own misreport.
    """
    outcome.unproven = True
    outcome.note = (
        f"git inspection failed ({reason}): {unmeasured} UNKNOWN — not proven "
        f"empty. The worktree and branch were kept; look at {outcome.path} "
        f"(branch {outcome.branch}) before assuming the lane did nothing."
    )
    logger.warning(
        "worktree: inspection failed (%s) — keeping %s (branch %s)",
        reason,
        outcome.path,
        outcome.branch,
    )
    return outcome


def finalize(worktree: Worktree, *, prune: bool = True) -> Outcome:
    """Inspect a finished lane's worktree, and remove it if it holds nothing."""
    outcome = Outcome(path=worktree.path, branch=worktree.branch)

    if not worktree.path or not os.path.isdir(worktree.path):
        # Nothing on disk to review. Reported as pruned because that is what
        # the parent needs to know: there is no directory to go and look at.
        outcome.pruned = True
        return outcome

    if not worktree.base_commit:
        # The commit count is unmeasurable, and the prune condition reads it.
        return _unproven(
            outcome, "no base commit was recorded", unmeasured="commits"
        )

    failures: list[str] = []
    unmeasured: list[str] = []
    try:
        counted = git(
            ["rev-list", "--count", f"{worktree.base_commit}..HEAD"], cwd=worktree.path
        )
        if counted.returncode == 0:
            outcome.commits = int(counted.stdout.strip() or 0)
        else:
            failures.append(f"rev-list exit {counted.returncode}: {counted.stderr.strip()[:200]}")
            unmeasured.append("commits")

        status = git(["status", "--porcelain"], cwd=worktree.path)
        if status.returncode == 0:
            outcome.dirty = bool(status.stdout.strip())
        else:
            failures.append(f"status exit {status.returncode}: {status.stderr.strip()[:200]}")
            unmeasured.append("dirty")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # A timeout, a git that vanished, or a rev-list that printed something
        # that is not a number. Which probe raised is not knowable here, so
        # neither value can be trusted.
        return _unproven(outcome, f"inspection raised: {exc}")

    if failures:
        return _unproven(outcome, "; ".join(failures), unmeasured=" and ".join(unmeasured))

    if prune and outcome.commits == 0 and not outcome.dirty:
        remove(worktree.root or worktree.path, worktree.path, worktree.branch, outcome)

    return outcome


def remove(root: str, path: str, branch: str, outcome: Outcome | None = None) -> bool:
    """Take away a worktree and its branch, in that order.

    The order is the invariant: deleting the branch first, then failing to
    remove the tree, orphans commits that were reachable a moment ago.
    """
    try:
        removed = git(["worktree", "remove", "--force", path], cwd=root)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("worktree: removal failed: %s", exc)
        return False

    if removed.returncode != 0:
        logger.debug("worktree: removal failed: %s", removed.stderr.strip())
        return False

    try:
        git(["branch", "-D", branch], cwd=root)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("worktree: branch delete failed: %s", exc)

    if outcome is not None:
        outcome.pruned = True
    logger.info("worktree removed: %s", path)
    return True


def brief_note(worktree: Worktree) -> str:
    """What the lane is told about where it is working."""
    return (
        f"\n## Your working copy\n"
        f"You are in an isolated git worktree at {worktree.path}, on branch "
        f"{worktree.branch}. Other lanes are working at the same time in their "
        f"own copies, which is why you have one.\n"
        f"Every edit and every command happens here — your tools cannot reach "
        f"the main checkout at all, so there is nothing to avoid. Commit what "
        f"you finish to your branch; whoever delegated this will read it. If "
        f"you commit nothing and leave the tree clean, this copy is discarded "
        f"when you are done.\n"
    )


# ---------------------------------------------------------------------------
# Reclaiming what accumulated
# ---------------------------------------------------------------------------

# Never deleted, at any age, in any mode.
PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "dev", "trunk"})


@dataclass
class Record:
    """One existing worktree, and what may be done with it."""

    name: str
    path: str
    branch: str
    verdict: str  # "reap" | "keep"
    reason: str
    commits: int = 0
    dirty: bool = False
    untracked: list[str] = field(default_factory=list)


def audit(root: str | Path) -> list[Record]:
    """Classify every worktree under `.worktrees/`.

    Everything fails safe toward `keep`. A probe that does not answer is not
    evidence of an empty tree, and this is the code path that deletes things.
    """
    base = Path(root) / WORKTREES_DIRNAME
    if not base.is_dir():
        return []

    records: list[Record] = []
    for path in sorted(p for p in base.iterdir() if p.is_dir()):
        records.append(_classify(root, path))
    return records


def _classify(root: str | Path, path: Path) -> Record:
    name = path.name
    branch = ""
    try:
        head = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        branch = head.stdout.strip() if head.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return Record(name, str(path), "", "keep", "could not read its branch")

    if not branch:
        return Record(name, str(path), "", "keep", "could not read its branch")

    try:
        status = git(["status", "--porcelain"], cwd=path)
    except (OSError, subprocess.SubprocessError):
        return Record(name, str(path), branch, "keep", "could not read its status")
    if status.returncode != 0:
        return Record(name, str(path), branch, "keep", "could not read its status")

    lines = [line for line in status.stdout.splitlines() if line.strip()]
    tracked = [line for line in lines if not line.startswith("??")]
    untracked = [line[3:] for line in lines if line.startswith("??")]

    if tracked:
        return Record(
            name,
            str(path),
            branch,
            "keep",
            "uncommitted changes to tracked files",
            dirty=True,
            untracked=untracked,
        )

    # Commits that exist nowhere else. `git cherry` answers by patch, so a
    # branch whose work was rebased or squashed onto the main line still reads
    # as merged rather than as unique.
    try:
        unique = git(["cherry", "HEAD@{upstream}", branch], cwd=path)
        if unique.returncode != 0:
            unique = git(["cherry", "HEAD", branch], cwd=root)
    except (OSError, subprocess.SubprocessError):
        return Record(name, str(path), branch, "keep", "could not compare its commits")

    ahead = [line for line in unique.stdout.splitlines() if line.startswith("+")]
    if unique.returncode != 0:
        return Record(name, str(path), branch, "keep", "could not compare its commits")
    if ahead:
        return Record(
            name,
            str(path),
            branch,
            "keep",
            f"{len(ahead)} commit(s) that exist nowhere else",
            commits=len(ahead),
            untracked=untracked,
        )

    if untracked:
        return Record(
            name,
            str(path),
            branch,
            "keep",
            f"{len(untracked)} untracked file(s) — nothing else to keep, but "
            f"nobody else has them either",
            untracked=untracked,
        )

    return Record(name, str(path), branch, "reap", "clean, and nothing unmerged")


def reclaim(root: str | Path, *, dry_run: bool = False) -> tuple[list[str], list[Record]]:
    """Remove the worktrees `audit` found nothing in.

    Returns (what was done, what was kept). Kept records carry their reason so
    the caller can say why — "nothing to reclaim" without saying what is being
    held on to is the answer that makes people delete the directory by hand.
    """
    actions: list[str] = []
    kept: list[Record] = []

    for record in audit(root):
        if record.verdict != "reap":
            kept.append(record)
            continue
        if dry_run:
            actions.append(f"would remove {record.name} ({record.branch})")
            continue
        if remove(str(root), record.path, record.branch):
            actions.append(f"removed {record.name} ({record.branch})")
        else:
            kept.append(
                Record(record.name, record.path, record.branch, "keep", "removal failed")
            )

    return actions, kept
