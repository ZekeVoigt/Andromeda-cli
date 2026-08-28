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
        # The first thing somebody sees, so it says what to *do* rather than
        # where the empty file would live. Naming a path is an instruction to
        # go and write JSON, and that was the whole reason nobody ever
        # connected anything.
        output.info("No MCP servers connected yet.\n")
        output.info("  andromeda mcp catalog          # 19 that install with one command")
        output.info("  andromeda mcp install stripe   # for example")
        output.info("  andromeda mcp add <name> --url <endpoint>   # anything else")
        output.info(f"\n  {mcp_module.config_path(home)}")
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

    # What else could be connected, right here, rather than behind a command
    # somebody has to already know about. The whole failure this fixes was a
    # capability that existed and was undiscoverable — listing what is
    # connected and stopping there repeats it one level in.
    from andromeda_tools import mcp_catalog

    rest = [
        entry.name
        for entry in mcp_catalog.entries()
        if entry.name not in {server.name for server in servers}
    ]
    if rest:
        output.info(f"\n  Also available: {', '.join(rest)}")
        output.info("  andromeda mcp install <name>")

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


# ---------------------------------------------------------------------------
# Adding, removing, testing
# ---------------------------------------------------------------------------


def _probe(name: str, config: dict) -> tuple[bool, list[dict], str]:
    """Connect once and report what came back.

    Done before a server is written, not after, so `add` fails on a typo in the
    URL rather than leaving a config that only turns out to be wrong on the
    next launch. An OAuth server that answers "unauthorized" counts as reached:
    it is a real server, it just has not been signed in to yet.
    """
    server = mcp_module.MCPServer(
        name=name, config=config, home=config_module.home()
    )
    try:
        connected = server.connect()
        return connected, list(server.tools), server.error
    finally:
        server.close()


def _ask(question: str, *, secret: bool = False, default: str = "") -> str:
    """One line from the person, with the default shown and the secret hidden."""
    import getpass

    suffix = f" [{default}]" if default else ""
    prompt = f"  {question}{suffix}: "
    try:
        answer = getpass.getpass(prompt) if secret else input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    answer = answer.strip()
    return answer or default


def _confirm(question: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {question} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def add(
    name: str,
    *,
    url: str = "",
    command: str = "",
    args: list[str] | None = None,
    auth: str = "",
    env: list[str] | None = None,
    headers: list[str] | None = None,
    force: bool = False,
) -> int:
    """`andromeda mcp add <name>` — connect a server without opening an editor.

    Writes the entry, then proves it: a server that is saved but unreachable is
    the failure mode this command exists to remove, because the person who
    typed the URL is the only one who can fix a typo in it and they are here
    right now.
    """
    from andromeda_tools import mcp_config

    home = config_module.home()

    if not url and not command:
        output.fail(
            "Give the server either a URL or a command.",
            "andromeda mcp add linear --url https://mcp.linear.app/mcp --auth oauth",
        )
        output.info("  andromeda mcp catalog     # ones that need neither")
        return 1
    if url and command:
        output.fail("A server is reached one way or the other, not both.")
        return 1

    try:
        if mcp_config.exists(home, name) and not force:
            if not _confirm(f"`{name}` already exists. Replace it?"):
                output.info("Left alone.")
                return 1

        config: dict = {}
        if url:
            config["url"] = url
            if auth == "oauth":
                config["auth"] = "oauth"
            header_values = mcp_config.parse_env(headers)
            if header_values:
                config["headers"] = header_values
            if env:
                output.fail("`--env` sets a child process's environment, which a remote server does not have.")
                output.info("  Use `--header NAME=value` for a remote server.")
                return 1
        else:
            config["command"] = command
            if args:
                config["args"] = list(args)
            env_values = mcp_config.parse_env(env)
            if env_values:
                config["env"] = env_values

        mcp_config.save(home, name, config)
    except mcp_config.ConfigError as exc:
        output.fail(str(exc))
        return 1

    output.ok(f"Added `{name}`.")

    connected, tools, error = _probe(name, config)
    if connected:
        output.info(f"  {len(tools)} tools:")
        for tool in tools[:12]:
            output.console.print(f"      [dim]{tool.get('name')}[/dim]")
        if len(tools) > 12:
            output.info(f"      … and {len(tools) - 12} more")
        return 0

    if auth == "oauth" or config.get("auth") == "oauth":
        output.info(f"  Not signed in yet — andromeda mcp login {name}")
        return 0
    output.fail(f"  Saved, but it did not answer: {error}")
    output.info(f"  andromeda mcp remove {name}   # if that was a typo")
    return 1


def remove(name: str) -> int:
    """Drop a server, and the credentials that only existed for it.

    Tokens are forgotten alongside the entry. Leaving them behind would mean a
    server removed for being untrusted still has a live grant sitting in the
    token store, which is exactly the state somebody removing it was trying to
    get out of.
    """
    from andromeda_tools import mcp_auth, mcp_config

    home = config_module.home()
    try:
        dropped = mcp_config.remove(home, name)
    except mcp_config.ConfigError as exc:
        output.fail(str(exc))
        return 1

    if not dropped:
        output.fail(f"There is no server called `{name}`.")
        known = sorted(mcp_config.servers(home))
        if known:
            output.info("  Configured: " + ", ".join(known))
        return 1

    output.ok(f"Removed `{name}`.")
    if mcp_auth.forget(home, name):
        output.info("  Its stored credentials were forgotten too.")
    return 0


def test(name: str) -> int:
    """Connect to one server and say precisely what happened."""
    from andromeda_tools import mcp_config

    home = config_module.home()
    config = mcp_config.servers(home).get(name)
    if config is None:
        output.fail(f"There is no server called `{name}`.")
        known = sorted(mcp_config.servers(home))
        if known:
            output.info("  Configured: " + ", ".join(known))
        return 1

    where = config.get("url") or f"{config.get('command', '')} {' '.join(config.get('args') or [])}".strip()
    output.info(f"  {name} → {where}")

    connected, tools, error = _probe(name, config)
    if not connected:
        output.fail(f"  {error}")
        if config.get("auth") == "oauth":
            output.info(f"  andromeda mcp login {name}")
        return 1

    output.ok(f"  answered, {len(tools)} tools")
    for tool in tools:
        summary = str(tool.get("description") or "").splitlines()
        first = summary[0][:64] if summary else ""
        output.console.print(f"      [dim]{str(tool.get('name')).ljust(30)} {first}[/dim]")
    return 0


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def catalog(search: str = "") -> int:
    """`andromeda mcp catalog` — the servers that install with one command."""
    from andromeda_tools import mcp_catalog, mcp_config

    entries = mcp_catalog.search(search) if search else mcp_catalog.entries()
    if not entries:
        output.fail(f"Nothing in the catalog matches {search!r}.")
        output.info("  andromeda mcp catalog       # the whole list")
        output.info("  andromeda mcp add <name> --url …   # anything not on it")
        return 1

    configured = set(mcp_config.servers(config_module.home()))
    output.info(f"  {len(entries)} available\n")
    for entry in entries:
        mark = "[green]✓[/green]" if entry.name in configured else " "
        note = "oauth" if entry.auth == "oauth" else entry.auth
        output.console.print(
            f"  {mark} [cyan]{entry.name.ljust(15)}[/cyan] "
            f"[dim]{entry.description}[/dim]"
        )
        output.console.print(f"      [dim]{note} · {entry.url or entry.command}[/dim]")

    output.info("\n  andromeda mcp install <name>")
    output.info("  Not on the list is not a limit — `andromeda mcp add` takes any server.")

    broken = mcp_catalog.problems()
    if broken:
        output.info("")
        for filename, reason in broken:
            output.fail(f"  {filename}: {reason}")
    return 0


def _collect_env(entry) -> dict[str, str] | None:
    """Ask for what the server needs. `None` means the person backed out.

    A secret may be answered with a reference — `keychain://…`, `op://…` — and
    it is stored as the reference rather than the value, so the credential
    never lands in `mcp.json` at all. That is offered rather than required:
    somebody who wants to paste the key can, and the file is written 0600
    either way.
    """
    if not entry.env:
        return {}

    from andromeda_agent import secrets as secrets_module

    output.info("\n  This one needs a few things:")
    values: dict[str, str] = {}
    for spec in entry.env:
        if spec.secret:
            output.info(f"    {spec.name} — paste the value, or a reference like keychain://andromeda/{entry.name}")
        answer = _ask(spec.question, secret=spec.secret, default=spec.default)
        if not answer:
            if spec.required:
                output.fail(f"  `{spec.name}` is required. Nothing was saved.")
                return None
            continue
        if spec.secret and secrets_module.is_reference(answer):
            resolution = secrets_module.resolve(spec.name, answer)
            if not resolution.ok:
                output.fail(f"  {answer} did not resolve: {resolution.detail or resolution.remedy}")
                return None
            output.info(f"    stored as a reference, resolved at connect time")
        values[spec.name] = answer
    return values


def install(name: str, *, force: bool = False) -> int:
    """`andromeda mcp install <name>` — a catalog entry, configured and proved."""
    from andromeda_tools import mcp_catalog, mcp_config

    entry = mcp_catalog.get(name)
    if entry is None:
        near = mcp_catalog.search(name)
        output.fail(f"`{name}` is not in the catalog.")
        if near:
            output.info("  Did you mean: " + ", ".join(item.name for item in near))
        output.info("  andromeda mcp catalog")
        output.info(f"  andromeda mcp add {name} --url <endpoint>   # to add it by hand")
        return 1

    home = config_module.home()
    if mcp_config.exists(home, entry.name) and not force:
        if not _confirm(f"`{entry.name}` is already configured. Replace it?"):
            output.info("Left alone.")
            return 1

    output.info(f"  {entry.name} — {entry.description}")
    if entry.source:
        output.info(f"  {entry.source}")

    if entry.needs_clone:
        # Deliberately not done silently. A git install runs commands from a
        # third-party repository on this machine, which is a larger thing than
        # writing a URL into a config file, and it gets shown in full and
        # agreed to rather than mentioned afterwards.
        output.info(f"\n  This installs from {entry.install_url}")
        output.info(f"  at commit {entry.install_ref}, and then runs:")
        for step in entry.bootstrap:
            output.console.print(f"      [dim]{step}[/dim]")
        if not _confirm("Go ahead?"):
            output.info("Nothing was installed.")
            return 1
        output.fail("  Git-installed catalog entries are not wired up yet.")
        return 1

    values = _collect_env(entry)
    if values is None:
        return 1

    config = mcp_catalog.config_for(entry, values)
    try:
        mcp_config.save(home, entry.name, config)
    except mcp_config.ConfigError as exc:
        output.fail(str(exc))
        return 1

    output.info("")
    output.ok(f"Configured `{entry.name}`.")

    if entry.auth == "oauth":
        # Offered, not performed. Opening a browser is the person's decision,
        # and `install` running inside a script should not hang on a consent
        # screen nobody is watching.
        output.info(f"  It needs signing in to: andromeda mcp login {entry.name}")
        if _confirm("Do that now?", default=True):
            return login(entry.name)
        return 0

    connected, tools, error = _probe(entry.name, config)
    if connected:
        output.info(f"  {len(tools)} tools available.")
        return 0
    output.fail(f"  Configured, but it did not answer: {error}")
    if entry.after:
        output.info("")
        for line in entry.after.splitlines():
            output.info(f"  {line}")
    return 1


# ---------------------------------------------------------------------------
# Carrying a connection to the cloud
# ---------------------------------------------------------------------------


def _endpoint() -> tuple[str, str, str]:
    credentials = config_module.load_credentials()
    base = credentials.base_url or config_module.load().get("base_url", "")
    return base, credentials.device_token, credentials.device_id


def push(name: str = "") -> int:
    """Make this machine's MCP connections reachable from a hosted job.

    A container is not the machine you signed in on: no config, no tokens, and
    no browser to mint one with. Without this a cloud job reaches none of the
    servers you have connected and reports it as "I have no tools for that".
    """
    from andromeda_agent import mcp_cloud

    base, token, device = _endpoint()
    if not (base and token and device):
        output.fail(
            "This machine is not paired with an account.",
            "Run `andromeda login` first — the cloud store is per account.",
        )
        return 2

    home = config_module.home()
    try:
        skipped = mcp_cloud.push(base, token, device, home, [name] if name else None)
    except mcp_cloud.PushError as exc:
        output.fail(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        output.fail(f"could not reach the account's secret store: {exc}")
        return 1

    servers, _auth, _ = mcp_cloud.collect(home, [name] if name else None)
    output.ok(f"{len(servers)} server(s) available to cloud jobs: {', '.join(servers)}")
    for line in skipped:
        output.info(f"  {line}")
    output.info("\n  Cloud jobs pick this up on their next run.")
    return 0


def unpush() -> int:
    """Stop hosted jobs reaching this machine's MCP servers."""
    from andromeda_agent import mcp_cloud

    base, token, device = _endpoint()
    if not (base and token and device):
        output.fail("This machine is not paired with an account.")
        return 2
    mcp_cloud.forget(base, token, device)
    output.ok("Cloud jobs no longer have MCP access.")
    return 0
