"""Inspecting configured MCP servers."""

from __future__ import annotations

from andromeda_tools import mcp as mcp_module

from .. import config as config_module
from .. import output


def _auth_note(server) -> str:
    """Whether an OAuth server has credentials, said in three words.

    Only for OAuth servers: annotating every stdio server with "no OAuth" would
    be noise on the common case.
    """
    if not server.uses_oauth:
        return ""
    from andromeda_tools import mcp_auth

    stored = mcp_auth.load(config_module.home(), server.name)
    if not stored.tokens.access_token:
        return ", not signed in"
    if stored.tokens.valid or stored.tokens.refreshable:
        return ", signed in"
    return ", sign-in expired"


def status() -> int:
    home = config_module.home()
    servers = mcp_module.build_servers(home)

    if not servers:
        output.info(f"No MCP servers configured in {mcp_module.config_path(home)}")
        output.info("  andromeda mcp example   # print a starter config")
        return 0

    failures = 0
    for server in servers:
        connected = server.connect()
        if connected:
            output.console.print(
                f"  [green]✓[/green] [cyan]{server.name}[/cyan] "
                f"[dim]{len(server.tools)} tools{_auth_note(server)}[/dim]"
            )
            for tool in server.tools:
                name = mcp_module.tool_name(server.name, str(tool.get("name") or ""))
                summary = str(tool.get("description") or "").splitlines()[0][:70]
                output.console.print(f"      [dim]{name.ljust(38)} {summary}[/dim]")
        else:
            failures += 1
            output.console.print(
                f"  [red]✗[/red] [cyan]{server.name}[/cyan]"
                f"[dim]{_auth_note(server)}[/dim]"
            )
            output.console.print(f"      [dim]{server.error}[/dim]")
            if server.uses_oauth:
                output.console.print(
                    f"      [dim]andromeda mcp login {server.name}[/dim]"
                )
        server.close()

    output.info(f"\n  {mcp_module.config_path(home)}")
    return 1 if failures else 0


EXAMPLE = """{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..." }
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    },
    "hosted": {
      "url": "https://mcp.example.com/mcp",
      "auth": "oauth"
    }
  }
}

Then, for an OAuth server: andromeda mcp login hosted"""


def example() -> int:
    home = config_module.home()
    output.info(f"Write this to {mcp_module.config_path(home)}:\n")
    output.console.print(EXAMPLE, highlight=False)
    return 0


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _find(name: str):
    """The configured server called `name`, or None with a message printed."""
    home = config_module.home()
    for server in mcp_module.build_servers(home):
        if server.name == name:
            return server
    known = [s.name for s in mcp_module.build_servers(home)]
    output.fail(f"No MCP server called {name!r} in {mcp_module.config_path(home)}.")
    if known:
        output.info("  Configured: " + ", ".join(known))
    return None


def login(name: str) -> int:
    """`andromeda mcp login <server>` — the one place a browser opens.

    Signing in is a deliberate act by a person, so it lives in a command rather
    than happening inside a tool call. A model that asked for a search did not
    ask for a consent screen, and a session that stopped to authorize would be
    taking a decision that is not its to take.
    """
    from andromeda_tools import mcp_auth

    server = _find(name)
    if server is None:
        return 1

    if not server.uses_oauth:
        output.fail(f"{name} is not configured to use OAuth.")
        output.info('  Add `"auth": "oauth"` to its entry in mcp.json.')
        return 1
    if "url" not in server.config:
        output.fail(f"{name} is a local server — OAuth applies to `url` servers.")
        return 1

    def announce(url: str, opened: bool) -> None:
        if opened:
            output.info(f"Opened your browser to authorize {name}.")
        else:
            output.info(f"Open this to authorize {name}:")
        output.info(f"  {url}")

    try:
        mcp_auth.authorize(
            home=config_module.home(),
            server=name,
            server_url=str(server.config["url"]),
            config=server.config.get("oauth")
            if isinstance(server.config.get("oauth"), dict)
            else {},
            announce=announce,
        )
    except mcp_auth.OAuthError as exc:
        output.fail(str(exc), exc.hint)
        return 1

    output.ok(f"Signed in to {name}.")
    # Proved rather than assumed: a stored token that the server will not
    # accept is worse than no token, because nothing looks wrong until the
    # first tool call fails mid-turn.
    reconnected = mcp_module.build_servers(config_module.home())
    for candidate in reconnected:
        if candidate.name == name:
            if candidate.connect():
                output.info(f"  {len(candidate.tools)} tools available.")
            else:
                output.fail(f"  Signed in, but the server still refuses: {candidate.error}")
                candidate.close()
                return 1
            candidate.close()
    return 0


def logout(name: str) -> int:
    """Forget the stored tokens for one server."""
    from andromeda_tools import mcp_auth

    home = config_module.home()
    if mcp_auth.forget(home, name):
        output.ok(f"Forgot the credentials for {name}.")
    else:
        output.info(f"There were no stored credentials for {name}.")
    return 0
