"""Carrying an MCP connection to a hosted runner.

A container is not the machine somebody signed in on. Without this a cloud job
reached none of the servers its owner had connected and reported it as "I have
no tools for that" — indistinguishable from the server being broken.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from andromeda_agent import mcp_cloud
from andromeda_tools import mcp_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in (mcp_cloud.SERVERS_SECRET, mcp_cloud.AUTH_SECRET):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANDROMEDA_MCP_CONFIG", raising=False)
    monkeypatch.delenv("ANDROMEDA_MCP_TOKEN_DIR", raising=False)
    return tmp_path


class TestWhatCanTravel:
    def test_a_remote_server_travels(self, home):
        assert mcp_cloud.travellable("stripe", {"url": "https://mcp.stripe.com"}) == ""

    def test_a_stdio_server_cannot_and_says_why(self, home):
        """Pushing one produces a server that fails to start on every fire."""
        reason = mcp_cloud.travellable("fs", {"command": "npx", "args": ["-y", "x"]})
        assert "npx" in reason
        assert "cannot" in reason

    def test_it_is_refused_rather_than_dropped(self, home):
        mcp_config.save(home, "stripe", {"url": "https://mcp.stripe.com"})
        mcp_config.save(home, "fs", {"command": "npx"})

        servers, _auth, skipped = mcp_cloud.collect(home)

        assert list(servers) == ["stripe"]
        assert any("fs" in line for line in skipped)

    def test_configured_but_unsigned_is_called_out(self, home):
        """"Connected" and "signed in" are different states and only one works."""
        mcp_config.save(home, "stripe", {"url": "https://x", "auth": "oauth"})
        _servers, _auth, skipped = mcp_cloud.collect(home)
        assert any("not signed in" in line for line in skipped)

    def test_nothing_travellable_is_an_error_not_a_silent_success(self, home):
        mcp_config.save(home, "fs", {"command": "npx"})
        with pytest.raises(mcp_cloud.PushError):
            mcp_cloud.push("http://x", "t", "d", home)


class TestTheRunnerSide:
    def _values(self, servers, auth=None):
        return {
            mcp_cloud.SERVERS_SECRET: json.dumps(servers),
            mcp_cloud.AUTH_SECRET: json.dumps(auth or {}),
            "UNRELATED": "keep me",
        }

    def test_it_assembles_a_config_the_client_can_read(self, home, tmp_path):
        from andromeda_tools import mcp as mcp_module

        scratch = tmp_path / "scratch"
        assert mcp_cloud.materialise(
            self._values({"stripe": {"url": "https://mcp.stripe.com"}}), scratch
        )
        assert list(mcp_module.load_config(home)) == ["stripe"]

    def test_tokens_land_where_the_reader_looks(self, home, tmp_path):
        from andromeda_tools import mcp_auth

        mcp_cloud.materialise(
            self._values(
                {"stripe": {"url": "https://x"}},
                {"stripe": {"tokens": {"access_token": "tok"}}},
            ),
            tmp_path / "scratch",
        )
        assert mcp_auth.load(home, "stripe").tokens.access_token == "tok"

    def test_a_server_whose_name_needs_sanitising_still_resolves(self, home, tmp_path):
        """`@acme/x` has to land exactly where `load` goes hunting."""
        from andromeda_tools import mcp_auth

        mcp_cloud.materialise(
            self._values(
                {"@acme/x": {"url": "https://x"}},
                {"@acme/x": {"tokens": {"access_token": "tok"}}},
            ),
            tmp_path / "scratch",
        )
        assert mcp_auth.load(home, "@acme/x").tokens.access_token == "tok"

    def test_the_reserved_names_are_consumed_not_exported(self, home, tmp_path):
        """An access token does not belong in the environment of a job that
        never asked for one."""
        values = self._values({"stripe": {"url": "https://x"}})
        mcp_cloud.materialise(values, tmp_path / "scratch")

        assert mcp_cloud.SERVERS_SECRET not in values
        assert mcp_cloud.AUTH_SECRET not in values
        assert values["UNRELATED"] == "keep me"

    def test_everything_written_is_private(self, home, tmp_path):
        scratch = tmp_path / "scratch"
        mcp_cloud.materialise(
            self._values(
                {"stripe": {"url": "https://x"}},
                {"stripe": {"tokens": {"access_token": "tok"}}},
            ),
            scratch,
        )
        assert stat.S_IMODE(os.stat(scratch / "mcp.json").st_mode) == 0o600
        assert stat.S_IMODE(os.stat(scratch / "mcp-auth").st_mode) == 0o700

    def test_nothing_pushed_is_not_an_error(self, home, tmp_path):
        assert not mcp_cloud.materialise({"UNRELATED": "x"}, tmp_path / "scratch")

    def test_unreadable_configuration_does_not_kill_the_run(self, home, tmp_path):
        values = {mcp_cloud.SERVERS_SECRET: "{not json"}
        assert not mcp_cloud.materialise(values, tmp_path / "scratch")
