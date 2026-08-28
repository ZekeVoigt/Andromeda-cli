"""Reading and writing `mcp.json` without losing what is already in it.

`mcp.py` reads this file to build servers. This module is the other half: the
commands that put a server *into* it, so connecting one is something a person
does with a sentence rather than an editor.

Two rules shape everything here.

**Never rewrite what was not asked about.** The file belongs to the user. It may
carry comments-as-keys, servers this version has never heard of, a `disabled`
flag someone set on purpose, an editor's own block. Every write reads the whole
document, changes exactly one key under `mcpServers`, and writes the rest back
byte-for-byte as parsed. A tool that reformats a config file on every touch is a
tool people stop letting near their config file.

**Never write a server that would be refused at load.** `mcp_security.screen`
runs before the write, not after, so a refusal is a message about a command you
just typed rather than a mystery on next launch.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import mcp_security

# The two spellings in the wild. `mcpServers` is what every other client writes
# and what we write; `mcp_servers` is accepted so a config can be pasted in from
# a harness that chose the other one without anybody editing it first.
KEYS = ("mcpServers", "mcp_servers")


def path(home: Path) -> Path:
    return home / "mcp.json"


class ConfigError(RuntimeError):
    """Something about the file itself, said in a sentence a person can act on."""


def read(home: Path) -> dict[str, Any]:
    """The whole document, or an empty one.

    A missing file is not an error — it is the state every install starts in.
    A *malformed* file is, and it raises rather than being silently replaced:
    overwriting a file somebody hand-edited into invalidity would destroy the
    thing they were in the middle of writing.
    """
    target = path(home)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{target} is not valid JSON ({exc.msg}, line {exc.lineno}). "
            f"Fix or move it — it is not going to be overwritten."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{target} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} should hold a JSON object, not a {type(raw).__name__}.")
    return raw


def _servers_key(document: dict[str, Any]) -> str:
    """Which spelling this document already uses, defaulting to the common one."""
    for key in KEYS:
        if isinstance(document.get(key), dict):
            return key
    return KEYS[0]


def servers(home: Path) -> dict[str, dict[str, Any]]:
    """Every configured server, including ones marked `disabled`.

    `mcp.py:load_config` filters those out because it is about to connect them.
    This is about *managing* them, and a server you cannot see is a server you
    cannot re-enable.
    """
    document = read(home)
    found = document.get(_servers_key(document))
    if not isinstance(found, dict):
        return {}
    return {str(name): entry for name, entry in found.items() if isinstance(entry, dict)}


def _write(home: Path, document: dict[str, Any]) -> None:
    """Atomically, and with the private bit set.

    Servers carry bearer tokens and API keys in `headers` and `env`. A partial
    write here leaves a config that fails to parse on next launch, and a
    world-readable one leaves credentials in a file anybody on the box can read.
    """
    target = path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), prefix=".mcp.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def save(home: Path, name: str, entry: dict[str, Any]) -> None:
    """Put one server in the file, screening it first.

    Raises `ConfigError` with the reasons if the entry is one of the shapes
    `mcp_security` refuses, and writes nothing.
    """
    issues = mcp_security.screen(name, entry)
    if issues:
        raise ConfigError("\n".join(issues))

    document = read(home)
    key = _servers_key(document)
    existing = document.get(key)
    document[key] = dict(existing) if isinstance(existing, dict) else {}
    document[key][name] = entry
    _write(home, document)


def remove(home: Path, name: str) -> bool:
    """Drop a server. False if there was no such server, which is not an error."""
    document = read(home)
    key = _servers_key(document)
    found = document.get(key)
    if not isinstance(found, dict) or name not in found:
        return False
    found = dict(found)
    found.pop(name)
    document[key] = found
    _write(home, document)
    return True


def update(home: Path, name: str, **fields: Any) -> bool:
    """Change some keys of an existing server, leaving the rest alone.

    A `None` value removes the key rather than writing a null — `update(home,
    "x", disabled=None)` is how a server gets re-enabled without leaving a
    `"disabled": null` behind for the next reader to puzzle over.
    """
    current = servers(home).get(name)
    if current is None:
        return False
    entry = dict(current)
    for field, value in fields.items():
        if value is None:
            entry.pop(field, None)
        else:
            entry[field] = value
    save(home, name, entry)
    return True


def exists(home: Path, name: str) -> bool:
    return name in servers(home)


def parse_env(assignments: list[str] | None) -> dict[str, str]:
    """`["A=1", "B=2"]` as a dict.

    A bare name with no `=` is taken as "pass this through from my own
    environment", because that is what somebody typing `--env HOME` means, and
    the alternative — refusing, or writing an empty string — is worse than
    either reading of it.
    """
    out: dict[str, str] = {}
    for raw in assignments or []:
        text = str(raw)
        if "=" in text:
            key, _, value = text.partition("=")
            key = key.strip()
            if not key:
                raise ConfigError(f"`{text}` has no variable name before the `=`.")
            out[key] = value
        else:
            key = text.strip()
            if not key:
                continue
            inherited = os.environ.get(key)
            if inherited is None:
                raise ConfigError(
                    f"`{key}` is not set in this shell, so there is nothing to "
                    f"pass through. Write `{key}=<value>` to set it explicitly."
                )
            out[key] = inherited
    return out
