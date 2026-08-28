"""A plugin that already works, to start from.

`andromeda plugins new <name>` writes these three files. A scaffold rather than
a blank directory because the first thing an author needs is not documentation,
it is a file that already loads: the manifest keys in the right places,
`register(ctx)` with the right signature, and a tool whose risk tier is set.
Everything after that is editing rather than assembling.

The comments in what it writes are load-bearing. They are the only
documentation an author reads at the moment they are deciding — the README is
somewhere else, and the capability list is a command they have not run yet.
"""

from __future__ import annotations

MANIFEST = '''name: {plugin_id}
version: 0.1.0
description: {description}
kind: standalone
api_version: 1

# Environment variables this plugin needs. A plugin whose variables are unset
# is refused *before* it is imported, with the names in the message, rather
# than failing later somewhere further from the cause.
# requires_env: [ACME_API_KEY]

# Other plugins this one wants loaded first. Advisory: a missing one is a
# warning, not a failure, and `ctx.has_plugin("other")` is the runtime check.
# requires_plugins: [other]

# Seams this plugin takes over. Each is refused until the person installing
# grants it, and each is described to them in a sentence about what it does to
# them. `andromeda plugins capabilities` lists all twelve.
# capabilities: [tools.override]
'''


INIT = '''"""{description}"""

from andromeda_tools.spec import ToolResult


def register(ctx):
    """Called once at startup, if this plugin is enabled.

    Everything below *adds*, so none of it needs a capability: a new tool still
    goes through the same approval gate as every built-in, and a new command is
    a new name. Replacing something the harness already owns — the memory
    backend, the model provider, a built-in tool — is what needs a grant.
    """
    ctx.register_tool(
        "{plugin_id}_hello",
        "Say hello from the {plugin_id} plugin.",
        {{"type": "object", "properties": {{"name": {{"type": "string"}}}}}},
        _hello,
        # The same vocabulary every built-in uses, because the approval gate and
        # the delegation belts are written in terms of it. Omit these and you
        # get `outbound`/`write` — a tool that asks first, which is the right
        # default for code somebody else wrote.
        risk_tier="safe_local",
        category="read",
    )

    ctx.register_command(
        "{plugin_id}",
        lambda raw: f"hello {{raw or 'there'}}",
        "Say hello.",
    )

    # Other things worth knowing about `ctx`:
    #
    #   ctx.register_hook("on_session_start", fn)   17 lifecycle events
    #   ctx.state.set("cursor", 41)                 10MB of JSON, yours alone
    #   ctx.get_config("key", default)              settings from the ledger
    #   ctx.emit("event", payload)                  published as "{plugin_id}:event"
    #   ctx.on_unload(close_things)                 run when unloaded
    #
    # `andromeda plugins doctor .` prints what you registered, which is how you
    # find out that the thing you thought you added is not there.


def _hello(name: str = "there") -> ToolResult:
    """One tool.

    Returns a `ToolResult` rather than a string: `content` is what the model
    reads and `display` is what the terminal shows, which is usually shorter.
    Raising is fine too — the loop turns it into an error the model can recover
    from rather than ending the turn.
    """
    return ToolResult(content=f"hello {{name}}")
'''


README = '''# {plugin_id}

{description}

## Trying it

    andromeda plugins doctor .
    andromeda plugins install . --enable
    andromeda plugins remove {plugin_id}

`doctor` loads it through the real runtime with the network cut, and prints
what it registered.

## Publishing it

Push it to a repository and anyone can install it:

    andromeda plugins install owner/{plugin_id}

Installing runs a security scan before anything is imported. A `dangerous`
verdict refuses the install and `--force` does not override it.
'''


def files(plugin_id: str, description: str) -> dict[str, str]:
    """The scaffold, as a path-to-content mapping."""
    return {
        "plugin.yaml": MANIFEST.format(plugin_id=plugin_id, description=description),
        "__init__.py": INIT.format(plugin_id=plugin_id, description=description),
        "README.md": README.format(plugin_id=plugin_id, description=description),
    }
