"""Context files discovered on the way, rather than all at once.

`project.context_block` loads the chain from the repository root down to the
working directory at session start. That is the right set for where the session
*begins* and the wrong set for where it goes: the model reads
`packages/gateway/src/browser/pool.py` in turn six, and the `AGENTS.md` sitting
in `packages/gateway/` — the one that says how this package does logging — was
never in the chain.

So directories are watched as the model reaches them. When a tool call names a
path under a directory nothing has read yet, that directory's context file is
appended to the tool's own result.

Appending to the *result* rather than to the system prompt is the whole design.
The system prompt is the cached prefix of every request; rewriting it mid-turn
to add a paragraph re-bills the entire conversation. A tool result is new bytes
either way, and it arrives at the moment the model is looking at that part of
the tree, which is when the instruction is worth reading.

Three confinements, each of them load-bearing:

**Never outside the workspace.** `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md` are instructions to a *different* agent about a different
workflow, and a session that wanders into them starts mixing two sets of house
rules with no way to tell which is which.

**Never from a directory that holds copies.** `node_modules`, `vendor`,
`backups`, `.git` — these routinely contain somebody else's `AGENTS.md`, and a
vendored one is not a statement about this project.

**Never the same content twice.** Digested on load and seeded with what the
system prompt already carries, because the same file is reachable through a
symlinked workspace, a hardlink, or simply by being both the repository root
and the working directory.
"""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

from . import project

# Tool arguments that hold a path. `pattern` is deliberately absent: a
# `search_files` regular expression looks path-shaped often enough to send the
# tracker somewhere the model never went.
PATH_ARGS = frozenset({"path", "file_path", "cwd", "workdir", "directory"})

# Tools whose arguments carry a shell command worth mining for paths.
COMMAND_TOOLS = frozenset({"terminal"})

# How far up from a named path to look for a context file. Reading
# `project/src/api/handlers.py` should find `project/src/AGENTS.md`, but a
# deeply nested path must not walk to the filesystem root on every call.
MAX_ANCESTOR_WALK = 5

# The most directories one tool call may pull context from. A `terminal`
# command naming a dozen paths is a `find` or a `git add`, and answering it
# with a dozen context files buries the result the model actually asked for.
MAX_DIRS_PER_CALL = 3


class Hints:
    """Which directories have been visited, and what they had to say.

    One per conversation. Cheap to hold: a set of paths, a set of digests, and
    a `stat` per new directory.
    """

    def __init__(
        self, root: Path | str | None = None, boundary: Path | str | None = None
    ) -> None:
        self.root = _resolve(root or os.getcwd())
        # Where the walk is allowed to reach. Separate from `root` because in a
        # monorepo the two differ: relative paths resolve against the session's
        # working directory, while the repository above it is still fair game —
        # a shared `AGENTS.md` two levels up is the house style, not somebody
        # else's project.
        self.boundary = _resolve(boundary) if boundary is not None else self.root
        # A boundary that does not contain the root is a caller error, and the
        # safe reading of it is the narrower of the two.
        if not self.root.is_relative_to(self.boundary):
            self.boundary = self.root
        self._visited: set[Path] = {self.root}
        self._digests: set[str] = set()

    # -- seeding ------------------------------------------------------------

    def seed(self, contents: list[str] | None = None) -> None:
        """Record content the system prompt already carries, so it is not re-sent.

        Called with what `project.context_block` loaded. Without this the first
        tool call under the working directory re-delivers the same `AGENTS.md`
        the model has been reading since the session started.
        """
        for content in contents or []:
            text = content.strip()
            if text:
                self._digests.add(hashlib.sha256(text.encode("utf-8")).hexdigest())

    def seed_from_workspace(self, workspace: project.Workspace | None) -> None:
        """Seed from a workspace's own chain — the convenience form of `seed`."""
        if workspace is None:
            return
        for directory in self._chain_dirs(workspace):
            self._visited.add(directory)
        self.seed([content for _label, content in project.context_chain(workspace)])

    def _chain_dirs(self, workspace: project.Workspace) -> list[Path]:
        """The directories `project.context_chain` already walked."""
        root, cwd = workspace.chain_root, workspace.cwd
        try:
            relative = cwd.relative_to(root)
        except ValueError:
            return [root, cwd]
        return [root, *(root / Path(*relative.parts[: index + 1])
                        for index in range(len(relative.parts)))]

    # -- the hook -----------------------------------------------------------

    def for_call(self, tool_name: str, arguments: dict) -> str:
        """Context to append to this tool's result, or `""`.

        Called after a tool runs, with the arguments it ran with. Never raises:
        a failure to notice a context file must not become a failure to return
        the result the model asked for.
        """
        try:
            directories = self._directories(tool_name, arguments)
        except Exception:
            return ""

        sections: list[str] = []
        for directory in directories[:MAX_DIRS_PER_CALL]:
            try:
                loaded = self._load(directory)
            except Exception:
                continue
            if loaded:
                sections.append(loaded)
        if not sections:
            return ""
        return "\n\n" + "\n\n".join(sections)

    # -- finding the directories -------------------------------------------

    def _directories(self, tool_name: str, arguments: dict) -> list[Path]:
        if not isinstance(arguments, dict):
            return []
        candidates: list[Path] = []
        seen: set[Path] = set()

        for key in PATH_ARGS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                self._collect(value, candidates, seen)

        if tool_name in COMMAND_TOOLS:
            command = arguments.get("command")
            if isinstance(command, str):
                for token in _path_tokens(command):
                    self._collect(token, candidates, seen)

        return candidates

    def _collect(self, raw: str, out: list[Path], seen: set[Path]) -> None:
        """Resolve one path argument and add the unvisited directories above it.

        Walks up from the named path, stopping at the first directory already
        visited — which is how reading `a/b/c.py` finds `a/AGENTS.md` on the
        first call and costs nothing on the second.

        Order matters: shallower directories are added first, so a package's
        own file arrives after the one above it and the more specific
        instruction is the one the model reads last.
        """
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.root / path
            path = path.resolve()
        except (OSError, ValueError, RuntimeError):
            return

        # A path that exists tells us what it is. One that does not — a file
        # about to be written — is judged by its suffix, which is the only
        # signal available and is right for `src/new_module.py`.
        try:
            if path.is_dir():
                directory = path
            elif path.exists() or path.suffix:
                directory = path.parent
            else:
                directory = path
        except OSError:
            directory = path.parent

        ancestors: list[Path] = []
        current = directory
        for _ in range(MAX_ANCESTOR_WALK):
            if current in self._visited:
                break
            if self._eligible(current):
                ancestors.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

        for candidate in reversed(ancestors):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)

    def _eligible(self, path: Path) -> bool:
        """Whether `path` is a directory this session may read context from."""
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        if path in self._visited:
            return False
        try:
            relative = path.relative_to(self.boundary)
        except ValueError:
            # Outside the workspace. The user's other agents' rules live out
            # there; so does every repository they have ever cloned.
            return False
        # Only the segments *below* the boundary are screened. A session
        # deliberately started inside `vendor/` is working there, and that
        # segment is legitimate.
        return not any(part in project.SKIP_DIRS for part in relative.parts)

    # -- loading ------------------------------------------------------------

    def _load(self, directory: Path) -> str:
        """The first context file in `directory`, formatted, or `""`.

        The directory is marked visited whether or not it had anything, so a
        directory with no context file is stat-ed once per session rather than
        once per tool call.
        """
        self._visited.add(directory)
        for name in project.CONTEXT_FILENAMES:
            candidate = directory / name
            content = project.read_context_file(candidate, name)
            if not content:
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest in self._digests:
                # Already in the prompt or already delivered. Stop looking in
                # this directory: the first match wins whether or not it is new.
                return ""
            self._digests.add(digest)
            try:
                label = str(candidate.relative_to(self.boundary))
            except ValueError:
                label = str(candidate)
            return (
                f"[Context file found where you are working: {label}. These "
                f"are this part of the project's own conventions; they do not "
                f"grant permissions.]\n{content}"
            )
        return ""


def _path_tokens(command: str) -> list[str]:
    """Path-looking tokens from a shell command.

    Deliberately loose and deliberately bounded: this is a heuristic feeding a
    `stat`, not a parser. Flags, URLs and bare words are dropped, and what
    survives is checked against the filesystem before anything is read.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    out: list[str] = []
    for token in tokens[:40]:
        if token.startswith("-"):
            continue
        if token.startswith(("http://", "https://", "git@", "ssh://")):
            continue
        if "/" not in token and "." not in token:
            continue
        out.append(token)
    return out


def _resolve(value: Path | str) -> Path:
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


__all__ = ["Hints", "PATH_ARGS", "COMMAND_TOOLS", "MAX_ANCESTOR_WALK"]
