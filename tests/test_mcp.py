"""The MCP client, against a real server process over a real pipe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from andromeda_tools import mcp

SERVER = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


def write_config(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def echo_config():
    return {"command": sys.executable, "args": [str(SERVER)]}


@pytest.fixture
def server(home, echo_config):
    write_config(home, {"echo": echo_config})
    servers = mcp.build_servers(home)
    yield servers[0]
    servers[0].close()


class TestNaming:
    def test_a_tool_carries_its_server(self):
        assert mcp.tool_name("github", "search") == "mcp__github__search"

    def test_illegal_characters_are_replaced(self):
        assert mcp.tool_name("@acme/search-tools", "Find Thing") == (
            "mcp__acme_search_tools__find_thing"
        )

    def test_two_servers_with_the_same_tool_do_not_collide(self):
        assert mcp.tool_name("a", "search") != mcp.tool_name("b", "search")

    def test_an_empty_name_still_yields_something_legal(self):
        assert mcp.tool_name("", "") == "mcp__unnamed__unnamed"


class TestConfig:
    def test_no_file_means_no_servers(self, home):
        assert mcp.load_config(home) == {}

    def test_the_standard_key_is_read(self, home, echo_config):
        write_config(home, {"echo": echo_config})
        assert "echo" in mcp.load_config(home)

    def test_the_snake_case_key_is_also_read(self, home, echo_config):
        """So a config can be copied in from either spelling without editing."""
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp.json").write_text(
            json.dumps({"mcp_servers": {"echo": echo_config}}), encoding="utf-8"
        )
        assert "echo" in mcp.load_config(home)

    def test_a_disabled_server_is_skipped(self, home, echo_config):
        write_config(home, {"echo": {**echo_config, "disabled": True}})
        assert mcp.load_config(home) == {}

    def test_a_corrupt_file_reads_as_no_servers(self, home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp.json").write_text("{not json", encoding="utf-8")
        assert mcp.load_config(home) == {}


class TestConnection:
    def test_it_handshakes_and_lists_tools(self, server):
        assert server.connect() is True
        assert {tool["name"] for tool in server.tools} == {"echo", "add", "explode"}

    def test_non_json_output_from_the_server_is_ignored(self, server):
        """The fixture prints 'starting up' before speaking protocol."""
        assert server.connect() is True

    def test_a_missing_command_is_recorded_not_raised(self, home):
        write_config(home, {"broken": {"command": "definitely-not-a-real-binary-xyz"}})
        server = mcp.build_servers(home)[0]
        assert server.connect() is False
        assert server.error
        assert server.connected is False

    def test_a_config_with_neither_command_nor_url_is_recorded(self, home):
        write_config(home, {"broken": {"args": ["x"]}})
        server = mcp.build_servers(home)[0]
        assert server.connect() is False
        assert "command" in server.error

    def test_one_broken_server_does_not_stop_another(self, home, echo_config):
        write_config(home, {"broken": {"command": "nope-xyz"}, "echo": echo_config})
        servers = mcp.build_servers(home)
        results = [server.connect() for server in servers]
        assert results.count(True) == 1
        for server in servers:
            server.close()


class TestCalling:
    def test_a_tool_call_round_trips(self, server):
        server.connect()
        result = server.call("echo", {"text": "hello mcp"})
        assert result.ok and "hello mcp" in result.content

    def test_arguments_reach_the_server(self, server):
        server.connect()
        assert "7" in server.call("add", {"a": 3, "b": 4}).content

    def test_a_tool_error_is_reported_not_raised(self, server):
        server.connect()
        result = server.call("explode", {})
        assert result.ok is False
        assert "it broke" in result.content

    def test_an_unknown_tool_is_reported(self, server):
        server.connect()
        result = server.call("nonexistent", {})
        assert result.ok is False and "no tool" in result.content

    def test_calling_connects_if_needed(self, server):
        assert server.connected is False
        assert server.call("echo", {"text": "x"}).ok

    def test_a_dead_server_is_marked_for_reconnection(self, server):
        server.connect()
        server.transport.close()
        result = server.call("echo", {"text": "x"})
        assert result.ok is False
        assert server.connected is False


class TestSpecs:
    def test_every_tool_becomes_a_spec(self, server):
        server.connect()
        specs = mcp.specs_for(server)
        assert {spec.name for spec in specs} == {
            "mcp__echo__echo",
            "mcp__echo__add",
            "mcp__echo__explode",
        }

    def test_specs_carry_the_servers_schema(self, server):
        server.connect()
        spec = next(s for s in mcp.specs_for(server) if s.name == "mcp__echo__add")
        assert set(spec.parameters["properties"]) == {"a", "b"}

    def test_every_mcp_tool_is_outbound(self, server):
        """Third-party code reaching somewhere the harness knows nothing about."""
        server.connect()
        assert all(spec.risk_tier == "outbound" for spec in mcp.specs_for(server))

    def test_the_description_names_the_server(self, server):
        server.connect()
        spec = next(s for s in mcp.specs_for(server) if s.name == "mcp__echo__echo")
        assert spec.description.startswith("[echo]")

    def test_a_spec_actually_calls_through(self, server):
        server.connect()
        spec = next(s for s in mcp.specs_for(server) if s.name == "mcp__echo__echo")
        assert "round trip" in spec.run(text="round trip").content

    def test_a_tool_with_no_schema_still_gets_an_object(self, server):
        server.connect()
        server.tools = [{"name": "bare"}]
        spec = mcp.specs_for(server)[0]
        assert spec.parameters["type"] == "object"


class TestContentFlattening:
    def test_text_blocks_are_joined(self):
        result = mcp._flatten({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
        assert result == "a\nb"

    def test_images_are_named_not_decoded(self):
        """Reasoning about pixels is not what this harness does."""
        result = mcp._flatten(
            {"content": [{"type": "image", "mimeType": "image/png", "data": "AAAA"}]}
        )
        assert "image content" in result and "AAAA" not in result

    def test_a_resource_yields_its_text(self):
        result = mcp._flatten(
            {"content": [{"type": "resource", "resource": {"text": "file body"}}]}
        )
        assert result == "file body"

    def test_structured_content_is_used_when_there_are_no_blocks(self):
        result = mcp._flatten({"structuredContent": {"count": 3}})
        assert "count" in result


class TestGating:
    def test_mcp_tools_are_allowed_by_prefix_not_by_name(self, server):
        """Their names are not knowable when the defaults are written."""
        from andromeda_agent.approval import Policy

        server.connect()
        spec = mcp.specs_for(server)[0]
        policy = Policy(mode="auto", enabled=frozenset({"read_file"}))
        assert policy.decide(spec) == "allowed"

    def test_they_are_still_gated_in_ask_mode(self, server):
        from andromeda_agent.approval import Policy

        server.connect()
        spec = mcp.specs_for(server)[0]
        assert Policy(mode="ask", enabled=frozenset()).decide(spec) == "needs_approval"

    def test_a_pipe_never_sees_them(self, server):
        """`outbound` is above a narrowed non-interactive ceiling."""
        from andromeda_agent.approval import Policy

        server.connect()
        spec = mcp.specs_for(server)[0]
        narrowed = Policy(mode="ask", enabled=frozenset()).narrow(max_tier="safe_local")
        assert narrowed.decide(spec) == "denied"
