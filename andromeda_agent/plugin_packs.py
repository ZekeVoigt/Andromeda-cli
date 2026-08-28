"""A set of plugins as one file.

A pack is a single YAML file naming plugins and pinning each to an exact
commit. Installing one is nothing new at runtime — it fans out to N ordinary
installs through the same path `plugins install` uses, and then seeds each
plugin's own settings.

    name: writing-desk
    description: Everything I want on a machine I write on.
    author: zeke
    version: 1.0.0
    plugins:
      - name: wordcount                    # an index name…
        ref: 4f1c2b9a8e7d6c5b4a3928170615243342516070
      - repo: owner/thesaurus              # …or a repo, or a git URL
        ref: 8f3c2d1a9b4e5f6071829304a5b6c7d8e9f00112
    config:                                # optional, non-secret settings
      wordcount:
        target: 800

Supply chain
------------
**Every entry must pin a 40-character commit SHA.** Tags and branch names are
refused by name, so the refusal says which entry and why. A pack whose entries
moved would be a pack that installs different code tomorrow under the same
description — which is the one thing a pack is supposed to prevent.

**A pack can never grant a capability.** After each plugin installs, its
declared capabilities go through the identical per-plugin consent flow. This
is the rule the format exists to protect: a file that could pre-approve
`tools.override` for five plugins would make consent a formality that arrives
after the decision, which is worse than not asking.

**`config:` seeds cannot carry secrets.** Keys whose names look like
credentials are refused. A pack is a thing people share; a pack with a token
in it is a token in a repository, and the format should not make that the
convenient path.

What a pack is not
------------------
It is not a lockfile for a dependency graph, and it does not resolve
`requires_plugins`. It is a list somebody wrote down. If an entry depends on a
plugin the pack does not name, the load-order warning says so at startup and
`ctx.has_plugin` is how the plugin itself copes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Key names that must not appear in a pack's `config:` block. Matched as a
#: substring on the lowercased key, so `openai_api_key` and `apiKey` both fail.
SECRET_KEYWORDS = (
    "secret",
    "token",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
    "private_key",
    "access_key",
)

#: Ledger fields a pack may never write. `config:` is for a plugin's own
#: settings; these decide trust, and a file cannot decide trust.
FORBIDDEN_CONFIG_KEYS = frozenset(
    {"granted_capabilities", "capability_hash", "granted_at", "enabled", "source"}
)

PACK_FILENAME = "andromeda-pack.yaml"


class PackError(ValueError):
    """A pack file that cannot be trusted or cannot be read."""


@dataclass(frozen=True)
class PackEntry:
    """One plugin in a pack. Exactly one of `name` or `repo` is the source."""

    ref: str
    name: str = ""
    repo: str = ""
    subdir: str = ""

    @property
    def source(self) -> str:
        """What `plugins install` should be handed."""
        return self.repo or self.name

    @property
    def label(self) -> str:
        return self.name or self.repo


@dataclass(frozen=True)
class Pack:
    name: str
    entries: list[PackEntry]
    description: str = ""
    author: str = ""
    version: str = "0.0.0"
    config: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse(raw: Any, *, origin: str = "") -> Pack:
    """Validate pack data. Raises `PackError` with the offending entry named.

    Strict, unlike the manifest parser, and the difference is deliberate. A
    manifest describes a plugin you already decided to install; a pack is a
    file that decides *what* to install, so a field it got wrong is a field
    that installs the wrong thing. Nothing here degrades.
    """
    where = f"{origin}: " if origin else ""
    if not isinstance(raw, dict):
        raise PackError(f"{where}a pack is a mapping, not {type(raw).__name__}")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise PackError(f"{where}the pack has no `name:`")

    rows = raw.get("plugins")
    if not isinstance(rows, list) or not rows:
        raise PackError(f"{where}the pack lists no plugins")

    entries: list[PackEntry] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise PackError(f"{where}entry {index} is not a mapping")

        entry_name = str(row.get("name") or "").strip().lower()
        repo = str(row.get("repo") or "").strip()
        if not entry_name and not repo:
            raise PackError(f"{where}entry {index} names neither a `name` nor a `repo`")
        if entry_name and repo:
            raise PackError(
                f"{where}entry {index} names both a `name` and a `repo`; pick "
                f"one, because they resolve to different places"
            )
        if entry_name and not NAME_RE.match(entry_name):
            raise PackError(f"{where}entry {index} has an unusable name {entry_name!r}")

        ref = str(row.get("ref") or "").strip().lower()
        if not SHA_RE.match(ref):
            label = entry_name or repo
            raise PackError(
                f"{where}{label} pins {ref!r}, which is not a 40-character "
                f"commit SHA. A tag can be moved and a branch head moves by "
                f"definition, so a pack that named one would install different "
                f"code tomorrow under the same description."
            )

        label = entry_name or repo
        if label in seen:
            raise PackError(f"{where}{label} is listed twice")
        seen.add(label)

        entries.append(
            PackEntry(
                ref=ref,
                name=entry_name,
                repo=repo,
                subdir=str(row.get("subdir") or "").strip(),
            )
        )

    config = _validated_config(raw.get("config"), where)

    return Pack(
        name=name,
        entries=entries,
        description=str(raw.get("description") or "").strip(),
        author=str(raw.get("author") or "").strip(),
        version=str(raw.get("version") or "0.0.0").strip() or "0.0.0",
        config=config,
    )


def _validated_config(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PackError(f"{where}`config:` must be a mapping of plugin id to settings")

    validated: dict[str, dict[str, Any]] = {}
    for plugin_id, settings in raw.items():
        key = str(plugin_id).strip().lower()
        if not NAME_RE.match(key):
            raise PackError(f"{where}`config:` names an unusable plugin id {plugin_id!r}")
        if not isinstance(settings, dict):
            raise PackError(f"{where}`config.{key}` must be a mapping")
        for setting, value in settings.items():
            name = str(setting)
            lowered = name.lower().replace("-", "_")
            if name in FORBIDDEN_CONFIG_KEYS:
                raise PackError(
                    f"{where}`config.{key}.{name}` is a trust decision, and a "
                    f"pack file cannot make one. Capabilities are granted by "
                    f"the person installing, per plugin."
                )
            if any(word in lowered.replace("_", "") or word in lowered for word in SECRET_KEYWORDS):
                raise PackError(
                    f"{where}`config.{key}.{name}` looks like a credential. A "
                    f"pack is a file people share, so it must not be the "
                    f"convenient place to put one — set it in the environment "
                    f"or a `secrets:` reference instead."
                )
            validated.setdefault(key, {})[name] = value
    return validated


def load(path: Path) -> Pack:
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackError(f"{target} does not exist") from exc
    except yaml.YAMLError as exc:
        raise PackError(f"{target} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise PackError(f"{target} could not be read: {exc}") from exc
    return parse(raw, origin=str(target))


def export(name: str, description: str = "") -> dict[str, Any]:
    """A pack describing what is installed and enabled here.

    Only plugins installed from a recorded origin *at a recorded commit* are
    included. A plugin installed from a local directory has no SHA to pin, and
    exporting it with a placeholder would produce a pack that fails validation
    on the machine it was shared with — better to leave it out and say so.
    """
    from . import plugin_store, plugins as plugins_module

    entries: list[dict[str, Any]] = []
    skipped: list[str] = []
    config: dict[str, dict[str, Any]] = {}

    discovered = plugins_module.discover()
    for plugin_id in sorted(discovered):
        if not plugin_store.is_enabled(plugin_id):
            continue
        row = plugin_store.entry(plugin_id)
        ref = str(row.get("ref") or "").strip().lower()
        origin = str(row.get("origin") or "").strip()
        if not SHA_RE.match(ref) or not origin:
            skipped.append(plugin_id)
            continue
        entry: dict[str, Any] = {"ref": ref}
        if "/" in origin or "://" in origin:
            entry["repo"] = origin
        else:
            entry["name"] = plugin_id
        entries.append(entry)

        settings = plugin_store.plugin_config(plugin_id)
        safe = {
            key: value
            for key, value in settings.items()
            if key not in FORBIDDEN_CONFIG_KEYS
            and not any(word in str(key).lower().replace("-", "_") for word in SECRET_KEYWORDS)
        }
        if safe:
            config[plugin_id] = safe

    document: dict[str, Any] = {"name": name, "version": "1.0.0", "plugins": entries}
    if description:
        document["description"] = description
    if config:
        document["config"] = config
    if skipped:
        # Named in the document, not just in the terminal. Whoever reads the
        # file later is not necessarily whoever exported it.
        document["$skipped"] = (
            "Not exportable — installed without a pinned commit: "
            + ", ".join(skipped)
        )
    return document


def to_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
