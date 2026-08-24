"""On-disk state: configuration and credentials.

Two files, deliberately separate:

  ``config.yaml``       non-secret preferences, safe to read, print and commit
                        to a dotfiles repo.
  ``credentials.json``  the device token. Written 0600 and never echoed by any
                        command, including ``config get``.

Splitting them is what lets ``andromeda config get`` dump the whole config
without a redaction pass — there is nothing secret in it to redact.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from andromeda_tools import DEFAULT_ENABLED

from . import profiles

ENV_HOME = "ANDROMEDA_HOME"

DEFAULTS: dict[str, Any] = {
    # `relay` routes through the hosted endpoint, which holds the provider key
    # and spends credit. `direct` is BYOK against any OpenAI-compatible base.
    "provider": "relay",
    "base_url": "https://ai-andromeda.com",
    "model": "deepseek/deepseek-v4-flash-0731",
    "max_tokens": 8192,
    # The model's context window. Compaction starts at 75% of it. 0 means
    # "look it up from the model", which is the right answer unless you are
    # deliberately shrinking it to test compaction.
    "context_window": 0,
    "temperature": 0.7,
    # BYOK only. Ignored on the relay lane, which never sees a client key.
    "direct_base_url": "https://openrouter.ai/api/v1",
    "direct_api_key_env": "OPENROUTER_API_KEY",
    # `ask` stops before anything that changes the machine. `auto` does not ask
    # at all; `deny` refuses every tool. Defaulting to `ask` is the whole point
    # of a harness that can reach a real filesystem.
    "approval_mode": "ask",
    # Which interactive surface a bare `andromeda` opens. `repl` is the
    # line-based one; `tui` is the full-screen interface. The default stays
    # `repl` deliberately — the TUI takes over the whole terminal and clears it
    # on exit, and a surface that arrives unannounced in an update is a
    # surface people fight rather than use. `--tui` and `--no-tui` override it
    # per invocation, and neither applies to a one-shot or a pipe.
    "interface": "repl",
    # How hard the model thinks before answering. `off` sends no reasoning
    # field at all, which is not the same as asking for minimal effort — right
    # for a one-line question, wrong for a refactor. Reasoning is billed as
    # output, so the higher levels cost real money and real time.
    "thinking": "off",
    # The ceiling. Nothing above this tier runs at any mode, with any grant.
    "max_tier": "destructive",
    "enabled_tools": list(DEFAULT_ENABLED),
    # Who decides a job is due. `built-in` is the tick loop in `andromeda cron
    # daemon`. An unrecognised name falls back to it rather than refusing to
    # start — a typo here must not silently stop every scheduled job.
    "cron_provider": "built-in",
    # Where memories are kept. `json` is one readable file; `sqlite` puts them
    # in the state index and finds recall candidates through FTS, which only
    # starts to matter past a few thousand of them. An unrecognised name falls
    # back to `json` for the same reason `cron_provider` does — a typo in a
    # setting must not take away the agent's memory.
    "memory_backend": "json",
    # Off by default. The URL a fetch or a navigation uses comes from the
    # model, and this machine sits inside the user's own network — a metadata
    # endpoint, a router page, a service bound to localhost. Turning it on is
    # for working against a local development server, deliberately.
    "allow_private_network": False,
}


def home() -> Path:
    """Where the CLI keeps config, credentials, sessions and memory.

    Deliberately NOT `~/.andromeda`: that belongs to the desktop app — its
    sqlite databases, gateway store, local secret vault and browser profiles
    live there. Writing session transcripts and a device token into another
    program's data directory invites exactly one bug, and it is the expensive
    kind: someone clears the desktop app's state and silently loses every CLI
    session and every stored memory with it.

    Everything the CLI owns lives under one root of its own — config,
    credentials, sessions, memory, and the installer's checkout.

    Which root, when there is more than one, is `profiles`' answer: an
    explicit `ANDROMEDA_HOME` first, then a named profile, then the default —
    which is this directory itself, so an install that has never heard of
    profiles resolves exactly where it always did.
    """
    return profiles.home()


def config_path() -> Path:
    return home() / "config.yaml"


def credentials_path() -> Path:
    return home() / "credentials.json"


class ConfigError(RuntimeError):
    pass


def _ensure_home() -> Path:
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    return root


def load() -> dict[str, Any]:
    """Config as defaults overlaid with the user's file, then the environment.

    Environment wins so a CI job or a one-off shell can override without
    mutating the user's file: ``ANDROMEDA_MODEL=... andromeda``.
    """
    values = dict(DEFAULTS)

    path = config_path()
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a mapping, not {type(loaded).__name__}.")
        values.update(loaded)

    for key in DEFAULTS:
        env_value = os.environ.get(f"ANDROMEDA_{key.upper()}")
        if env_value is not None and env_value.strip():
            values[key] = _coerce(key, env_value.strip())

    # Validated on the way out, not only on the way in: a hand-edited file, an
    # env var and a `--model` flag all bypass `set_value`.
    for key in VALID_VALUES:
        validate(key, values[key])
    validate("model", values["model"])

    return values


def _coerce(key: str, raw: str) -> Any:
    """Match the default's type, so `max_tokens` from a file or env is an int."""
    default = DEFAULTS.get(key)
    if isinstance(default, list):
        # Comma-separated on the command line and in the environment; a real
        # YAML list in the file. Both land as a list of names.
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(default, bool):
        return raw.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a whole number, got {raw!r}.") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number, got {raw!r}.") from exc
    return raw


VALID_VALUES: dict[str, tuple[str, ...]] = {
    "provider": ("relay", "direct"),
    "interface": ("repl", "tui"),
    "thinking": ("off", "low", "medium", "high"),
    "approval_mode": ("auto", "ask", "deny"),
    "max_tier": ("safe_local", "outbound", "destructive", "irreversible"),
    "memory_backend": ("json", "sqlite"),
}


def validate(key: str, value: Any) -> Any:
    """Reject a value the gate would otherwise read as something permissive.

    A misspelled `approval_mode: "sk"` must not quietly become "not ask".
    """
    allowed = VALID_VALUES.get(key)
    if allowed and value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)} — got {value!r}.")
    if key == "enabled_tools" and not isinstance(value, list):
        raise ConfigError("enabled_tools must be a list of tool names.")
    if key == "model":
        # Imported here, not at module scope: `andromeda_agent` imports this
        # module for the home directory, and a top-level import would close the
        # cycle.
        from andromeda_agent.models import is_allowed, refusal

        if not is_allowed(value):
            raise ConfigError(refusal(value))
    return value


def set_value(key: str, raw: str) -> Any:
    if key not in DEFAULTS:
        known = ", ".join(sorted(DEFAULTS))
        raise ConfigError(f"Unknown setting {key!r}. Known settings: {known}")
    value = validate(key, _coerce(key, raw))

    _ensure_home()
    path = config_path()
    current: dict[str, Any] = {}
    if path.exists():
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    current[key] = value
    path.write_text(yaml.safe_dump(current, sort_keys=True), encoding="utf-8")
    return value


@dataclass
class Credentials:
    device_token: str = ""
    device_id: str = ""
    user_id: str = ""
    base_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def paired(self) -> bool:
        return bool(self.device_token and self.device_id)


def load_credentials() -> Credentials:
    path = credentials_path()
    if not path.exists():
        return Credentials()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt credentials file reads as "not signed in" rather than
        # crashing every command: `auth login` is then the obvious repair, and
        # it overwrites the file.
        return Credentials()
    if not isinstance(raw, dict):
        return Credentials()
    known = {"device_token", "device_id", "user_id", "base_url"}
    return Credentials(
        device_token=str(raw.get("device_token") or ""),
        device_id=str(raw.get("device_id") or ""),
        user_id=str(raw.get("user_id") or ""),
        base_url=str(raw.get("base_url") or ""),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def save_credentials(credentials: Credentials) -> Path:
    _ensure_home()
    path = credentials_path()
    payload = {
        "device_token": credentials.device_token,
        "device_id": credentials.device_id,
        "user_id": credentials.user_id,
        "base_url": credentials.base_url,
        **credentials.extra,
    }
    # Create with 0600 already set. Writing then chmod'ing leaves a window where
    # the token is world-readable on a shared machine.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def clear_credentials() -> bool:
    path = credentials_path()
    if not path.exists():
        return False
    path.unlink()
    return True
