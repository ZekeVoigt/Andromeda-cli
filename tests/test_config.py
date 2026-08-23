from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from andromeda_cli import config as config_module


def test_defaults_when_no_file():
    values = config_module.load()
    assert values["provider"] == "relay"
    assert values["model"] == "deepseek/deepseek-v4-flash-0731"


def test_file_overrides_default_and_env_overrides_file(monkeypatch):
    config_module.set_value("temperature", "0.1")
    assert config_module.load()["temperature"] == 0.1

    monkeypatch.setenv("ANDROMEDA_TEMPERATURE", "0.9")
    assert config_module.load()["temperature"] == 0.9


def test_numeric_settings_keep_their_type(monkeypatch):
    config_module.set_value("max_tokens", "1024")
    assert config_module.load()["max_tokens"] == 1024

    monkeypatch.setenv("ANDROMEDA_MAX_TOKENS", "2048")
    assert config_module.load()["max_tokens"] == 2048


def test_bad_numeric_value_is_rejected():
    with pytest.raises(config_module.ConfigError):
        config_module.set_value("max_tokens", "lots")


def test_unknown_key_is_rejected():
    with pytest.raises(config_module.ConfigError):
        config_module.set_value("nope", "1")


def test_credentials_are_written_owner_only():
    path = config_module.save_credentials(
        config_module.Credentials(
            device_token="t" * 64,
            device_id="cli-abc",
            user_id="user_1",
            base_url="https://example.test",
        )
    )
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"credentials file is {oct(mode)}, must be 0600"

    loaded = config_module.load_credentials()
    assert loaded.paired
    assert loaded.user_id == "user_1"


def test_corrupt_credentials_read_as_not_paired():
    config_module.credentials_path().parent.mkdir(parents=True, exist_ok=True)
    config_module.credentials_path().write_text("{not json", encoding="utf-8")
    assert config_module.load_credentials().paired is False


def test_missing_credentials_read_as_not_paired():
    assert config_module.load_credentials().paired is False


def test_the_default_home_is_not_the_desktop_apps_directory(monkeypatch):
    """`~/.andromeda` holds the desktop app's sqlite, vault key and profiles.

    Clearing that directory must not take the CLI's sessions and memory with it.
    """
    monkeypatch.delenv(config_module.ENV_HOME, raising=False)
    default = config_module.home()

    assert default.name == ".andromeda-cli"
    assert default != Path.home() / ".andromeda"
    # And not nested inside it either — the installer's checkout lives there.
    assert (Path.home() / ".andromeda") not in default.parents


def test_the_home_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_HOME, str(tmp_path / "elsewhere"))
    assert config_module.home() == tmp_path / "elsewhere"
