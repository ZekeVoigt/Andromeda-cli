"""Which language server handles which file, and whether it is on this machine.

The one decision that shapes this file: **nothing is ever installed.** Upstream
downloads a server the first time the agent touches a matching file — four
hundred lines of npm, `go install` and version pinning, running as a side
effect of the model editing a `.py`. That is the exact shape this codebase
refuses everywhere else: an agent may propose, only a person grants. So a
missing server is *named*, with the one command that would install it, and the
edit proceeds without diagnostics.

The cost of that decision is real and worth stating: a fresh machine gets no
diagnostics until somebody installs a server. The benefit is that `andromeda`
never puts a package on a machine nobody asked it to, and `andromeda lsp
status` can answer "what would I use here" without side effects.

Servers are found on `PATH`, plus the two places a project keeps its own —
`node_modules/.bin` for the JavaScript family and the active virtualenv for
Python — because a repository that pins its own toolchain means the pinned one.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Directories never worth walking for a language census: dependencies, build
# output, version-control internals. Kept here rather than imported from
# `project`, so the LSP package does not depend on the prompt package.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", "target", ".next", ".turbo", "vendor", "third_party",
    ".cache", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages", "dist-packages",
})

# LSP wants a `languageId` per document. The mapping is a protocol constant,
# not a preference: a server given the wrong one silently produces nothing.
LANGUAGE_IDS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".rb": "ruby",
    ".sh": "shellscript", ".bash": "shellscript", ".zsh": "shellscript",
    ".lua": "lua",
    ".php": "php",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".ex": "elixir", ".exs": "elixir",
    ".zig": "zig",
    ".nix": "nix",
    ".tf": "terraform", ".tfvars": "terraform",
    ".dart": "dart",
    ".hs": "haskell",
    ".vue": "vue",
    ".svelte": "svelte",
}


@dataclass(frozen=True)
class Server:
    """One language server: what it handles, how to run it, how to get it."""

    id: str
    # Executables that provide this server, in preference order. The first one
    # found wins, so a project-local `node_modules/.bin/tsserver` beats a global.
    binaries: tuple[str, ...]
    args: tuple[str, ...]
    extensions: frozenset[str]
    # Filenames that mark this server's own project root, nearest-first. A
    # Python server rooted at a monorepo's top sees none of the package's
    # configuration; one rooted at the package sees all of it.
    roots: tuple[str, ...]
    install: str
    label: str
    # Some servers publish nothing until a file is opened *and* something else
    # nudges them. Recorded rather than worked around: it decides whether an
    # empty first push means "clean" or "not ready yet".
    slow_first_push: bool = False
    settings: dict = field(default_factory=dict)

    def handles(self, path: Path | str) -> bool:
        return Path(path).suffix.lower() in self.extensions


# Ordered. The first entry that handles an extension wins, so a more specific
# server (a `.vue` server) has to come before a general one that also claims it.
SERVERS: tuple[Server, ...] = (
    Server(
        id="pyright",
        binaries=("pyright-langserver", "basedpyright-langserver"),
        args=("--stdio",),
        extensions=frozenset({".py", ".pyi"}),
        roots=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pyrightconfig.json"),
        install="npm install -g pyright",
        label="Python — pyright",
    ),
    Server(
        id="ruff",
        binaries=("ruff",),
        args=("server",),
        extensions=frozenset({".py", ".pyi"}),
        roots=("pyproject.toml", "ruff.toml", ".ruff.toml"),
        install="pip install ruff  (or: uv tool install ruff)",
        label="Python — ruff",
    ),
    Server(
        id="vue",
        binaries=("vue-language-server",),
        args=("--stdio",),
        extensions=frozenset({".vue"}),
        roots=("package.json", "tsconfig.json"),
        install="npm install -g @vue/language-server",
        label="Vue — @vue/language-server",
    ),
    Server(
        id="svelte",
        binaries=("svelteserver",),
        args=("--stdio",),
        extensions=frozenset({".svelte"}),
        roots=("package.json", "svelte.config.js"),
        install="npm install -g svelte-language-server",
        label="Svelte — svelte-language-server",
    ),
    Server(
        id="typescript",
        binaries=("typescript-language-server",),
        args=("--stdio",),
        extensions=frozenset({
            ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"
        }),
        roots=("tsconfig.json", "jsconfig.json", "package.json"),
        install="npm install -g typescript-language-server typescript",
        label="TypeScript / JavaScript — typescript-language-server",
        slow_first_push=True,
    ),
    Server(
        id="gopls",
        binaries=("gopls",),
        args=(),
        extensions=frozenset({".go"}),
        roots=("go.mod", "go.work"),
        install="go install golang.org/x/tools/gopls@latest",
        label="Go — gopls",
        slow_first_push=True,
    ),
    Server(
        id="rust-analyzer",
        binaries=("rust-analyzer",),
        args=(),
        extensions=frozenset({".rs"}),
        roots=("Cargo.toml",),
        install="rustup component add rust-analyzer",
        label="Rust — rust-analyzer",
        slow_first_push=True,
    ),
    Server(
        id="clangd",
        binaries=("clangd",),
        args=("--background-index",),
        extensions=frozenset({".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}),
        roots=("compile_commands.json", "CMakeLists.txt", "Makefile"),
        install="your platform's LLVM package (brew install llvm, apt install clangd)",
        label="C / C++ — clangd",
        slow_first_push=True,
    ),
    Server(
        id="bash",
        binaries=("bash-language-server",),
        args=("start",),
        extensions=frozenset({".sh", ".bash", ".zsh"}),
        roots=(),
        install="npm install -g bash-language-server",
        label="Shell — bash-language-server",
    ),
    Server(
        id="lua",
        binaries=("lua-language-server",),
        args=(),
        extensions=frozenset({".lua"}),
        roots=(".luarc.json",),
        install="brew install lua-language-server",
        label="Lua — lua-language-server",
    ),
    Server(
        id="terraform",
        binaries=("terraform-ls",),
        args=("serve",),
        extensions=frozenset({".tf", ".tfvars"}),
        roots=(),
        install="brew install hashicorp/tap/terraform-ls",
        label="Terraform — terraform-ls",
    ),
    Server(
        id="zig",
        binaries=("zls",),
        args=(),
        extensions=frozenset({".zig"}),
        roots=("build.zig",),
        install="your platform's zls package",
        label="Zig — zls",
    ),
    Server(
        id="nix",
        binaries=("nil", "nixd"),
        args=(),
        extensions=frozenset({".nix"}),
        roots=("flake.nix",),
        install="nix profile install nixpkgs#nil",
        label="Nix — nil",
    ),
)


# Where a project keeps its own copy of a server, relative to a root. Checked
# before `PATH`, because a repository that pins its toolchain means the pinned
# one — a global `typescript-language-server` two major versions ahead reports
# errors the project's own build does not have.
_LOCAL_BIN_DIRS = (
    Path("node_modules") / ".bin",
    Path(".venv") / "bin",
    Path("venv") / "bin",
    Path(".venv") / "Scripts",
)


def find_binary(server: Server, root: Path | None = None) -> str | None:
    """The executable for `server`, or `None` if this machine does not have it."""
    if root is not None:
        for directory in _LOCAL_BIN_DIRS:
            for name in server.binaries:
                candidate = root / directory / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    for name in server.binaries:
        found = shutil.which(name)
        if found:
            return found
    return None


def for_file(path: Path | str) -> list[Server]:
    """Every server that handles this file, in preference order.

    A list rather than one server: `.py` is handled by both a type checker and
    a linter, and they answer different questions. The service starts whichever
    of them is actually installed.
    """
    return [server for server in SERVERS if server.handles(path)]


def language_id(path: Path | str) -> str:
    """The protocol's name for this file's language, or `"plaintext"`.

    `plaintext` rather than a guess: a server handed a `languageId` it does not
    recognise usually says nothing at all, and silence that looks like "no
    problems" is the worst answer this layer can give.
    """
    return LANGUAGE_IDS.get(Path(path).suffix.lower(), "plaintext")


def project_root(path: Path, server: Server, workspace: Path) -> Path:
    """The root to start `server` in for `path`: nearest marker, else `workspace`.

    Never above `workspace`. A server rooted outside the workspace indexes the
    user's whole home directory the first time the model reads a file — which
    is slow, surprising, and reads code the session was never pointed at.
    """
    try:
        current = path.resolve().parent
        limit = workspace.resolve()
    except OSError:
        return workspace
    if not current.is_relative_to(limit):
        return workspace
    while True:
        for marker in server.roots:
            if (current / marker).exists():
                return current
        if current == limit or current.parent == current:
            return limit
        current = current.parent


@dataclass(frozen=True)
class Availability:
    """What `andromeda lsp status` reports for one server."""

    server: Server
    binary: str | None

    @property
    def available(self) -> bool:
        return self.binary is not None


def survey(root: Path | None = None) -> list[Availability]:
    """Every known server and whether this machine has it. Pure `stat` and `PATH`."""
    return [Availability(server, find_binary(server, root)) for server in SERVERS]


# A project reveals which languages it is in within the first few thousand
# files. A full walk of a monorepo to draw a status table is not a trade worth
# making, and the answer does not change after the first `.py`.
_EXTENSION_SCAN_LIMIT = 4000


def project_extensions(root: Path, limit: int = _EXTENSION_SCAN_LIMIT) -> set[str]:
    """File extensions present under `root`, from a bounded walk.

    Lets a status screen say "these servers apply *here*" rather than listing
    every server the harness knows — a Python project told about `rust-analyzer`
    learns nothing and stops reading.
    """
    found: set[str] = set()
    seen = 0
    stack = [root]
    while stack and seen < limit:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen >= limit:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix:
                                found.add(suffix)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def relevant(root: Path) -> list[Availability]:
    """The servers that would actually be used in this project."""
    extensions = project_extensions(root)
    return [entry for entry in survey(root) if entry.server.extensions & extensions]


__all__ = [
    "Availability",
    "SKIP_DIRS",
    "LANGUAGE_IDS",
    "SERVERS",
    "Server",
    "find_binary",
    "for_file",
    "language_id",
    "project_extensions",
    "project_root",
    "relevant",
    "survey",
]
