"""A plugin with no code in it.

The ordinary plugin is Python: `plugin.yaml` plus an `__init__.py` this process
imports. This is the other shape — a `plugin.json` describing **skills and MCP
servers**, and nothing that runs in-process at all.

    my-package/
    ├── plugin.json               name, version, and what it carries
    ├── skills/
    │   └── deploy/SKILL.md       instructions the model may load
    └── mcp.json                  servers to connect to

Why have both
-------------
Because "it has no code" is a real difference and it should be visible. A
Python plugin can `import os` and ignore every gate; a portable package cannot,
because nothing in it is ever executed by this process. Its skills are text the
model may read and its MCP servers are subprocesses or URLs that go through the
ordinary MCP path, tool gate and all.

So a portable package **declares no capabilities and is refused if it tries**.
There is nothing for one to open: every registration point a capability guards
is a Python call, and there is no Python here. `plugins list` labels it, and
`plugins install` says so before asking anything.

This is the interchange format from `agent-plugins.org`, so a package written
for another harness loads here unchanged. Fields this build does not understand
are reported rather than dropped — a package targeting a newer schema still
installs, and `plugins show` names what was not read.

Placeholders
------------
`${PLUGIN_ROOT}` and `${PLUGIN_DATA}` expand in MCP `command`, `args`, `env`
and `cwd`. They are the only two, and both resolve to directories this harness
owns — a package cannot point them at an arbitrary path, and a server whose
resolved `cwd` lands outside its own root is refused.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "plugin.json"
MCP_FILENAME = "mcp.json"

SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

#: The interchange spec's name rule: lowercase, no leading or trailing
#: separator, and no `--` or `..` anywhere — the second half of which is what
#: keeps a name from being a path traversal.
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*$")
PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")

KNOWN_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)

MAX_DESCRIPTION = 1024


class PortableError(ValueError):
    """A portable package that cannot be read."""


@dataclass(frozen=True)
class Note:
    """Something wrong with one part, that did not stop the rest.

    Kept and shown rather than logged and forgotten: a package whose one broken
    skill was silently skipped is a package whose author is about to file a bug
    about a skill that "does not appear".
    """

    scope: str
    message: str


@dataclass(frozen=True)
class PortableSkill:
    name: str
    description: str
    path: Path


@dataclass
class PortablePackage:
    name: str
    version: str
    description: str
    root: Path
    skills: list[PortableSkill] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    unknown_fields: tuple[str, ...] = ()
    author: str = ""
    homepage: str = ""
    license: str = ""
    keywords: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.skills and not self.mcp_servers


def is_portable(directory: Path) -> bool:
    return (Path(directory) / MANIFEST_FILENAME).is_file()


def _inside(path: Path, root: Path) -> bool:
    """Whether `path` really resolves within `root`.

    Resolved, not string-compared, because a symlink out of the tree is exactly
    how a package would have the scan read one file and the loader read another.
    """
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def load(directory: Path) -> PortablePackage:
    """Read a portable package. Raises `PortableError` only on the fatal parts.

    Fatal is a short list: no manifest, unreadable JSON, no usable name. A
    broken skill or a malformed server is a note — one bad entry must not cost
    the package, because the parts are independent and the author can see which
    one failed.
    """
    root = Path(directory)
    manifest_path = root / MANIFEST_FILENAME

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PortableError(f"{manifest_path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise PortableError(f"{manifest_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise PortableError(f"{manifest_path} could not be read: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise PortableError(f"{manifest_path} must contain an object")

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PortableError(
            f"{manifest_path}: `name` must be lowercase letters, digits, '.' or "
            f"'-', without a leading or trailing separator and without '--' or "
            f"'..'. Got {name!r}."
        )

    notes: list[Note] = []

    if "capabilities" in raw:
        # Refused rather than ignored. A package that asks for one has
        # misunderstood what it is, and letting the field sit there unread
        # would let its author keep believing it works.
        raise PortableError(
            f"{manifest_path}: a portable package declares no capabilities and "
            f"cannot hold one — there is no code in it for a capability to "
            f"govern. Write a Python plugin ({name}/plugin.yaml and "
            f"__init__.py) if it needs one."
        )

    description = raw.get("description")
    if not isinstance(description, str):
        description = ""
    elif len(description) > MAX_DESCRIPTION:
        notes.append(Note("manifest", f"description truncated at {MAX_DESCRIPTION}"))
        description = description[:MAX_DESCRIPTION]

    schema = raw.get("$schema")
    if isinstance(schema, str) and schema and schema != SCHEMA_V1:
        notes.append(
            Note("manifest", f"written against {schema}; read as {SCHEMA_V1}")
        )

    package = PortablePackage(
        name=name,
        version=str(raw.get("version") or "0.0.0"),
        description=description,
        root=root,
        notes=notes,
        unknown_fields=tuple(
            sorted(key for key in raw if isinstance(key, str) and key not in KNOWN_FIELDS)
        ),
        author=_author(raw.get("author")),
        homepage=str(raw.get("homepage") or ""),
        license=str(raw.get("license") or ""),
        keywords=tuple(
            str(item) for item in (raw.get("keywords") or []) if isinstance(item, str)
        ),
    )

    package.skills = _skills(root, notes)
    package.mcp_servers = _mcp_servers(root, name, notes)
    return package


def _author(value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(
            str(value[key]) for key in ("name", "email", "url") if value.get(key)
        )
    return "" if value is None else str(value).strip()


def _skills(root: Path, notes: list[Note]) -> list[PortableSkill]:
    """Every `skills/<name>/SKILL.md`, validated against the interchange spec."""
    skills_root = root / "skills"
    if not skills_root.exists():
        return []
    if not skills_root.is_dir() or not _inside(skills_root, root):
        notes.append(Note("skills", "skills/ must be a directory inside the package"))
        return []

    found: list[PortableSkill] = []
    try:
        children = sorted(skills_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        notes.append(Note("skills", f"cannot list skills: {exc}"))
        return []

    for child in children:
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.exists():
            continue
        scope = f"skill:{child.name}"

        if not _inside(skill_md, root) or not skill_md.is_file():
            notes.append(Note(scope, "SKILL.md must be a regular file inside the package"))
            continue
        if not SKILL_NAME_RE.fullmatch(child.name):
            notes.append(Note(scope, "the directory name is not a usable skill name"))
            continue

        frontmatter = _frontmatter(skill_md, scope, notes)
        if frontmatter is None:
            continue

        declared = frontmatter.get("name")
        if declared != child.name:
            # The spec requires them to match, and the reason is addressing:
            # the directory is how the skill is reached, so a manifest naming
            # something else names a skill nobody can load.
            notes.append(
                Note(scope, f"`name: {declared!r}` does not match the directory")
            )
            continue

        summary = frontmatter.get("description")
        if not isinstance(summary, str) or not summary.strip():
            notes.append(Note(scope, "a skill needs a non-empty `description`"))
            continue

        found.append(
            PortableSkill(
                name=child.name,
                description=summary.strip()[:MAX_DESCRIPTION],
                path=skill_md,
            )
        )
    return found


def _frontmatter(path: Path, scope: str, notes: list[Note]) -> dict[str, Any] | None:
    import yaml

    try:
        content = path.read_text(encoding="utf-8").lstrip("﻿")
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(Note(scope, f"cannot read SKILL.md: {exc}"))
        return None

    if not content.startswith("---"):
        notes.append(Note(scope, "SKILL.md has no YAML frontmatter"))
        return None
    closing = re.search(r"\n---\s*\n", content[3:])
    if closing is None:
        notes.append(Note(scope, "SKILL.md has unterminated frontmatter"))
        return None

    try:
        parsed = yaml.safe_load(content[3 : closing.start() + 3])
    except yaml.YAMLError as exc:
        notes.append(Note(scope, f"invalid frontmatter: {exc}"))
        return None
    if not isinstance(parsed, dict):
        notes.append(Note(scope, "frontmatter must be an object"))
        return None
    return parsed


def _mcp_servers(
    root: Path, package_name: str, notes: list[Note]
) -> dict[str, dict[str, Any]]:
    """Servers from `mcp.json`, namespaced and with placeholders expanded."""
    path = root / MCP_FILENAME
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        notes.append(Note("mcp", f"cannot read {MCP_FILENAME}: {exc}"))
        return {}

    servers = raw.get("mcpServers") if isinstance(raw, Mapping) else None
    if not isinstance(servers, Mapping):
        notes.append(Note("mcp", f"{MCP_FILENAME} has no `mcpServers` object"))
        return {}

    data_root = _data_root(package_name)
    built: dict[str, dict[str, Any]] = {}

    for server_name, config in servers.items():
        scope = f"mcp:{server_name}"
        if not isinstance(server_name, str) or not server_name.strip():
            notes.append(Note("mcp", "a server with no name was skipped"))
            continue
        if not isinstance(config, Mapping):
            notes.append(Note(scope, "the server entry is not an object"))
            continue

        expanded = _expand(dict(config), root, data_root)

        cwd = expanded.get("cwd")
        if isinstance(cwd, str) and cwd and not _inside(Path(cwd), root):
            notes.append(
                Note(scope, "`cwd` resolves outside the package and was dropped")
            )
            expanded.pop("cwd", None)

        # Namespaced by the package, so two packages carrying a server called
        # `github` do not silently become one, and so a portable server can
        # never shadow one the user configured themselves.
        built[f"{package_name}:{server_name}"] = expanded

    return built


def _data_root(package_name: str) -> Path:
    from andromeda_cli import config as config_module

    return config_module.home() / "plugin-data" / package_name


def _expand(value: Any, root: Path, data_root: Path) -> Any:
    """Replace `${PLUGIN_ROOT}` and `${PLUGIN_DATA}`, recursively."""
    replacements = {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data_root)}
    if isinstance(value, str):
        return PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], value)
    if isinstance(value, list):
        return [_expand(item, root, data_root) for item in value]
    if isinstance(value, Mapping):
        return {key: _expand(item, root, data_root) for key, item in value.items()}
    return value
