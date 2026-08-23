"""Where the agent is allowed to reach.

The hosted runtime never had a user filesystem to protect; this harness does,
so confinement is a property of the harness rather than something inherited.

The rule is one sentence: **paths under the workspace root are ordinary, paths
outside it are not reachable at all.** Not "reachable with approval" — a model
that can ask for `~/.ssh/id_rsa` will eventually ask for it at a moment the
user is clicking through prompts. Widening the root is a deliberate act taken
by the person, at the command line, before the session starts.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathOutsideWorkspace(Exception):
    def __init__(self, path: Path, root: Path) -> None:
        super().__init__(
            f"{path} is outside the workspace root ({root}). "
            "Start Andromeda from that directory, or pass --workspace."
        )
        self.path = path
        self.root = root


class Workspace:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        # `resolve()` and not `absolute()`: the check has to happen on the real
        # path, or a symlink inside the root pointing at /etc walks straight out.
        self.root = Path(root or Path.cwd()).expanduser().resolve()

    def resolve(self, candidate: str | os.PathLike[str]) -> Path:
        """Resolve `candidate` inside the workspace, or refuse.

        Relative paths are taken against the root, never against the process
        cwd — those diverge the moment a tool changes directory, and the
        difference is exactly where a confinement check stops meaning anything.
        """
        raw = Path(candidate).expanduser()
        target = raw if raw.is_absolute() else self.root / raw

        # A path that does not exist yet still has to be checked — that is the
        # `write_file` case. `resolve(strict=False)` normalizes `..` without
        # requiring the leaf to exist.
        resolved = target.resolve()

        if resolved != self.root and self.root not in resolved.parents:
            raise PathOutsideWorkspace(resolved, self.root)
        return resolved

    def relative(self, path: Path) -> str:
        """Display form. Absolute paths in transcripts are noise and leak $HOME."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def __repr__(self) -> str:
        return f"Workspace({self.root})"
