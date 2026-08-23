"""Inspecting configured MCP servers."""

from __future__ import annotations

from andromeda_tools import mcp as mcp_module

from .. import config as config_module
from .. import output


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
                f"[dim]{len(server.tools)} tools[/dim]"
            )
            for tool in server.tools:
                name = mcp_module.tool_name(server.name, str(tool.get("name") or ""))
                summary = str(tool.get("description") or "").splitlines()[0][:70]
                output.console.print(f"      [dim]{name.ljust(38)} {summary}[/dim]")
        else:
            failures += 1
            output.console.print(f"  [red]✗[/red] [cyan]{server.name}[/cyan]")
            output.console.print(f"      [dim]{server.error}[/dim]")
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
    }
  }
}"""


def example() -> int:
    home = config_module.home()
    output.info(f"Write this to {mcp_module.config_path(home)}:\n")
    output.console.print(EXAMPLE, highlight=False)
    return 0
