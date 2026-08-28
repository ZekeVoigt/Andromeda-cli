"""Carrying an MCP connection to a hosted runner.

A container is not the machine somebody signed in on. It has no `mcp.json`, no
token store, and no browser to mint one with — so a cloud job reached exactly
none of the servers its owner had connected, and said so as "I have no tools
for that", which is the same wrong answer local sessions used to give.

The connection travels through the account's **secret store**, which already
exists for exactly this shape of problem: values held server-side, opened by a
runner that proved it holds the device credential, never written where they
outlive the fire.

Two reserved names carry it:

* ``ANDROMEDA_MCP_SERVERS`` — the `mcpServers` block, HTTP servers only.
* ``ANDROMEDA_MCP_AUTH`` — `{server: <stored token blob>}`.

**stdio servers do not travel.** They are a local command — `npx`, `uvx`, a
script in a checkout — and the runner image has no Node and no clone. Pushing
one would produce a server that fails to start on every fire, so they are
refused at push time with the reason, rather than silently dropped.

**Nothing lands on the shared volume.** `secrets.py`'s standing rule is that a
value somebody moved into a vault does not get written back into a file that
outlives the run. `materialise` writes to the container's own throwaway disk
and points the client at it through `mcp.CONFIG_PATH_ENV` and
`mcp_auth.TOKEN_DIR_ENV`.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

#: The reserved secret names. Chosen to be obviously ours, so `andromeda
#: secrets list` shows what they are rather than looking like a user's own.
SERVERS_SECRET = "ANDROMEDA_MCP_SERVERS"
AUTH_SECRET = "ANDROMEDA_MCP_AUTH"


class PushError(RuntimeError):
    """Why a server cannot travel, in a sentence somebody can act on."""


def travellable(name: str, config: dict[str, Any]) -> str:
    """Why this server cannot go to the cloud, or an empty string.

    The message is the deliverable. Somebody who connected a filesystem server
    and then scheduled a cloud job has a correct intention and an impossible
    request, and the difference is whether they learn that now or from a week
    of runs that quietly did nothing.
    """
    if not isinstance(config, dict):
        return f"`{name}` has no usable configuration."
    if "url" not in config:
        command = str(config.get("command") or "a local command")
        return (
            f"`{name}` runs `{command}` on this machine. A hosted runner has no "
            f"such command and no filesystem to run it against, so it cannot "
            f"travel. Remote servers reached over a URL can."
        )
    return ""


def collect(home: Path, names: list[str] | None = None) -> tuple[dict, dict, list[str]]:
    """The servers and credentials to push, plus what was left behind.

    Returns `(servers, auth, skipped)`. `skipped` is one sentence per server
    that cannot travel — reported, never silently dropped.
    """
    from andromeda_tools import mcp_auth, mcp_config

    configured = mcp_config.servers(home)
    wanted = [n for n in (names or list(configured)) if n]

    servers: dict[str, Any] = {}
    auth: dict[str, Any] = {}
    skipped: list[str] = []

    for name in wanted:
        config = configured.get(name)
        if config is None:
            skipped.append(f"`{name}` is not configured here.")
            continue
        refusal = travellable(name, config)
        if refusal:
            skipped.append(refusal)
            continue

        servers[name] = config
        stored = mcp_auth.load(home, name)
        if stored.tokens.access_token:
            auth[name] = _blob(home, name)
        elif str(config.get("auth") or "").lower() == "oauth" or config.get("oauth"):
            # Pushed anyway — the config is still useful and the failure is
            # legible on the runner — but said out loud, because "connected"
            # and "signed in" are different states and only one of them works.
            skipped.append(
                f"`{name}` is configured but not signed in, so the runner will "
                f"get the same 401 this machine would. Run `andromeda mcp "
                f"login {name}` first."
            )
    return servers, auth, skipped


def _blob(home: Path, server: str) -> dict[str, Any]:
    """One server's stored credential, exactly as it sits on disk.

    Read as raw JSON rather than rebuilt from `Stored`, so a field this version
    has never heard of still reaches the runner instead of being dropped on the
    way through.
    """
    from andromeda_tools import mcp_auth

    try:
        return json.loads(mcp_auth.token_path(home, server).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def push(base_url: str, token: str, device: str, home: Path, names=None) -> list[str]:
    """Upload the connection to the account's secret store. Returns what was skipped."""
    from . import cloud_client

    servers, auth, skipped = collect(home, names)
    if not servers:
        raise PushError(
            "No MCP server here can run in the cloud.\n"
            + ("\n".join(f"  {line}" for line in skipped) if skipped else "")
        )

    cloud_client.put_secret(base_url, token, device, SERVERS_SECRET, json.dumps(servers))
    cloud_client.put_secret(base_url, token, device, AUTH_SECRET, json.dumps(auth))
    return skipped


def forget(base_url: str, token: str, device: str) -> None:
    """Remove the pushed connection. What a person expects `logout` to mean."""
    from . import cloud_client

    for name in (SERVERS_SECRET, AUTH_SECRET):
        try:
            cloud_client.forget_secret(base_url, token, device, name)
        except Exception:  # noqa: BLE001 - removing what is not there is fine
            pass


# ---------------------------------------------------------------------------
# The runner's half
# ---------------------------------------------------------------------------


def materialise(values: dict[str, str], scratch: Path) -> bool:
    """Assemble a usable MCP setup from resolved secrets. True if there was one.

    `values` is what `resolve_secrets` returned, and the two reserved names are
    consumed here and removed from it — an access token does not belong in the
    process environment of a job that never asked for one.

    Everything is written under `scratch`, which is the container's own disk.
    """
    raw_servers = values.pop(SERVERS_SECRET, "")
    raw_auth = values.pop(AUTH_SECRET, "")
    if not raw_servers:
        return False

    try:
        servers = json.loads(raw_servers)
        auth = json.loads(raw_auth) if raw_auth else {}
    except json.JSONDecodeError:
        print("[andromeda] the pushed MCP configuration is unreadable; skipping it")
        return False
    if not isinstance(servers, dict) or not servers:
        return False

    scratch.mkdir(parents=True, exist_ok=True)
    _write_private(scratch / "mcp.json", {"mcpServers": servers})

    from andromeda_tools import mcp as mcp_module
    from andromeda_tools import mcp_auth as auth_module

    tokens = scratch / "mcp-auth"
    tokens.mkdir(parents=True, exist_ok=True)
    os.chmod(tokens, 0o700)

    # Set before the writes, not after: `token_path` reads it to decide the
    # filename, and writing first would put every blob where nothing looks for
    # it. Going through `token_path` rather than formatting a name here is what
    # keeps the sanitising identical on both sides — a server called `@acme/x`
    # has to land exactly where `load` goes hunting.
    os.environ[auth_module.TOKEN_DIR_ENV] = str(tokens)
    os.environ[mcp_module.CONFIG_PATH_ENV] = str(scratch / "mcp.json")

    if isinstance(auth, dict):
        for name, blob in auth.items():
            if isinstance(blob, dict):
                _write_private(auth_module.token_path(scratch, name), blob)
    print(f"[andromeda] {len(servers)} MCP server(s) available to this run")
    return True


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
