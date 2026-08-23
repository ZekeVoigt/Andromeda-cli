"""Backup, export and restore.

The property that matters most: `export` must never contain a credential.
Everything else here is plumbing; that one is the reason the two verbs exist
separately rather than as one flagged command.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from andromeda_cli import config as config_module
from andromeda_cli.commands import transfer


@pytest.fixture
def populated_home():
    home = config_module.home()
    home.mkdir(parents=True, exist_ok=True)
    config_module.set_value("temperature", "0.42")
    config_module.save_credentials(
        config_module.Credentials(
            device_token="SECRET" * 10,
            device_id="cli-1",
            user_id="user_9",
            base_url="https://x",
        )
    )
    (home / "memory").mkdir(exist_ok=True)
    (home / "memory" / "memories.json").write_text('[{"content": "a fact"}]', encoding="utf-8")
    (home / "sessions").mkdir(exist_ok=True)
    (home / "sessions" / "abc.json").write_text('{"id": "abc"}', encoding="utf-8")
    (home / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    return home


def names_in(archive_path: Path) -> set[str]:
    with tarfile.open(archive_path, "r:gz") as archive:
        return {member.name for member in archive.getmembers()}


def body_of(archive_path: Path) -> str:
    with tarfile.open(archive_path, "r:gz") as archive:
        parts = []
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                if handle is not None:
                    parts.append(handle.read().decode("utf-8", errors="replace"))
    return "\n".join(parts)


class TestExport:
    def test_it_contains_the_portable_state(self, populated_home, tmp_path):
        target = tmp_path / "export.tar.gz"
        assert transfer.export(str(target)) == 0
        names = names_in(target)
        assert "config.yaml" in names and "memory" in names and "sessions" in names

    def test_it_never_contains_credentials(self, populated_home, tmp_path):
        """The reason `export` and `backup` are separate verbs."""
        target = tmp_path / "export.tar.gz"
        transfer.export(str(target))

        assert "credentials.json" not in names_in(target)
        assert "SECRET" not in body_of(target)

    def test_the_manifest_records_that(self, populated_home, tmp_path):
        target = tmp_path / "export.tar.gz"
        transfer.export(str(target))
        with tarfile.open(target, "r:gz") as archive:
            manifest = json.loads(archive.extractfile(transfer.MANIFEST).read())
        assert manifest["includesCredentials"] is False

    def test_it_says_credentials_were_excluded(self, populated_home, tmp_path, capsys):
        transfer.export(str(tmp_path / "export.tar.gz"))
        assert "Credentials excluded" in capsys.readouterr().out


class TestBackup:
    def test_it_contains_credentials(self, populated_home, tmp_path):
        target = tmp_path / "backup.tar.gz"
        assert transfer.backup(str(target)) == 0
        assert "credentials.json" in names_in(target)

    def test_it_warns_that_it_does(self, populated_home, tmp_path, capsys):
        transfer.backup(str(tmp_path / "backup.tar.gz"))
        assert "like a password" in capsys.readouterr().out

    def test_an_unpaired_backup_does_not_claim_to_hold_a_token(self, tmp_path, capsys):
        """A warning that fires when it should not is a warning nobody reads."""
        config_module.set_value("temperature", "0.5")
        transfer.backup(str(tmp_path / "backup.tar.gz"))
        printed = capsys.readouterr().out
        assert "like a password" not in printed
        assert "not paired" in printed

    def test_the_manifest_of_an_unpaired_backup_says_so(self, tmp_path):
        config_module.set_value("temperature", "0.5")
        target = tmp_path / "backup.tar.gz"
        transfer.backup(str(target))
        with tarfile.open(target, "r:gz") as archive:
            manifest = json.loads(archive.extractfile(transfer.MANIFEST).read())
        assert manifest["includesCredentials"] is False

    def test_the_manifest_records_that(self, populated_home, tmp_path):
        target = tmp_path / "backup.tar.gz"
        transfer.backup(str(target))
        with tarfile.open(target, "r:gz") as archive:
            manifest = json.loads(archive.extractfile(transfer.MANIFEST).read())
        assert manifest["includesCredentials"] is True


class TestRestore:
    def test_a_backup_round_trips(self, populated_home, tmp_path, monkeypatch):
        target = tmp_path / "backup.tar.gz"
        transfer.backup(str(target))

        fresh = tmp_path / "fresh-home"
        monkeypatch.setenv(config_module.ENV_HOME, str(fresh))

        assert transfer.restore(str(target)) == 0
        assert config_module.load()["temperature"] == 0.42
        assert config_module.load_credentials().user_id == "user_9"
        assert (fresh / "memory" / "memories.json").exists()

    def test_an_export_restores_without_credentials(self, populated_home, tmp_path, monkeypatch):
        target = tmp_path / "export.tar.gz"
        transfer.export(str(target))

        fresh = tmp_path / "fresh-home"
        monkeypatch.setenv(config_module.ENV_HOME, str(fresh))

        assert transfer.restore(str(target)) == 0
        assert config_module.load_credentials().paired is False

    def test_it_says_to_pair_again(self, populated_home, tmp_path, monkeypatch, capsys):
        target = tmp_path / "export.tar.gz"
        transfer.export(str(target))
        monkeypatch.setenv(config_module.ENV_HOME, str(tmp_path / "fresh"))
        transfer.restore(str(target))
        assert "auth login" in capsys.readouterr().out

    def test_it_refuses_to_overwrite_by_default(self, populated_home, tmp_path, capsys):
        target = tmp_path / "backup.tar.gz"
        transfer.backup(str(target))
        assert transfer.restore(str(target)) == 2
        assert "already has" in capsys.readouterr().err

    def test_force_overwrites(self, populated_home, tmp_path):
        target = tmp_path / "backup.tar.gz"
        transfer.backup(str(target))
        assert transfer.restore(str(target), force=True) == 0

    def test_a_missing_archive_is_a_usage_error(self, tmp_path):
        assert transfer.restore(str(tmp_path / "nope.tar.gz")) == 2

    def test_a_corrupt_archive_is_reported_not_raised(self, tmp_path, monkeypatch):
        broken = tmp_path / "broken.tar.gz"
        broken.write_bytes(b"not a tarball")
        monkeypatch.setenv(config_module.ENV_HOME, str(tmp_path / "fresh"))
        assert transfer.restore(str(broken)) == 1


class TestPathTraversal:
    def test_a_member_escaping_the_home_is_dropped(self, tmp_path, monkeypatch):
        """Extracting an archive someone handed you is exactly the situation."""
        malicious = tmp_path / "evil.tar.gz"
        outside = tmp_path / "outside.txt"
        outside.write_text("original", encoding="utf-8")

        with tarfile.open(malicious, "w:gz") as archive:
            archive.add(outside, arcname="../../outside.txt")

        fresh = tmp_path / "deep" / "fresh-home"
        monkeypatch.setenv(config_module.ENV_HOME, str(fresh))
        transfer.restore(str(malicious))

        assert outside.read_text(encoding="utf-8") == "original"

    def test_a_symlink_member_is_dropped(self, tmp_path, monkeypatch):
        malicious = tmp_path / "evil.tar.gz"
        with tarfile.open(malicious, "w:gz") as archive:
            info = tarfile.TarInfo("config.yaml")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)

        fresh = tmp_path / "fresh-home"
        monkeypatch.setenv(config_module.ENV_HOME, str(fresh))
        transfer.restore(str(malicious))

        assert not (fresh / "config.yaml").is_symlink()
