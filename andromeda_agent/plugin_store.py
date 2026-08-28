"""Where the plugin ledger lives.

One file, `<home>/plugins.json`, holding everything about a plugin that is a
*decision* rather than a fact about its code: whether it is enabled, which
capabilities the user consented to, and the plugin's own settings.

It is deliberately not `config.yaml`. Three reasons, in order of how much they
cost when ignored:

  1. `config.set_value` writes flat keys and validates against `DEFAULTS`.
     Teaching it nested `plugins.entries.<id>.granted_capabilities` paths would
     make every other setting pay for a feature only this one needs.
  2. Consent is read-modify-write from more than one process — two terminals
     starting at once is the ordinary case — so it needs the same `flock`
     discipline `shell_hooks` uses for its allowlist. `config.yaml` has none.
  3. A person is invited to hand-edit `config.yaml`. A grant record carries a
     hash of exactly what was consented to; an edited hash is a consent record
     that no longer describes a decision anyone made.

The shape::

    {
      "entries": {
        "<plugin id>": {
          "enabled": true,
          "granted_capabilities": ["tools.override"],
          "capability_hash": "sha256:…",
          "granted_at": "2026-08-26T…Z",
          "source": "user",
          "installed_at": "…",
          "ref": "<commit sha, when installed from git>",
          "config": {"any": "plugin-owned settings"}
        }
      }
    }

Every read tolerates a corrupt or absent file by returning an empty ledger.
That is the safe direction: an unreadable ledger means *nothing is enabled and
nothing is consented to*, which fails closed on both axes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - POSIX only, and every target platform has it
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "plugins.json"

_write_lock = threading.RLock()


def ledger_path() -> Path:
    # Imported here rather than at module scope: `andromeda_cli.config` imports
    # from `andromeda_agent` for the model lock, and a top-level import would
    # close the cycle.
    from andromeda_cli import config as config_module

    return config_module.home() / LEDGER_FILENAME


def empty() -> dict[str, Any]:
    return {"entries": {}}


def load() -> dict[str, Any]:
    """The ledger, or an empty one.

    A malformed file is not an error to the caller. It is reported once, in the
    log, and read as "nothing enabled, nothing granted" — the only reading that
    cannot turn a broken file into an accidental grant.
    """
    try:
        raw = json.loads(ledger_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "could not read the plugin ledger at %s (%s); treating every "
            "plugin as disabled and unconsented until it is fixed",
            ledger_path(),
            exc,
        )
        return empty()

    if not isinstance(raw, dict):
        return empty()
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raw["entries"] = {}
    else:
        # A non-mapping entry is dropped rather than repaired: there is no
        # honest way to read a grant out of a list.
        raw["entries"] = {
            key: value for key, value in entries.items() if isinstance(value, dict)
        }
    return raw


def save(data: dict[str, Any]) -> None:
    """Write through a temp file and a rename.

    A crash mid-write must not leave a truncated ledger, because a truncated
    ledger reads as "nothing is enabled" and silently takes away every plugin
    the user installed.
    """
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "could not write the plugin ledger to %s: %s. The change holds for "
            "this run and is lost at the next start.",
            path,
            exc,
        )


@contextmanager
def locked() -> Iterator[dict[str, Any]]:
    """Serialise read-modify-write across processes.

    Without this, enabling two plugins in two terminals loses one of them: both
    read the same ledger, both write their own version, last writer wins.
    """
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover - non-POSIX
        with _write_lock:
            data = load()
            yield data
            save(data)
        return

    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = load()
            yield data
            save(data)
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def entry(plugin_id: str) -> dict[str, Any]:
    """One plugin's ledger row, or an empty one. Never None — callers read
    fields off it directly and a None would turn every read site into a guard."""
    found = load()["entries"].get(plugin_id)
    return found if isinstance(found, dict) else {}


def update(plugin_id: str, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into one plugin's row and persist. Returns the new row."""
    with locked() as data:
        row = data["entries"].setdefault(plugin_id, {})
        row.update(fields)
        return dict(row)


def remove(plugin_id: str) -> bool:
    """Drop a plugin's row entirely. True when there was one."""
    with locked() as data:
        return data["entries"].pop(plugin_id, None) is not None


def is_enabled(plugin_id: str) -> bool:
    """Enabled only when the ledger says so, explicitly.

    The default is False and stays False. An installed-but-never-enabled plugin
    is Python that has not been imported, which is the whole point of asking:
    consent happens before code runs, not after it has already run once.
    """
    return entry(plugin_id).get("enabled") is True


def enabled_ids() -> set[str]:
    return {
        key
        for key, row in load()["entries"].items()
        if isinstance(row, dict) and row.get("enabled") is True
    }


def plugin_config(plugin_id: str) -> dict[str, Any]:
    value = entry(plugin_id).get("config")
    return dict(value) if isinstance(value, dict) else {}


def set_plugin_config(plugin_id: str, key: str, value: Any) -> None:
    with locked() as data:
        row = data["entries"].setdefault(plugin_id, {})
        settings = row.get("config")
        if not isinstance(settings, dict):
            settings = {}
        settings[key] = value
        row["config"] = settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
