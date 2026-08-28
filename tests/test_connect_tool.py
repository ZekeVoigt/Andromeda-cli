"""`connect_app` — the agent connecting an app without sending anybody out."""

from __future__ import annotations

import pytest

from andromeda_tools import connect, mcp_config


@pytest.fixture
def home(tmp_path):
    return tmp_path


def test_list_says_what_is_connected_and_what_could_be(home):
    text = connect.run(home, "list").content
    assert "Nothing is connected yet" in text
    assert "stripe" in text


def test_list_reports_an_oauth_server_as_not_signed_in(home):
    mcp_config.save(home, "stripe", {"url": "https://mcp.stripe.com", "auth": "oauth"})
    text = connect.run(home, "list").content
    assert "not signed in" in text
    # And says both ways to fix it, because the whole complaint was being sent
    # out of the session to run a command.
    assert "andromeda mcp login stripe" in text
    assert "connect_app" in text


def test_list_says_the_catalog_is_not_a_limit(home):
    """A model that reads the list as exhaustive will tell somebody their app
    is unsupported when it merely is not preloaded."""
    text = connect.run(home, "list").content
    assert "Any other MCP server can be added" in text


def test_find_matches_a_name_and_a_host(home):
    assert "stripe" in connect.run(home, "find", "stripe").content
    assert "stripe" in connect.run(home, "find", "dashboard.stripe.com").content


def test_find_an_unknown_app_does_not_claim_it_is_impossible(home):
    text = connect.run(home, "find", "some-app-nobody-has").content
    assert "does not mean the app has no MCP server" in text


def test_connect_writes_the_config(home):
    result = connect.run(home, "connect", "stripe", sign_in=False)
    assert result.ok
    assert mcp_config.servers(home)["stripe"]["url"] == "https://mcp.stripe.com"


def test_connect_says_the_tools_arrive_next_session(home):
    """Tools are chosen when a session starts. A model that thinks they are
    live now will reach for them and get nothing."""
    text = connect.run(home, "connect", "stripe", sign_in=False).content
    assert "new session" in text


def test_the_next_session_note_is_on_every_configuring_path(home):
    """Including the one that stops before signing in — the tools are absent
    from this session either way."""
    for sign_in in (False,):
        text = connect.run(home, "connect", "stripe", sign_in=sign_in).content
        assert connect.NEXT_SESSION in text


def test_connect_refuses_anything_not_in_the_catalog(home):
    """A model that could write an arbitrary URL into the config could be
    talked into writing one by a page it read."""
    result = connect.run(home, "connect", "https://evil.example/mcp", sign_in=False)
    assert not result.ok
    assert mcp_config.servers(home) == {}


def test_a_near_miss_is_suggested_rather_than_guessed(home):
    result = connect.run(home, "connect", "strip", sign_in=False)
    assert not result.ok
    assert "stripe" in result.content
    assert mcp_config.servers(home) == {}


def test_connect_refuses_an_entry_that_needs_a_typed_credential(home, monkeypatch):
    """An API key belongs in a prompt the person answers, not in a tool
    argument the model filled in."""
    from andromeda_tools import mcp_catalog

    entry = mcp_catalog.Entry(
        name="needs-key",
        description="x",
        transport="http",
        url="https://x.example/mcp",
        auth="api_key",
        env=(mcp_catalog.EnvVar(name="SOME_KEY"),),
    )
    monkeypatch.setattr(mcp_catalog, "get", lambda name: entry)

    result = connect.run(home, "connect", "needs-key", sign_in=False)
    assert not result.ok
    assert "SOME_KEY" in result.content
    assert mcp_config.servers(home) == {}


def test_an_unknown_action_is_a_result_not_an_exception(home):
    assert not connect.run(home, "explode").ok


def test_the_description_tells_the_model_this_is_the_route(home):
    """The failure was not being unable to connect an app. It was not knowing
    that connecting one was a thing that could happen."""
    text = connect.spec(home).description
    assert "no tools for" in text
    assert "do not simply report that you lack access" in text


def test_it_is_gated(home):
    spec = connect.spec(home)
    assert spec.risk_tier == "outbound"
    assert spec.summarize({"action": "connect", "app": "stripe"}) == "connect stripe"


def test_a_lane_does_not_get_it(tmp_path):
    """Connecting an app writes the config every future session reads. A
    context spawned out of the person's sight must not do that."""
    from andromeda_tools import Workspace, build_registry
    from andromeda_tools.todo import TodoList

    lane = build_registry(Workspace(str(tmp_path)), TodoList())
    assert "connect_app" not in lane

    interactive = build_registry(
        Workspace(str(tmp_path)), TodoList(), connect_home=tmp_path
    )
    assert "connect_app" in interactive


# ---------------------------------------------------------------------------
# Bringing the tools into the running session
# ---------------------------------------------------------------------------


class TestLiveReload:
    """Connecting an app and then saying "restart" is the harness admitting it
    cannot use the thing it just did — and the restart throws away the
    conversation that led to the connection."""

    @staticmethod
    def _session(home):
        from andromeda_agent import Policy
        from andromeda_agent.loop import Conversation
        from andromeda_tools import Workspace, build_registry
        from andromeda_tools.todo import TodoList

        servers: list = []
        live: list = []

        def reconnect():
            from andromeda_tools import mcp as mcp_module

            known = {server.name for server in servers}
            for server in mcp_module.build_servers(home):
                if server.name in known:
                    continue
                server.connect()
                servers.append(server)
            return live[0].reload_tools() if live else []

        def rebuild(todos):
            return build_registry(
                Workspace(),
                todos,
                mcp_servers=servers,
                connect_home=home,
                on_connected=reconnect,
            )

        class Provider:
            model = "m"
            label = "l"
            thinking = "off"

        conversation = Conversation(
            provider=Provider(),
            policy=Policy(
                mode="auto", enabled=frozenset({"connect_app"}), max_tier="irreversible"
            ),
            workspace=Workspace(),
            registry=rebuild(TodoList()),
            rebuild_registry=rebuild,
        )
        live.append(conversation)
        return conversation, reconnect

    def test_a_server_connected_now_is_callable_now(self, tmp_path):
        import sys
        from pathlib import Path

        from andromeda_tools import mcp_config

        conversation, reconnect = self._session(tmp_path)
        assert not [n for n in conversation.registry if n.startswith("mcp__")]

        fixture = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
        mcp_config.save(
            tmp_path, "echo", {"command": sys.executable, "args": [str(fixture)]}
        )
        added = reconnect()

        assert "mcp__echo__echo" in added
        assert "mcp__echo__echo" in conversation.registry

    def test_the_model_is_told_about_them_on_the_next_step(self, tmp_path):
        """`send` builds the catalogue once and hands the same list to every
        step, so without the flag a registry that grew mid-turn stays invisible
        until the person speaks again."""
        import sys
        from pathlib import Path

        from andromeda_tools import mcp_config

        conversation, reconnect = self._session(tmp_path)
        fixture = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
        mcp_config.save(
            tmp_path, "echo", {"command": sys.executable, "args": [str(fixture)]}
        )
        reconnect()

        assert conversation._tools_changed
        assert any(
            spec.name == "mcp__echo__echo" for spec in conversation.available
        )

    def test_the_transcript_survives_it(self, tmp_path):
        """This is not a reset. The conversation that led to the connection is
        the reason the connection happened."""
        conversation, reconnect = self._session(tmp_path)
        conversation.messages.append({"role": "user", "content": "connect echo"})
        before = len(conversation.messages)

        conversation.reload_tools()

        assert len(conversation.messages) == before

    def test_without_a_surface_hook_it_says_restart_rather_than_lying(self, tmp_path):
        """A one-shot run and a lane have no reload. Silently doing nothing
        and reporting success is worse than the restart."""
        from andromeda_tools import connect as connect_module

        assert connect_module._activate(None, "x") == connect_module.NEXT_SESSION

    def test_a_failing_reload_does_not_undo_the_connection(self, tmp_path):
        from andromeda_tools import connect as connect_module

        def explode():
            raise RuntimeError("nope")

        message = connect_module._activate(explode, "x")
        assert "nope" in message
        assert connect_module.NEXT_SESSION in message
