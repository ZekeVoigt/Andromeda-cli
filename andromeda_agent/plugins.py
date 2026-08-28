"""The plugin socket.

A plugin is a directory containing `plugin.yaml` and an `__init__.py` that
defines `register(ctx)`. On start, Andromeda finds those directories, imports
the enabled ones, and hands each a `PluginContext` — the only supported way for
outside code to add to this harness.

Why a socket at all
-------------------
Everything extensible here today is extensible by *editing this repository*:
memory backends are a closed two-name list, cron providers are a closed
two-name list, the provider seam has three files in it, and a new tool is a new
entry in `registry.py`. That is fine for one author and impossible for anyone
else. The socket costs about the same as one more backend and ends the pattern.

What a plugin can take over
---------------------------
Registration points, each landing on a seam that already existed::

    add                     replace
    ───                     ───────
    register_tool           register_memory_backend    (memory.backend)
    register_hook           register_cron_provider     (cron.provider)
    register_command        register_model_provider    (model.provider)
    register_cli_command    register_secret_source     (secrets.source)
    register_skill          register_tool(override=)   (tools.override)
    register_delivery       register_command(override=)(commands.override)
    register_redaction_patterns
    register_system_prompt_section                     (prompt.inject)

The parenthesised names are capabilities: seams that are refused until the user
grants them. See `plugin_capabilities`, which also states plainly that none of
this is a sandbox.

Hooks, not middleware
---------------------
Harnesses in this space tend to have both: dozens of hooks *and* a separate
four-kind middleware vocabulary that can rewrite a request or wrap the
execution callback. This build has hooks only,
and that is a decision rather than a shortfall. `hooks.py` opens with the rule
that governs it — "there is no inert vocabulary registered ahead of the code
that fires it" — and the two middleware kinds that would carry real weight are
already reachable:

    tool_request   → `pre_tool_call` returning {"action": "modify", ...}
    llm_request    → `pre_llm_call` returning context for this turn

`tool_execution` and `llm_execution` (wrap-the-callback) have no fire site, so
they are not offered. Minting the names first and finding sites for them later
is how a hook that looks configurable ends up never firing.

Load order and failure
----------------------
`requires_plugins` makes the order a real topological sort: if A needs B, B's
`register()` runs first. A cycle is warned about and falls back to alphabetical
rather than refusing to start. A missing dependency is a warning, not a
failure — `ctx.has_plugin` is how a plugin checks at runtime.

A plugin whose import or `register()` raises is skipped, loudly, and every
other plugin still loads. One broken third-party package must not be the reason
the agent will not start.
"""

from __future__ import annotations

import copy
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from . import hooks as hooks_module
from . import plugin_capabilities as caps
from . import plugin_store
from . import portable

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "plugin.yaml"
ENTRY_POINT_GROUP = "andromeda_cli.plugins"

#: Set to enable discovery of `./.andromeda/plugins/`. Off by default because a
#: repository you cloned could otherwise drop Python into your agent's process
#: the moment you `cd` into it — and unlike a skill, a plugin is not read by the
#: model, it is imported by the interpreter.
ENV_PROJECT_PLUGINS = "ANDROMEDA_ENABLE_PROJECT_PLUGINS"
#: Escape hatch. `andromeda --no-plugins` sets it; so can a user whose install
#: will not start. Named for what it does, not for a bug it works around.
ENV_DISABLE = "ANDROMEDA_NO_PLUGINS"
#: Where the bundled plugin tree is, when a packaged install moved it.
ENV_BUNDLED = "ANDROMEDA_BUNDLED_PLUGINS_DIR"

#: How far up from this file to look for the bundled `plugins/` directory.
#: The package is the tree root in a distribution checkout and one directory
#: down in the monorepo, so the answer is never more than a few levels up.
#: Getting this wrong is not theoretical: the bundled *skills* shipped
#: unreachable to every installed user for exactly this reason.
MAX_WALK_UP = 4

SUPPORTED_API_VERSION = 1

#: A plugin id: what the ledger keys on and what namespaces its events, its
#: state directory and its skills. Deliberately narrow — it becomes a path
#: segment and an event prefix.
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

VALID_KINDS: frozenset[str] = frozenset(
    {"standalone", "backend", "provider", "tool"}
)

#: Fields a manifest may carry. Anything else is reported by `plugins doctor`
#: as a probable typo rather than ignored — `capabilties:` silently doing
#: nothing is the failure this list exists to prevent.
KNOWN_MANIFEST_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "license",
        "homepage",
        "tags",
        "kind",
        "api_version",
        "capabilities",
        "requires_env",
        "optional_env",
        "requires_plugins",
        "python_dependencies",
        "config_schema",
        "provides_tools",
        "provides_hooks",
    }
)

# --- limits ---------------------------------------------------------------
# Every one of these is a bound on what a third party can do to a shared
# resource. They are constants rather than settings on purpose: a limit the
# plugin's own config can raise is not a limit.

#: Per-plugin state file ceiling.
STATE_QUOTA_BYTES = 10 * 1024 * 1024
STATE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

#: System-prompt injection. The prompt is the cached prefix of every request,
#: so an unbounded section is an unbounded per-turn cost on someone else's
#: money. Three limits because one is not enough: a single huge section, many
#: small ones, and the total all have to be refused.
MAX_PROMPT_SECTIONS = 32
MAX_PROMPT_SECTION_CHARS = 4_000
MAX_PROMPT_SECTIONS_TOTAL_CHARS = 8_000
PROMPT_SECTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROMPT_SECTIONS_START = "<!-- andromeda-plugin-sections:start -->"
PROMPT_SECTIONS_END = "<!-- andromeda-plugin-sections:end -->"

#: Event bus. Depth stops a plugin that emits from its own subscriber; the
#: reserved prefix stops one from impersonating the host.
MAX_EVENT_DEPTH = 8
CORE_EVENT_NAMESPACE = "andromeda"


class PluginError(RuntimeError):
    """A plugin could not be loaded, or asked for something it may not have."""


class ToolOverrideError(caps.CapabilityError):
    """A plugin tried to replace a built-in tool without the capability."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginManifest:
    """What `plugin.yaml` said, normalised."""

    id: str
    name: str
    directory: Path
    source: str  # "bundled" | "user" | "project" | "entrypoint"
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    license: str = ""
    homepage: str = ""
    kind: str = "standalone"
    api_version: int = SUPPORTED_API_VERSION
    tags: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    unknown_capabilities: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    requires_plugins: tuple[str, ...] = ()
    python_dependencies: tuple[str, ...] = ()
    config_schema: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: tuple[str, ...] = ()
    #: Set for entry-point plugins, which have a module rather than a directory.
    module_name: str = ""
    #: A `plugin.json` package: skills and MCP servers, no code. Nothing in one
    #: is ever imported, which is why it can declare no capabilities.
    portable: bool = False

    @property
    def is_entrypoint(self) -> bool:
        return bool(self.module_name)

    def missing_env(self) -> list[str]:
        return [name for name in self.requires_env if not os.environ.get(name)]


def _strings(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.append(item.strip())
    return tuple(seen)


def parse_manifest(
    data: Mapping[str, Any],
    directory: Path,
    source: str,
    *,
    module_name: str = "",
) -> PluginManifest:
    """Turn manifest data into a `PluginManifest`, or raise `PluginError`.

    Raises only on the two things that make a plugin unaddressable: no name, or
    a name that cannot be a path segment. Everything else degrades — an
    unreadable `kind` becomes `standalone` with a warning, an unknown
    capability is carried through so it can be *reported* rather than silently
    dropped.
    """
    if not isinstance(data, Mapping):
        raise PluginError(f"{directory / MANIFEST_FILENAME} must contain a mapping")

    raw_name = data.get("name")
    if isinstance(raw_name, bool) or (
        raw_name is not None and not isinstance(raw_name, str)
    ):
        # YAML 1.1 reads `on`, `off`, `yes`, `no`, `true` and `false` as
        # booleans, so `name: off` arrives here as `False` and the message
        # about a missing name would send someone looking at the wrong line.
        raise PluginError(
            f"{directory / MANIFEST_FILENAME}: `name:` parsed as "
            f"{type(raw_name).__name__} rather than text. YAML reads bare "
            f"`on`, `off`, `yes`, `no`, `true` and `false` as booleans — quote "
            f"it, as `name: \"{raw_name}\"`."
        )
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise PluginError(
            f"{directory / MANIFEST_FILENAME} has no `name:`. The name is the "
            f"plugin's id — it keys the ledger, namespaces its events and "
            f"names its state directory."
        )
    name = raw_name.strip()
    plugin_id = name.lower()
    if not PLUGIN_ID_RE.match(plugin_id):
        raise PluginError(
            f"Plugin name {name!r} is not usable as an id. Use lowercase "
            f"letters, digits, '.', '_' or '-', starting with a letter or "
            f"digit, at most 64 characters."
        )

    kind = data.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) and kind.strip() else "standalone"
    if kind not in VALID_KINDS:
        logger.warning(
            "plugin %s declares unknown kind %r (valid: %s); treating it as "
            "standalone",
            plugin_id,
            kind,
            ", ".join(sorted(VALID_KINDS)),
        )
        kind = "standalone"

    api_version = data.get("api_version", SUPPORTED_API_VERSION)
    if not isinstance(api_version, int):
        try:
            api_version = int(str(api_version).strip())
        except (TypeError, ValueError):
            api_version = SUPPORTED_API_VERSION

    known_caps, unknown_caps = caps.parse_declared(data.get("capabilities"))

    config_schema = data.get("config_schema")
    if not isinstance(config_schema, Mapping):
        config_schema = {}

    unknown_fields = tuple(
        sorted(key for key in data if isinstance(key, str) and key not in KNOWN_MANIFEST_FIELDS)
    )

    version = data.get("version")
    if not isinstance(version, str):
        version = "0.0.0" if version is None else str(version)

    return PluginManifest(
        id=plugin_id,
        name=name,
        directory=Path(directory),
        source=source,
        version=version.strip() or "0.0.0",
        description=str(data.get("description") or "").strip(),
        author=_author(data.get("author")),
        license=str(data.get("license") or "").strip(),
        homepage=str(data.get("homepage") or "").strip(),
        kind=kind,
        api_version=api_version,
        tags=_strings(data.get("tags")),
        capabilities=tuple(known_caps),
        unknown_capabilities=tuple(unknown_caps),
        requires_env=_strings(data.get("requires_env")),
        optional_env=_strings(data.get("optional_env")),
        requires_plugins=tuple(item.lower() for item in _strings(data.get("requires_plugins"))),
        python_dependencies=_strings(data.get("python_dependencies")),
        config_schema=dict(config_schema),
        unknown_fields=unknown_fields,
        module_name=module_name,
    )


def _author(value: Any) -> str:
    """Accept both the string form and the table form npm-style manifests use."""
    if isinstance(value, Mapping):
        return ", ".join(
            str(value[key]) for key in ("name", "email", "url") if value.get(key)
        )
    return "" if value is None else str(value).strip()


def read_manifest(directory: Path, source: str) -> PluginManifest:
    path = Path(directory) / MANIFEST_FILENAME
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise PluginError(f"{path} does not exist") from exc
    except yaml.YAMLError as exc:
        raise PluginError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise PluginError(f"{path} could not be read: {exc}") from exc
    return parse_manifest(raw, Path(directory), source)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def bundled_dir() -> Path | None:
    """The `plugins/` directory that shipped with this install.

    Resolved by walking up from this file, not from the working directory. The
    bundled *skills* shipped unreachable to every installed user because their
    resolution started at the cwd, and someone running from an ordinary project
    directory found nothing. Same trap, same fix.
    """
    override = os.environ.get(ENV_BUNDLED, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    here = Path(__file__).resolve()
    for parent in here.parents[:MAX_WALK_UP]:
        candidate = parent / "plugins"
        if _looks_like_plugins_dir(candidate):
            return candidate
    return None


def _looks_like_plugins_dir(candidate: Path) -> bool:
    """Whether this is *our* `plugins/`, and not somebody else's.

    The walk goes up several levels, so on a developer's machine it passes
    through directories nobody meant it to see — a `plugins/` belonging to an
    unrelated project two levels up would otherwise be adopted as ours. Content
    is the test, not the name: at least one child holding a manifest.
    """
    if not candidate.is_dir():
        return False
    try:
        return any(
            child.is_dir() and (child / MANIFEST_FILENAME).exists()
            for child in candidate.iterdir()
        )
    except OSError:
        return False


def user_dir() -> Path:
    from andromeda_cli import config as config_module

    return config_module.home() / "plugins"


def project_dir() -> Path:
    return Path.cwd() / ".andromeda" / "plugins"


def project_plugins_enabled() -> bool:
    return os.environ.get(ENV_PROJECT_PLUGINS, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def plugins_disabled() -> bool:
    return os.environ.get(ENV_DISABLE, "").strip().lower() in {"1", "true", "yes", "on"}


def _scan(directory: Path, source: str) -> list[PluginManifest]:
    if not directory.is_dir():
        return []
    found: list[PluginManifest] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        try:
            if (child / MANIFEST_FILENAME).exists():
                found.append(read_manifest(child, source))
            elif portable.is_portable(child):
                found.append(read_portable_manifest(child, source))
        except (PluginError, portable.PortableError) as exc:
            logger.warning("skipping plugin at %s: %s", child, exc)
    return found


def read_portable_manifest(directory: Path, source: str) -> PluginManifest:
    """A `plugin.json` package, as a `PluginManifest`.

    Read through the same type as a Python plugin so that everything
    downstream — the ledger, the listing, the enable flow — treats the two
    alike. What differs is one flag and what `_load_one` does with it.
    """
    package = portable.load(Path(directory))
    return PluginManifest(
        id=package.name.lower(),
        name=package.name,
        directory=Path(directory),
        source=source,
        version=package.version,
        description=package.description,
        author=package.author,
        license=package.license,
        homepage=package.homepage,
        kind="standalone",
        tags=package.keywords,
        unknown_fields=package.unknown_fields,
        portable=True,
    )


def _entrypoint_manifests() -> list[PluginManifest]:
    """Plugins installed with pip that advertise `andromeda_cli.plugins`.

    The entry point's value is a module; its manifest is the `plugin.yaml`
    beside that module. We resolve the module's *file location* without
    importing it — discovery must never execute a plugin that is not enabled.
    """
    found: list[PluginManifest] = []
    try:
        entry_points = importlib.metadata.entry_points()
    except Exception as exc:  # noqa: BLE001 - a broken installed dist is not fatal
        logger.warning("could not read entry points: %s", exc)
        return found

    try:
        selected = entry_points.select(group=ENTRY_POINT_GROUP)
    except AttributeError:  # pragma: no cover - importlib.metadata < 3.10 shape
        selected = [ep for ep in entry_points if getattr(ep, "group", "") == ENTRY_POINT_GROUP]

    for entry_point in selected:
        module_name = (entry_point.value or "").split(":", 1)[0].strip()
        if not module_name:
            continue
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, ModuleNotFoundError) as exc:
            logger.warning("entry point %s does not resolve: %s", entry_point.name, exc)
            continue
        if spec is None or not spec.origin:
            continue
        directory = Path(spec.origin).parent
        if not (directory / MANIFEST_FILENAME).exists():
            logger.warning(
                "entry point %s resolves to %s, which has no %s; skipping",
                entry_point.name,
                directory,
                MANIFEST_FILENAME,
            )
            continue
        try:
            manifest = read_manifest(directory, "entrypoint")
        except PluginError as exc:
            logger.warning("skipping entry-point plugin %s: %s", entry_point.name, exc)
            continue
        found.append(
            PluginManifest(**{**manifest.__dict__, "module_name": module_name})
        )
    return found


def discover() -> dict[str, PluginManifest]:
    """Every plugin this install can see, keyed by id.

    Four sources, in this order, later winning a name collision::

        bundled      <install>/plugins/
        user         <home>/plugins/
        project      ./.andromeda/plugins/     (opt-in)
        entrypoint   pip packages

    Later-wins is what lets someone fix a bundled plugin by dropping their own
    copy of it in `<home>/plugins/` with no config change at all.
    """
    manifests: dict[str, PluginManifest] = {}

    bundled = bundled_dir()
    if bundled is not None:
        for manifest in _scan(bundled, "bundled"):
            manifests[manifest.id] = manifest

    for manifest in _scan(user_dir(), "user"):
        manifests[manifest.id] = manifest

    if project_plugins_enabled():
        for manifest in _scan(project_dir(), "project"):
            manifests[manifest.id] = manifest

    for manifest in _entrypoint_manifests():
        manifests[manifest.id] = manifest

    return manifests


def resolve_load_order(manifests: Mapping[str, PluginManifest]) -> list[str]:
    """Ids in dependency order: if A requires B, B comes first.

    Ties break alphabetically so the order is the same on every machine. A
    cycle is reported and the whole set falls back to alphabetical — refusing
    to start because two third-party plugins reference each other punishes the
    wrong person. A missing dependency is warned about once and does not remove
    the dependent plugin; `ctx.has_plugin` is the runtime check.
    """
    import graphlib

    keys = sorted(manifests)
    edges: dict[str, set[str]] = {key: set() for key in keys}

    for key in keys:
        for dependency in manifests[key].requires_plugins:
            if dependency == key:
                logger.warning("plugin %s depends on itself; ignoring", key)
                continue
            if dependency not in manifests:
                logger.warning(
                    "plugin %s requires %r, which is not installed or not "
                    "enabled; loading anyway — check at runtime with "
                    "ctx.has_plugin(%r)",
                    key,
                    dependency,
                    dependency,
                )
                continue
            edges[key].add(dependency)

    sorter = graphlib.TopologicalSorter(edges)
    try:
        sorter.prepare()
    except graphlib.CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else []
        logger.warning(
            "plugin dependency cycle (%s); falling back to alphabetical order",
            " -> ".join(str(item) for item in cycle),
        )
        return keys

    ordered: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        ordered.extend(ready)
        sorter.done(*ready)
    return ordered


# ---------------------------------------------------------------------------
# Per-plugin state
# ---------------------------------------------------------------------------


class PluginState:
    """A small JSON key/value store, one per plugin, with a hard ceiling.

    Plugins need somewhere to keep a cursor, a cache key, a last-seen id. The
    alternatives are worse: the ledger is consent state, and letting a plugin
    write anywhere under the home means one plugin can corrupt another's.
    """

    def __init__(self, plugin_id: str) -> None:
        self._plugin_id = plugin_id
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        from andromeda_cli import config as config_module

        return config_module.home() / "plugin-data" / self._plugin_id

    @property
    def path(self) -> Path:
        return self.directory / "state.json"

    @property
    def quota_bytes(self) -> int:
        return STATE_QUOTA_BYTES

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or ".." in key or not STATE_KEY_RE.match(key):
            raise ValueError(
                "Plugin state keys are 1-128 characters of letters, digits, "
                "'_', '-', '.' or ':', and may not contain '..'."
            )

    def _read(self) -> dict[str, Any]:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot parse plugin state {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"cannot parse plugin state {self.path}: not an object")
        return data

    def get(self, key: str, default: Any = None) -> Any:
        self._validate_key(key)
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        import json

        self._validate_key(key)
        with self._lock:
            data = self._read()
            data[key] = value
            try:
                encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"plugin state value for {key!r} is not JSON-serializable"
                ) from exc
            if len(encoded) > self.quota_bytes:
                raise ValueError(
                    f"plugin state quota exceeded: {len(encoded)} bytes is over "
                    f"the {self.quota_bytes}-byte per-plugin limit"
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_bytes(encoded)
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)

    def delete(self, key: str) -> bool:
        import json

        self._validate_key(key)
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
            return True

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._read())


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------


@dataclass
class PromptSection:
    plugin_id: str
    section_id: str
    content: str


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    module: Any = None
    error: str = ""
    #: Callbacks the plugin asked to run when it is unloaded.
    unload_callbacks: list[Callable[[], None]] = field(default_factory=list)
    #: Non-fatal problems found while loading — a portable package's broken
    #: skill, say. Kept rather than logged and forgotten, because "my skill
    #: does not appear" is otherwise unanswerable.
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


# ---------------------------------------------------------------------------
# The context handed to a plugin
# ---------------------------------------------------------------------------


class PluginContext:
    """What `register(ctx)` receives.

    Every method either adds something the harness will offer, or replaces
    something it already had. The replacing ones are capability-gated; the
    adding ones are not, because adding a tool the user then has to enable is
    already gated by the tool policy that gates every other tool.
    """

    def __init__(self, manifest: PluginManifest, manager: "PluginManager") -> None:
        self.manifest = manifest
        self._manager = manager
        self._state: PluginState | None = None

    # -- identity ----------------------------------------------------------

    @property
    def plugin_id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def has_plugin(self, plugin_id: str) -> bool:
        """Whether another plugin is loaded *and* loaded successfully.

        A plugin that raised during `register()` is not here. That is the
        useful reading: a dependant should take the fallback path when its
        dependency is broken, not only when it is absent.
        """
        loaded = self._manager.loaded.get(str(plugin_id).lower())
        return loaded is not None and loaded.ok

    def has_capability(self, capability: str) -> bool:
        return caps.is_granted(self.plugin_id, capability)

    def _require(self, capability: str) -> None:
        caps.require(self.plugin_id, self.manifest.name, capability)

    # -- settings and state ------------------------------------------------

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read one of this plugin's own settings from the ledger."""
        return plugin_store.plugin_config(self.plugin_id).get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        plugin_store.set_plugin_config(self.plugin_id, key, value)

    @property
    def state(self) -> PluginState:
        if self._state is None:
            self._state = PluginState(self.plugin_id)
        return self._state

    def on_unload(self, callback: Callable[[], None]) -> None:
        """Run `callback` when this plugin is unloaded.

        Unload happens on a config reload and in tests. A plugin that opened a
        file, a socket or a thread registers its close here; without it, a
        reload leaks one of each, every time.
        """
        if not callable(callback):
            raise PluginError("on_unload requires a callable")
        self._manager.loaded[self.plugin_id].unload_callbacks.append(callback)

    # -- adding ------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        run: Callable[..., Any],
        *,
        risk_tier: str = "outbound",
        category: str = "write",
        summarize: Callable[[dict[str, Any]], str] | None = None,
        override: bool = False,
    ) -> None:
        """Offer a tool under `name`.

        `risk_tier` and `category` are the same vocabulary every built-in uses,
        because the approval gate and the specialist belts are written in terms
        of them. They default *pessimistically* — `outbound`/`write` — so a
        plugin author who omits them gets a tool that asks before it runs
        rather than one that is quietly treated as a safe local read.

        Replacing a built-in needs `tools.override`. That gate is the single
        most consequential one in this file: a plugin holding it and
        registering `terminal` sees every command the model runs.
        """
        from andromeda_tools.spec import TIER_ORDER

        tool_name = str(name).strip()
        if not tool_name:
            raise PluginError("register_tool needs a name")
        if risk_tier not in TIER_ORDER:
            raise PluginError(
                f"unknown risk tier {risk_tier!r} for tool {tool_name!r}; "
                f"expected one of {', '.join(TIER_ORDER)}"
            )
        if category not in {"read", "write", "admin"}:
            raise PluginError(
                f"unknown category {category!r} for tool {tool_name!r}; "
                f"expected read, write or admin"
            )
        if not callable(run):
            raise PluginError(f"tool {tool_name!r} needs a callable `run`")

        if override:
            self._require("tools.override")
        elif tool_name in builtin_tool_names():
            raise ToolOverrideError(
                f"Plugin {self.manifest.name!r} tried to register the built-in "
                f"tool {tool_name!r} without asking to override it. Pass "
                f"override=True and declare the 'tools.override' capability in "
                f"{MANIFEST_FILENAME} if replacing it is the intent."
            )

        self._manager.add_tool(
            self.plugin_id,
            tool_name,
            description=str(description or ""),
            parameters=parameters if isinstance(parameters, dict) else {},
            run=run,
            risk_tier=risk_tier,
            category=category,
            summarize=summarize,
            override=bool(override),
        )

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Subscribe to one of the harness's lifecycle events.

        The vocabulary is `hooks.VALID_HOOKS` — the same events a shell hook
        can take, because there is exactly one hook bus and adding a second
        would mean two answers to "what is listening".
        """
        if event not in hooks_module.VALID_HOOKS:
            known = ", ".join(sorted(hooks_module.VALID_HOOKS))
            raise PluginError(f"unknown hook event {event!r}. Known events: {known}")
        if not callable(callback):
            raise PluginError("register_hook requires a callable")
        hooks_module.register(event, callback)
        self._manager.note_hook(self.plugin_id, event, callback)

    def register_command(
        self,
        name: str,
        handler: Callable[[str], Any],
        description: str = "",
        *,
        override: bool = False,
    ) -> None:
        """Add a `/slash` command to the interactive surfaces.

        The handler takes the raw argument string and returns text to print, or
        None to print nothing.
        """
        command = str(name).strip().lstrip("/")
        if not command:
            raise PluginError("register_command needs a name")
        if not callable(handler):
            raise PluginError(f"command /{command} needs a callable handler")
        if override:
            self._require("commands.override")
        elif command in builtin_command_names():
            raise caps.CapabilityError(
                f"Plugin {self.manifest.name!r} tried to register the built-in "
                f"command /{command} without asking to override it. Pass "
                f"override=True and declare 'commands.override' if replacing "
                f"it is the intent."
            )
        self._manager.add_command(
            self.plugin_id, command, handler, str(description or ""), bool(override)
        )

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup: Callable[[Any], None],
        handler: Callable[[Any], Any],
    ) -> None:
        """Add an `andromeda <name>` subcommand.

        `setup` is handed the argparse subparser to add arguments to; `handler`
        is handed the parsed namespace and returns an exit code (or None for 0).
        """
        command = str(name).strip()
        if not command:
            raise PluginError("register_cli_command needs a name")
        if not callable(setup) or not callable(handler):
            raise PluginError(f"cli command {command!r} needs callable setup and handler")
        self._manager.add_cli_command(
            self.plugin_id, command, str(help or ""), setup, handler
        )

    def register_skill(self, name: str, path: Path, description: str = "") -> str:
        """Offer a skill, addressable as `<plugin id>:<name>`.

        Plugin skills are **not** listed in the system prompt's skill index and
        are loaded explicitly by name. A plugin that could add lines to the
        index could grow the cached prefix of every request without ever being
        called.
        """
        skill_name = str(name).strip()
        if not skill_name or ":" in skill_name or not re.fullmatch(r"[A-Za-z0-9_-]+", skill_name):
            raise PluginError(
                f"invalid skill name {name!r}: letters, digits, '_' and '-' "
                f"only, and no ':' — the namespace is this plugin's id"
            )
        resolved = Path(path)
        if not resolved.exists():
            raise PluginError(f"skill file not found: {resolved}")
        return self._manager.add_skill(
            self.plugin_id, skill_name, resolved, str(description or "")
        )

    def register_delivery(self, mode: str, sender: Callable[..., bool]) -> None:
        """Add a way for a scheduled job to reach someone.

        `sender(name, body, ok, target)` returns True when it delivered. The
        built-in modes are `notify`, `stdout` and `webhook`; this is how a
        plugin adds SMS, email or a chat platform.
        """
        key = str(mode).strip().lower()
        if not key:
            raise PluginError("register_delivery needs a mode name")
        if not callable(sender):
            raise PluginError(f"delivery mode {key!r} needs a callable sender")
        self._manager.add_delivery(self.plugin_id, key, sender)

    def register_redaction_patterns(self, patterns: Iterable[str]) -> int:
        """Teach the redaction chokepoint to recognise more secret shapes.

        Returns how many compiled. This is deliberately *not* gated: it can
        only ever mask more, never less, so the worst a hostile plugin achieves
        is hiding its own traffic from the transcript — which it could do
        anyway by not printing it.
        """
        from . import redact

        added = 0
        for pattern in patterns or ():
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                logger.warning(
                    "plugin %s supplied an invalid redaction pattern %r: %s",
                    self.plugin_id,
                    pattern,
                    exc,
                )
                continue
            self._manager.add_redaction(self.plugin_id, pattern)
            added += 1
        if added:
            redact.register_plugin_patterns(self._manager.redaction_patterns())
        return added

    # -- replacing (capability-gated) --------------------------------------

    def register_system_prompt_section(self, section_id: str, content: str) -> None:
        """Add a block of text to the system prompt.

        Bounded three ways — per section, per plugin count, and in total —
        because the system prompt is the cached prefix of every request and an
        unbounded section is an unbounded per-turn cost on the user's money.
        """
        self._require("prompt.inject")
        identifier = str(section_id).strip().lower()
        if not PROMPT_SECTION_ID_RE.match(identifier):
            raise PluginError(
                f"invalid prompt section id {section_id!r}: lowercase letters, "
                f"digits, '.', '_' or '-', at most 64 characters"
            )
        text = str(content or "").strip()
        if not text:
            raise PluginError("a prompt section needs content")
        if len(text) > MAX_PROMPT_SECTION_CHARS:
            raise PluginError(
                f"prompt section {identifier!r} is {len(text)} characters, over "
                f"the {MAX_PROMPT_SECTION_CHARS}-character per-section limit"
            )
        self._manager.add_prompt_section(self.plugin_id, identifier, text)

    def register_memory_backend(self, name: str, factory: Callable[[Path], Any]) -> None:
        """Offer a place for memories to live, selectable as `memory_backend`."""
        self._require("memory.backend")
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_memory_backend needs a name")
        if not callable(factory):
            raise PluginError(f"memory backend {key!r} needs a callable factory")
        self._manager.add_memory_backend(self.plugin_id, key, factory)

    def register_cron_provider(self, name: str, provider: Any) -> None:
        """Offer a scheduler, selectable as `cron_provider`."""
        self._require("cron.provider")
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_cron_provider needs a name")
        self._manager.add_cron_provider(self.plugin_id, key, provider)

    def register_model_provider(self, name: str, factory: Callable[..., Any]) -> None:
        """Offer a model provider, selectable as `provider`."""
        self._require("model.provider")
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_model_provider needs a name")
        if not callable(factory):
            raise PluginError(f"model provider {key!r} needs a callable factory")
        self._manager.add_model_provider(self.plugin_id, key, factory)

    def register_secret_source(self, scheme: str, resolver: Callable[[str], Any]) -> None:
        """Resolve `secrets:<scheme>:...` references.

        The resolver is handed the reference and returns whatever
        `secrets.Resolution` expects. It is asked for the user's secrets by
        name, which is why the capability exists.
        """
        self._require("secrets.source")
        key = str(scheme).strip().lower()
        if not key:
            raise PluginError("register_secret_source needs a scheme")
        if not callable(resolver):
            raise PluginError(f"secret source {key!r} needs a callable resolver")
        self._manager.add_secret_source(self.plugin_id, key, resolver)

    def register_web_search_provider(
        self, name: str, search: Callable[[str, int], Any]
    ) -> None:
        """Offer a `web_search` backend.

        `search(query, limit)` returns a `ToolResult`. Additive rather than
        gated: it cannot shadow `brave` or `tavily`, and it is only reached
        when neither of their keys is set — so a plugin here is answering a
        question that would otherwise be "no search provider is configured".
        """
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_web_search_provider needs a name")
        if not callable(search):
            raise PluginError(f"web search provider {key!r} needs a callable")
        self._manager.add_web_search(self.plugin_id, key, search)

    def register_browser_provider(self, name: str, factory: Callable[..., Any]) -> None:
        """Answer as the browser.

        Gated, unlike web search, because this is not a fallback — it replaces
        the surface the agent drives, which carries whatever session cookies it
        has been signed in with.
        """
        self._require("browser.provider")
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_browser_provider needs a name")
        if not callable(factory):
            raise PluginError(f"browser provider {key!r} needs a callable factory")
        self._manager.add_browser(self.plugin_id, key, factory)

    def register_lsp_server(self, server: Any) -> None:
        """Teach the diagnostics layer about another language server.

        Takes an `lsp.servers.Server`. Additive and ungated: nothing is ever
        installed by this harness — a server that is not on the PATH is named
        with its install command and skipped — so the worst a bad entry does is
        advertise a binary nobody has.
        """
        identifier = str(getattr(server, "id", "") or "").strip()
        if not identifier:
            raise PluginError("a language server needs an `id`")
        if not getattr(server, "binaries", None):
            raise PluginError(f"language server {identifier!r} names no binaries")
        self._manager.add_lsp_server(self.plugin_id, identifier, server)

    def register_specialist(self, specialist: Any) -> None:
        """Define a delegation lane.

        Gated, and this is the least obvious of the twelve. A specialist's
        `admits` decides which tools its children may call — the belt *is* the
        permission boundary for everything that runs inside it, so defining one
        is defining what a whole class of child agents is allowed to do.
        """
        self._require("lanes.specialist")
        identifier = str(getattr(specialist, "id", "") or "").strip()
        if not identifier:
            raise PluginError("a specialist needs an `id`")
        if not callable(getattr(specialist, "admits", None)):
            raise PluginError(f"specialist {identifier!r} needs a callable `admits`")
        self._manager.add_specialist(self.plugin_id, identifier, specialist)

    def register_blueprint(self, blueprint: Any) -> None:
        """Add an automation to the `cron blueprint` catalogue.

        Ungated. A blueprint is a *form*; filling it in still goes through
        `Schedule.add`, which refuses `approval_mode="auto"` from an agent and
        names the class it chose. A plugin cannot make a job more permissive
        than the person creating it asked for.
        """
        key = str(getattr(blueprint, "key", "") or "").strip()
        if not key:
            raise PluginError("a blueprint needs a `key`")
        self._manager.add_blueprint(self.plugin_id, key, blueprint)

    def register_eval(self, scenario: Any) -> None:
        """Add a behavioural evaluation to `andromeda eval`.

        Ungated: it runs only when somebody types the command.
        """
        name = str(getattr(scenario, "name", "") or "").strip()
        if not name:
            raise PluginError("an eval scenario needs a `name`")
        self._manager.add_eval(self.plugin_id, name, scenario)

    def register_auxiliary_task(self, key: str, purpose: str = "vision") -> None:
        """Declare a side task that may call an auxiliary model.

        `purpose` names which auxiliary model to bind, and it must be one this
        build already allows — a plugin cannot introduce a model. The lock is
        the product decision this harness is built on, and a registration point
        that could add a model id would be a hole straight through it.

        What the capability buys is the *spending*: side calls on the user's
        credential that they see on the bill and not in the transcript.
        """
        self._require("model.auxiliary")
        from .models import AUXILIARY_MODELS

        name = str(key).strip().lower()
        if not name:
            raise PluginError("register_auxiliary_task needs a key")
        if purpose not in AUXILIARY_MODELS:
            allowed = ", ".join(sorted(AUXILIARY_MODELS)) or "none"
            raise PluginError(
                f"auxiliary task {name!r} asked for purpose {purpose!r}, which "
                f"this build has no model for. Available: {allowed}. A plugin "
                f"cannot introduce a model — see the model lock."
            )
        self._manager.add_auxiliary_task(self.plugin_id, name, purpose)

    def register_approval_transport(
        self, name: str, present: Callable[..., Any]
    ) -> None:
        """Answer approval prompts from somewhere other than this terminal.

        `present(request)` returns one of the gate's answers. The most
        consequential registration here: it decides what you *would* have said
        to "may I run this", at a moment you may not be present for.
        """
        self._require("approvals.transport")
        key = str(name).strip().lower()
        if not key:
            raise PluginError("register_approval_transport needs a name")
        if not callable(present):
            raise PluginError(f"approval transport {key!r} needs a callable")
        self._manager.add_approval_transport(self.plugin_id, key, present)

    def register_middleware(self, kind: str, callback: Callable[..., Any]) -> None:
        """Wrap what happens, rather than watch it.

        Four kinds — `tool_request`, `tool_execution`, `llm_request`,
        `llm_execution` — each with a real fire site in the loop. See
        `middleware.py` for the two shapes and the nesting order.
        """
        self._require("runtime.middleware")
        from . import middleware as middleware_module

        if kind not in middleware_module.VALID_KINDS:
            known = ", ".join(sorted(middleware_module.VALID_KINDS))
            raise PluginError(f"unknown middleware kind {kind!r}. Known: {known}")
        if not callable(callback):
            raise PluginError("register_middleware requires a callable")
        self._manager.add_middleware(self.plugin_id, kind, callback)

    # -- reaching back into the harness ------------------------------------

    @property
    def profile_name(self) -> str:
        """Which install profile this process is running under."""
        from andromeda_cli import profiles

        return profiles.selected()

    @property
    def llm(self):
        """An auxiliary model client, or None.

        Gated by `model.auxiliary` for the same reason `register_auxiliary_task`
        is: every call spends the user's credit somewhere they cannot see it.
        Returns None rather than raising when no auxiliary is available — a
        plugin that has to work without one should be able to ask.
        """
        self._require("model.auxiliary")
        return self._manager.auxiliary()

    def call_mcp(self, server: str, tool: str, arguments: dict[str, Any] | None = None):
        """Call a tool on one of the user's configured MCP servers.

        Ungated, and deliberately so: a plugin could import the MCP client and
        read the same config file directly, so a gate here would be a gate on
        the convenient path only — the worst kind, because it reads as a
        boundary and is not one.
        """
        from andromeda_tools import mcp as mcp_module
        from andromeda_cli import config as config_module

        for candidate in mcp_module.build_servers(config_module.home()):
            if candidate.name != server:
                continue
            candidate.connect()
            return candidate.call(tool, arguments or {})
        raise PluginError(f"no MCP server named {server!r} is configured")

    def dispatch_tool(self, name: str, arguments: dict[str, Any] | None = None):
        """Run another tool, through the live session's registry.

        Only available inside a session — a plugin that calls this from
        `register()` gets a clear refusal rather than a registry bound to
        nothing, because at registration time the workspace does not exist yet.
        """
        registry = self._manager.session_registry()
        if registry is None:
            raise PluginError(
                "dispatch_tool needs a live session; there is none at "
                "registration time. Call it from a hook or a tool instead."
            )
        spec = registry.get(name)
        if spec is None:
            raise PluginError(f"no tool named {name!r} in this session")
        return spec.run(**(arguments or {}))

    # -- plugin-to-plugin --------------------------------------------------

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> int:
        """Publish `<this plugin's id>:<event>`; return how many were called.

        The namespace is *forced*, not trusted. A plugin may not emit under
        `andromeda:` (reserved for the host) or under another plugin's id,
        because a subscriber that trusts the prefix would otherwise be
        trivially fooled.

        Dispatch is synchronous, matching the hook bus — each subscriber is
        isolated, gets a deep copy of the payload, and cannot see the others'
        mutations. The usual alternative is a background worker queue here, so
        a blocking subscriber cannot stall the emitter; that trade is not taken because a
        second dispatch model in one process is a second answer to "did my
        callback run yet", and the depth cap below covers the failure that
        actually happens (a plugin emitting from its own subscriber).
        """
        if not isinstance(event, str) or not event.strip():
            raise PluginError("emit requires a non-empty event name")
        if ":" in event:
            raise PluginError(
                f"plugin {self.plugin_id!r} may not emit {event!r}: pass the "
                f"bare event name. The namespace is forced to "
                f"'{self.plugin_id}:', and '{CORE_EVENT_NAMESPACE}:' is "
                f"reserved for the host."
            )
        if payload is not None and not isinstance(payload, dict):
            raise PluginError("emit payload must be a dict or None")
        return self._manager.dispatch_event(
            f"{self.plugin_id}:{event.strip()}", payload or {}
        )

    def subscribe(self, event: str, callback: Callable[..., Any]) -> None:
        """Listen for a fully-qualified `<plugin id>:<event>`.

        Subscribing is unrestricted — only emitting is namespace-gated. A
        listener learns nothing it could not learn by asking the plugin
        directly, and gating it would mean plugins declaring their listeners in
        a manifest nobody keeps up to date.
        """
        if not isinstance(event, str) or not event.strip():
            raise PluginError("subscribe requires a non-empty event name")
        if not callable(callback):
            raise PluginError("subscribe requires a callable")
        self._manager.subscribe_event(self.plugin_id, event.strip(), callback)


# ---------------------------------------------------------------------------
# What is already taken
# ---------------------------------------------------------------------------

#: Every tool name this harness ships. A plugin registering one of these
#: without `tools.override` is refused — silently shadowing `terminal` is the
#: single worst thing this socket could allow by accident.
#:
#: The list is written out rather than derived, because deriving it means
#: building a real registry (a workspace, a todo list, a browser session) at
#: import time. `tests/test_plugins.py::test_builtin_tool_names_are_complete`
#: builds one for real and fails if this set has drifted, so the cost of
#: writing it down is paid by the suite and not by every start.
BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser_back",
        "browser_click",
        "browser_navigate",
        "browser_press",
        "browser_read",
        "browser_scroll",
        "browser_snapshot",
        "browser_type",
        "clarify",
        "cron",
        "delegate",
        "list_dir",
        "memory_forget",
        "memory_search",
        "memory_store",
        "notepad",
        "patch",
        "process",
        "read_file",
        "search_files",
        "session_search",
        "skill_load",
        "subagents_list",
        "subagents_status",
        "subagents_wait",
        "terminal",
        "todo",
        "tool_call",
        "tool_describe",
        "tool_search",
        "vision_analyze",
        "web_fetch",
        "web_search",
        "write_file",
    }
)

#: Slash commands the interactive surfaces already answer, without the slash.
#: Pinned against `repl.py`'s dispatch by
#: `tests/test_plugins.py::test_builtin_command_names_match_the_repl`.
BUILTIN_COMMAND_NAMES: frozenset[str] = frozenset(
    {
        "approve",
        "credits",
        "cwd",
        "exit",
        "help",
        "history",
        "jobs",
        "lanes",
        "model",
        "new",
        "ps",
        "quit",
        "recap",
        "resume",
        "rewind",
        "sessions",
        "skills",
        "think",
        "tools",
        "upgrade",
        "usage",
    }
)


def builtin_tool_names() -> frozenset[str]:
    return BUILTIN_TOOL_NAMES


def builtin_command_names() -> frozenset[str]:
    return BUILTIN_COMMAND_NAMES


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


@dataclass
class ToolRegistration:
    plugin_id: str
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Any]
    risk_tier: str
    category: str
    summarize: Callable[[dict[str, Any]], str] | None
    override: bool


@dataclass
class CommandRegistration:
    plugin_id: str
    name: str
    handler: Callable[[str], Any]
    description: str
    override: bool


@dataclass
class CliCommandRegistration:
    plugin_id: str
    name: str
    help: str
    setup: Callable[[Any], None]
    handler: Callable[[Any], Any]


class PluginManager:
    """Everything plugins registered, and how it got there.

    One instance per process. Held module-level rather than passed around
    because the seams it feeds — the tool registry, the hook bus, the CLI
    parser — are themselves reached module-level, and threading a manager
    through all of them would be a larger change than the socket itself.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.loaded: dict[str, LoadedPlugin] = {}
        self._tools: dict[str, ToolRegistration] = {}
        self._commands: dict[str, CommandRegistration] = {}
        self._cli_commands: dict[str, CliCommandRegistration] = {}
        self._skills: dict[str, dict[str, Any]] = {}
        self._memory_backends: dict[str, tuple[str, Callable[[Path], Any]]] = {}
        self._cron_providers: dict[str, tuple[str, Any]] = {}
        self._model_providers: dict[str, tuple[str, Callable[..., Any]]] = {}
        self._secret_sources: dict[str, tuple[str, Callable[[str], Any]]] = {}
        self._delivery: dict[str, tuple[str, Callable[..., bool]]] = {}
        self._redaction: list[tuple[str, str]] = []
        self._web_search: dict[str, tuple[str, Callable[..., Any]]] = {}
        self._browser: dict[str, tuple[str, Callable[..., Any]]] = {}
        self._lsp_servers: dict[str, tuple[str, Any]] = {}
        self._specialists: dict[str, tuple[str, Any]] = {}
        self._blueprints: dict[str, tuple[str, Any]] = {}
        self._evals: dict[str, tuple[str, Any]] = {}
        self._auxiliary_tasks: dict[str, tuple[str, str]] = {}
        self._approval_transports: dict[str, tuple[str, Callable[..., Any]]] = {}
        self._mcp_servers: dict[str, tuple[str, dict[str, Any]]] = {}
        self._middleware: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        # Set by the surface once a session exists. `ctx.dispatch_tool` and
        # `ctx.llm` are the only things that read them, and both refuse
        # honestly when there is no session rather than binding to nothing.
        self._session_registry: dict[str, Any] | None = None
        self._auxiliary: Any = None
        self._prompt_sections: list[PromptSection] = []
        self._subscribers: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._hooks_by_plugin: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._event_depth = threading.local()
        self.discovered: dict[str, PluginManifest] = {}

    # -- registration sinks (called from PluginContext) --------------------

    def add_tool(self, plugin_id: str, name: str, **fields: Any) -> None:
        with self._lock:
            existing = self._tools.get(name)
            if existing is not None and existing.plugin_id != plugin_id:
                # First registration wins, and the loser is named. Two plugins
                # claiming one name is a conflict the user has to resolve, and
                # the only useless response is to pick silently.
                logger.warning(
                    "plugin %s registered tool %r, which plugin %s already "
                    "provides; keeping %s's and ignoring %s's",
                    plugin_id,
                    name,
                    existing.plugin_id,
                    existing.plugin_id,
                    plugin_id,
                )
                return
            self._tools[name] = ToolRegistration(plugin_id=plugin_id, name=name, **fields)

    def add_command(
        self,
        plugin_id: str,
        name: str,
        handler: Callable[[str], Any],
        description: str,
        override: bool,
    ) -> None:
        with self._lock:
            existing = self._commands.get(name)
            if existing is not None and existing.plugin_id != plugin_id:
                logger.warning(
                    "plugin %s registered /%s, which plugin %s already "
                    "provides; keeping %s's",
                    plugin_id,
                    name,
                    existing.plugin_id,
                    existing.plugin_id,
                )
                return
            self._commands[name] = CommandRegistration(
                plugin_id, name, handler, description, override
            )

    def add_cli_command(
        self,
        plugin_id: str,
        name: str,
        help_text: str,
        setup: Callable[[Any], None],
        handler: Callable[[Any], Any],
    ) -> None:
        with self._lock:
            existing = self._cli_commands.get(name)
            if existing is not None and existing.plugin_id != plugin_id:
                logger.warning(
                    "plugin %s registered `andromeda %s`, which plugin %s "
                    "already provides; keeping %s's",
                    plugin_id,
                    name,
                    existing.plugin_id,
                    existing.plugin_id,
                )
                return
            self._cli_commands[name] = CliCommandRegistration(
                plugin_id, name, help_text, setup, handler
            )

    def add_skill(
        self, plugin_id: str, name: str, path: Path, description: str
    ) -> str:
        qualified = f"{plugin_id}:{name}"
        with self._lock:
            self._skills[qualified] = {
                "plugin_id": plugin_id,
                "name": name,
                "path": path,
                "description": description,
            }
        return qualified

    def add_memory_backend(
        self, plugin_id: str, name: str, factory: Callable[[Path], Any]
    ) -> None:
        with self._lock:
            self._memory_backends[name] = (plugin_id, factory)

    def add_cron_provider(self, plugin_id: str, name: str, provider: Any) -> None:
        with self._lock:
            self._cron_providers[name] = (plugin_id, provider)

    def add_model_provider(
        self, plugin_id: str, name: str, factory: Callable[..., Any]
    ) -> None:
        with self._lock:
            self._model_providers[name] = (plugin_id, factory)

    def add_secret_source(
        self, plugin_id: str, scheme: str, resolver: Callable[[str], Any]
    ) -> None:
        with self._lock:
            self._secret_sources[scheme] = (plugin_id, resolver)

    def add_delivery(
        self, plugin_id: str, mode: str, sender: Callable[..., bool]
    ) -> None:
        with self._lock:
            self._delivery[mode] = (plugin_id, sender)

    def add_redaction(self, plugin_id: str, pattern: str) -> None:
        with self._lock:
            self._redaction.append((plugin_id, pattern))

    def _add_first_wins(
        self, store: dict, plugin_id: str, key: str, value: Any, label: str
    ) -> None:
        """Store `value` under `key` unless another plugin already claimed it.

        First registration wins and the loser is named. Two plugins claiming
        one name is a conflict the user has to resolve; the only useless
        response is to pick one silently.
        """
        with self._lock:
            existing = store.get(key)
            if existing is not None and existing[0] != plugin_id:
                logger.warning(
                    "plugin %s registered %s %r, which plugin %s already "
                    "provides; keeping %s's",
                    plugin_id,
                    label,
                    key,
                    existing[0],
                    existing[0],
                )
                return
            store[key] = (plugin_id, value)

    def add_web_search(self, plugin_id: str, name: str, search: Any) -> None:
        self._add_first_wins(self._web_search, plugin_id, name, search, "web search provider")

    def add_browser(self, plugin_id: str, name: str, factory: Any) -> None:
        self._add_first_wins(self._browser, plugin_id, name, factory, "browser provider")

    def add_lsp_server(self, plugin_id: str, identifier: str, server: Any) -> None:
        self._add_first_wins(self._lsp_servers, plugin_id, identifier, server, "language server")

    def add_specialist(self, plugin_id: str, identifier: str, specialist: Any) -> None:
        self._add_first_wins(self._specialists, plugin_id, identifier, specialist, "specialist")

    def add_blueprint(self, plugin_id: str, key: str, blueprint: Any) -> None:
        self._add_first_wins(self._blueprints, plugin_id, key, blueprint, "blueprint")

    def add_eval(self, plugin_id: str, name: str, scenario: Any) -> None:
        self._add_first_wins(self._evals, plugin_id, name, scenario, "eval scenario")

    def add_auxiliary_task(self, plugin_id: str, key: str, purpose: str) -> None:
        self._add_first_wins(self._auxiliary_tasks, plugin_id, key, purpose, "auxiliary task")

    def add_approval_transport(self, plugin_id: str, name: str, present: Any) -> None:
        self._add_first_wins(
            self._approval_transports, plugin_id, name, present, "approval transport"
        )

    def add_mcp_server(self, plugin_id: str, name: str, config: dict[str, Any]) -> None:
        self._add_first_wins(self._mcp_servers, plugin_id, name, config, "MCP server")

    def add_middleware(self, plugin_id: str, kind: str, callback: Any) -> None:
        # Appended, not first-wins: middleware composes, and two plugins each
        # adding a retry is a coherent thing to want. Registration order is the
        # nesting order, so it is never sorted.
        with self._lock:
            self._middleware.setdefault(kind, []).append((plugin_id, callback))

    def add_prompt_section(self, plugin_id: str, section_id: str, content: str) -> None:
        with self._lock:
            if len(self._prompt_sections) >= MAX_PROMPT_SECTIONS:
                raise PluginError(
                    f"there are already {MAX_PROMPT_SECTIONS} plugin prompt "
                    f"sections, which is the limit; {plugin_id}:{section_id} "
                    f"was not added"
                )
            total = sum(len(item.content) for item in self._prompt_sections)
            if total + len(content) > MAX_PROMPT_SECTIONS_TOTAL_CHARS:
                raise PluginError(
                    f"plugin prompt sections would total "
                    f"{total + len(content)} characters, over the "
                    f"{MAX_PROMPT_SECTIONS_TOTAL_CHARS}-character limit; "
                    f"{plugin_id}:{section_id} was not added"
                )
            self._prompt_sections = [
                item
                for item in self._prompt_sections
                if not (item.plugin_id == plugin_id and item.section_id == section_id)
            ]
            self._prompt_sections.append(PromptSection(plugin_id, section_id, content))

    def note_hook(
        self, plugin_id: str, event: str, callback: Callable[..., Any]
    ) -> None:
        """Remember a hook so `unload` can take it off the bus again.

        The callback itself is kept, not just the event name. Without it an
        unload would leave a live callback pointing into a module that has been
        removed from `sys.modules` — a reload then fires both the old and the
        new one, and the old one is the version the user just replaced.
        """
        with self._lock:
            self._hooks_by_plugin.setdefault(plugin_id, []).append((event, callback))

    # -- the event bus -----------------------------------------------------

    def subscribe_event(
        self, plugin_id: str, event: str, callback: Callable[..., Any]
    ) -> None:
        with self._lock:
            self._subscribers.setdefault(event, []).append((plugin_id, callback))

    def dispatch_event(self, event: str, payload: dict[str, Any]) -> int:
        depth = getattr(self._event_depth, "value", 0)
        if depth >= MAX_EVENT_DEPTH:
            logger.warning(
                "dropping plugin event %s: %d levels deep, which means a "
                "subscriber is emitting into its own chain",
                event,
                depth,
            )
            return 0

        with self._lock:
            listeners = tuple(self._subscribers.get(event, ()))
        if not listeners:
            return 0

        self._event_depth.value = depth + 1
        called = 0
        try:
            for plugin_id, callback in listeners:
                try:
                    # A deep copy per subscriber: one that mutates the payload
                    # must not change what the next one sees, or the answer
                    # depends on registration order in a way nobody can debug.
                    callback(copy.deepcopy(payload))
                    called += 1
                except Exception as exc:  # noqa: BLE001 - one bad listener only
                    logger.warning(
                        "plugin %s subscriber for %s raised: %s",
                        plugin_id,
                        event,
                        exc,
                    )
        finally:
            self._event_depth.value = depth
        return called

    # -- reading it back ---------------------------------------------------

    def tools(self) -> dict[str, ToolRegistration]:
        with self._lock:
            return dict(self._tools)

    def commands(self) -> dict[str, CommandRegistration]:
        with self._lock:
            return dict(self._commands)

    def cli_commands(self) -> dict[str, CliCommandRegistration]:
        with self._lock:
            return dict(self._cli_commands)

    def skills(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._skills)

    def memory_backends(self) -> dict[str, Callable[[Path], Any]]:
        with self._lock:
            return {name: factory for name, (_, factory) in self._memory_backends.items()}

    def cron_providers(self) -> dict[str, Any]:
        with self._lock:
            return {name: provider for name, (_, provider) in self._cron_providers.items()}

    def model_providers(self) -> dict[str, Callable[..., Any]]:
        with self._lock:
            return {name: factory for name, (_, factory) in self._model_providers.items()}

    def secret_sources(self) -> dict[str, Callable[[str], Any]]:
        with self._lock:
            return {name: resolver for name, (_, resolver) in self._secret_sources.items()}

    def delivery_modes(self) -> dict[str, Callable[..., bool]]:
        with self._lock:
            return {name: sender for name, (_, sender) in self._delivery.items()}

    def redaction_patterns(self) -> list[str]:
        with self._lock:
            return [pattern for _, pattern in self._redaction]

    def _values(self, store: dict) -> dict[str, Any]:
        with self._lock:
            return {key: value for key, (_, value) in store.items()}

    def web_search_providers(self) -> dict[str, Callable[..., Any]]:
        return self._values(self._web_search)

    def browser_providers(self) -> dict[str, Callable[..., Any]]:
        return self._values(self._browser)

    def lsp_servers(self) -> list[Any]:
        return [value for _, value in sorted(self._values(self._lsp_servers).items())]

    def specialists(self) -> dict[str, Any]:
        return self._values(self._specialists)

    def blueprints(self) -> list[Any]:
        return [value for _, value in sorted(self._values(self._blueprints).items())]

    def evals(self) -> list[Any]:
        return [value for _, value in sorted(self._values(self._evals).items())]

    def auxiliary_tasks(self) -> dict[str, str]:
        return self._values(self._auxiliary_tasks)

    def approval_transports(self) -> dict[str, Callable[..., Any]]:
        return self._values(self._approval_transports)

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return self._values(self._mcp_servers)

    def middleware(self, kind: str) -> tuple[Callable[..., Any], ...]:
        with self._lock:
            return tuple(callback for _, callback in self._middleware.get(kind, ()))

    # -- what a live session lends the plugins -----------------------------

    def bind_session(self, registry: dict[str, Any] | None, auxiliary: Any = None) -> None:
        """Lend the current session's registry and auxiliary client.

        Called by the surface once a session exists, and again with `None` when
        it ends. Held rather than passed because `ctx` is handed out at
        registration time, which is before any of this exists.
        """
        with self._lock:
            self._session_registry = registry
            self._auxiliary = auxiliary

    def session_registry(self) -> dict[str, Any] | None:
        with self._lock:
            return self._session_registry

    def auxiliary(self) -> Any:
        with self._lock:
            return self._auxiliary

    def prompt_sections(self) -> list[PromptSection]:
        with self._lock:
            return list(self._prompt_sections)

    def hooks_for(self, plugin_id: str) -> list[str]:
        with self._lock:
            return [event for event, _ in self._hooks_by_plugin.get(plugin_id, ())]

    # -- loading -----------------------------------------------------------

    def load(self, manifests: Mapping[str, PluginManifest] | None = None) -> None:
        """Import and register every enabled plugin, in dependency order."""
        if plugins_disabled():
            logger.debug("plugins are disabled by %s", ENV_DISABLE)
            return

        discovered = dict(manifests) if manifests is not None else discover()
        self.discovered = discovered

        enabled = {
            key: manifest
            for key, manifest in discovered.items()
            if plugin_store.is_enabled(key)
        }

        for plugin_id in resolve_load_order(enabled):
            self._load_one(enabled[plugin_id])

    def _load_one(self, manifest: PluginManifest) -> LoadedPlugin:
        entry = LoadedPlugin(manifest=manifest)
        self.loaded[manifest.id] = entry

        refusal = self._refusal_for(manifest)
        if refusal:
            entry.error = refusal
            logger.warning("not loading plugin %s: %s", manifest.id, refusal)
            return entry

        if manifest.portable:
            # No import, no `register()`, no module. The whole point of the
            # format is that this branch never executes anything the package
            # shipped, so it is the branch that has to stay free of one.
            return self._load_portable(manifest, entry)

        try:
            entry.module = self._import(manifest)
        except Exception as exc:  # noqa: BLE001 - one plugin must not stop the rest
            entry.error = f"import failed: {exc}"
            logger.warning("plugin %s failed to import: %s", manifest.id, exc)
            return entry

        register = getattr(entry.module, "register", None)
        if not callable(register):
            entry.error = (
                f"{manifest.directory}/__init__.py defines no `register(ctx)` "
                f"function, so there is nothing to call"
            )
            logger.warning("plugin %s: %s", manifest.id, entry.error)
            return entry

        try:
            register(PluginContext(manifest, self))
        except Exception as exc:  # noqa: BLE001 - same reason
            entry.error = f"register() raised: {exc}"
            logger.warning("plugin %s failed to register: %s", manifest.id, exc)
            return entry

        logger.debug("loaded plugin %s %s", manifest.id, manifest.version)
        return entry

    def _load_portable(self, manifest: PluginManifest, entry: LoadedPlugin) -> LoadedPlugin:
        """Register a code-free package's skills and MCP servers."""
        try:
            package = portable.load(manifest.directory)
        except portable.PortableError as exc:
            entry.error = str(exc)
            logger.warning("plugin %s: %s", manifest.id, exc)
            return entry

        entry.notes = [f"{note.scope}: {note.message}" for note in package.notes]
        for note in entry.notes:
            logger.info("plugin %s — %s", manifest.id, note)

        for skill in package.skills:
            self.add_skill(manifest.id, skill.name, skill.path, skill.description)
        for server_name, config in package.mcp_servers.items():
            self.add_mcp_server(manifest.id, server_name, config)

        if package.empty:
            # Not an error — an empty package is a package someone is still
            # writing — but said out loud, because "I installed it and nothing
            # happened" is otherwise unanswerable.
            entry.notes.append(
                "it carries no skills and no MCP servers, so enabling it does "
                "nothing yet"
            )
        return entry

    def _refusal_for(self, manifest: PluginManifest) -> str:
        """Why this plugin will not load, or an empty string.

        Checked *before* the import, which is the whole point: a plugin whose
        capability consent is stale must not run one line of its code first.
        """
        if manifest.api_version > SUPPORTED_API_VERSION:
            return (
                f"it declares api_version {manifest.api_version}, and this "
                f"install understands {SUPPORTED_API_VERSION}. Update Andromeda."
            )
        missing = manifest.missing_env()
        if missing:
            return f"these environment variables are not set: {', '.join(missing)}"
        ungranted = caps.needs_consent(manifest.id, manifest.capabilities)
        if ungranted:
            return (
                f"it declares capabilities that have not been granted: "
                f"{', '.join(ungranted)}. Run `andromeda plugins enable "
                f"{manifest.id}` to review them."
            )
        return ""

    def _import(self, manifest: PluginManifest) -> Any:
        """Import the plugin's package under a private namespace.

        The synthetic parent (`andromeda_plugins.<id>`) does two things: it
        keeps a plugin called `json` from shadowing the standard library, and
        it gives `unload` a prefix to sweep so a reload re-executes the module
        instead of finding a stale one in `sys.modules`.
        """
        if manifest.is_entrypoint:
            # `import importlib` here rather than at module scope would make
            # the name local to this whole function and shadow the
            # `importlib.util` bound at the top — which fails on the *other*
            # branch, several lines below, where nothing looks wrong.
            return importlib.import_module(manifest.module_name)

        _ensure_namespace_package()
        module_name = f"{_NS_PARENT}.{manifest.id.replace('-', '_').replace('.', '_')}"
        init = manifest.directory / "__init__.py"
        if not init.exists():
            raise PluginError(
                f"{manifest.directory} has a {MANIFEST_FILENAME} but no "
                f"__init__.py, so there is nothing to import"
            )

        spec = importlib.util.spec_from_file_location(
            module_name,
            init,
            submodule_search_locations=[str(manifest.directory)],
        )
        if spec is None or spec.loader is None:
            raise PluginError(f"could not build an import spec for {init}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            with _no_bytecode_cache():
                # Compiled from the bytes on disk rather than through
                # `exec_module`, which would consult `__pycache__`. Python's
                # staleness check is (mtime to the second, size), and a plugin
                # edit that changes neither — a one-word fix, an update that
                # lands a same-sized file — then loads the *previous* version
                # with nothing to indicate it. Found by a test that rewrote
                # 'one' as 'two'.
                source = init.read_bytes()
                code = compile(source, str(init), "exec")
                exec(code, module.__dict__)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    def unload(self) -> None:
        """Drop every registration and run each plugin's unload callbacks.

        Used by a config reload and by the suite. Callbacks run in reverse
        registration order — a plugin that opened A then B closes B then A —
        and a raising callback is logged rather than allowed to strand the
        rest.
        """
        for plugin_id, entry in list(self.loaded.items()):
            for callback in reversed(entry.unload_callbacks):
                try:
                    callback()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plugin %s unload callback raised: %s", plugin_id, exc
                    )

        # Off the hook bus before anything else is dropped. A callback left
        # registered would still fire, from a module that is about to be
        # removed from `sys.modules`.
        with self._lock:
            registered = list(self._hooks_by_plugin.items())
        for plugin_id, entries in registered:
            for event, callback in entries:
                try:
                    hooks_module.unregister(event, callback)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "could not unregister plugin %s's %s hook: %s",
                        plugin_id,
                        event,
                        exc,
                    )

        with self._lock:
            self.loaded.clear()
            self._tools.clear()
            self._commands.clear()
            self._cli_commands.clear()
            self._skills.clear()
            self._memory_backends.clear()
            self._cron_providers.clear()
            self._model_providers.clear()
            self._secret_sources.clear()
            self._delivery.clear()
            self._redaction.clear()
            self._web_search.clear()
            self._browser.clear()
            self._lsp_servers.clear()
            self._specialists.clear()
            self._blueprints.clear()
            self._evals.clear()
            self._auxiliary_tasks.clear()
            self._approval_transports.clear()
            self._mcp_servers.clear()
            self._middleware.clear()
            self._session_registry = None
            self._auxiliary = None
            self._prompt_sections.clear()
            self._subscribers.clear()
            self._hooks_by_plugin.clear()
            self.discovered.clear()

        # The redaction chokepoint holds a compiled union, not a reference to
        # the list above, so clearing the list is not enough to un-teach it.
        try:
            from . import redact

            redact.clear_plugin_patterns()
        except ImportError:  # pragma: no cover - half-installed package
            pass

        _clear_namespace_modules()


@contextmanager
def _no_bytecode_cache():
    """Do not write `__pycache__` while importing a plugin.

    Two reasons, and the second is the load-bearing one. A plugin directory is
    often somewhere the user did not expect writes — a read-only bundled tree,
    a checkout they are reviewing. And a pyc that exists is a pyc that can be
    read: submodules inside a plugin go through the ordinary loader, so leaving
    caches behind reintroduces the staleness the direct `compile` above exists
    to avoid, one level down.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


_NS_PARENT = "andromeda_plugins"


def _ensure_namespace_package() -> None:
    if _NS_PARENT in sys.modules:
        return
    import types

    package = types.ModuleType(_NS_PARENT)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[_NS_PARENT] = package


def _clear_namespace_modules() -> None:
    prefix = f"{_NS_PARENT}."
    for name in [key for key in sys.modules if key.startswith(prefix)]:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Module-level API — what the rest of the harness calls
# ---------------------------------------------------------------------------

_manager = PluginManager()


def manager() -> PluginManager:
    return _manager


def load(manifests: Mapping[str, PluginManifest] | None = None) -> None:
    _manager.load(manifests)


def reset() -> None:
    """Unload everything. Used by a reload and by every test that loads one."""
    _manager.unload()


def plugin_tool_specs() -> list[Any]:
    """Plugin-registered tools as `ToolSpec`s, ready to merge into a registry."""
    from andromeda_tools.spec import ToolSpec

    specs: list[ToolSpec] = []
    for registration in _manager.tools().values():
        specs.append(
            ToolSpec(
                name=registration.name,
                description=registration.description,
                parameters=registration.parameters,
                risk_tier=registration.risk_tier,  # type: ignore[arg-type]
                category=registration.category,  # type: ignore[arg-type]
                run=registration.run,
                summarize=registration.summarize,
            )
        )
    return specs


def plugin_commands() -> dict[str, CommandRegistration]:
    return _manager.commands()


def plugin_cli_commands() -> dict[str, CliCommandRegistration]:
    return _manager.cli_commands()


def plugin_skills() -> dict[str, dict[str, Any]]:
    return _manager.skills()


def memory_backends() -> dict[str, Callable[[Path], Any]]:
    return _manager.memory_backends()


def cron_providers() -> dict[str, Any]:
    return _manager.cron_providers()


def model_providers() -> dict[str, Callable[..., Any]]:
    return _manager.model_providers()


def secret_sources() -> dict[str, Callable[[str], Any]]:
    return _manager.secret_sources()


def delivery_modes() -> dict[str, Callable[..., bool]]:
    return _manager.delivery_modes()


def web_search_providers() -> dict[str, Callable[..., Any]]:
    return _manager.web_search_providers()


def browser_providers() -> dict[str, Callable[..., Any]]:
    return _manager.browser_providers()


def lsp_servers() -> list[Any]:
    return _manager.lsp_servers()


def specialists() -> dict[str, Any]:
    return _manager.specialists()


def blueprints() -> list[Any]:
    return _manager.blueprints()


def evals() -> list[Any]:
    return _manager.evals()


def auxiliary_tasks() -> dict[str, str]:
    return _manager.auxiliary_tasks()


def approval_transports() -> dict[str, Callable[..., Any]]:
    return _manager.approval_transports()


def mcp_servers() -> dict[str, dict[str, Any]]:
    return _manager.mcp_servers()


def middleware_for(kind: str) -> tuple[Callable[..., Any], ...]:
    return _manager.middleware(kind)


def bind_session(registry: dict[str, Any] | None, auxiliary: Any = None) -> None:
    _manager.bind_session(registry, auxiliary)


def render_prompt_sections() -> str:
    """The plugin block for the system prompt, or an empty string.

    Fenced by markers so the block can be found and replaced without a diff
    against the rest of the prompt, and sorted so the same set of plugins
    produces the same bytes on every machine — an unstable prompt prefix is a
    cache miss on every request.
    """
    sections = sorted(
        _manager.prompt_sections(), key=lambda item: (item.plugin_id, item.section_id)
    )
    if not sections:
        return ""
    lines = [PROMPT_SECTIONS_START]
    for section in sections:
        lines.append(f"## Plugin context: {section.plugin_id}:{section.section_id}")
        lines.append(section.content)
        lines.append("")
    lines.append(PROMPT_SECTIONS_END)
    return "\n".join(lines).strip() + "\n"
