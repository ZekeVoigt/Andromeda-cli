"""`andromeda secrets` — what the vault references resolve to, and what broke.

Deliberately never prints a secret. The question people actually have is "is
this one working", and answering it with the value turns a diagnostic command
into the thing the vault was meant to prevent. `get` exists and masks, because
a person confirming they moved the right key needs the first four characters,
not the key.
"""

from __future__ import annotations

import os

from andromeda_agent import cloud_client, redact, secrets as secrets_module

from .. import config as config_module
from .. import output


def status() -> int:
    """Every reference, resolved, with the reason and the fix for each failure.

    Resolution is done with the cache bypassed: somebody running this is
    checking whether the vault answers *now*, and a cached hit from four
    minutes ago answers a question they did not ask.
    """
    config = config_module.load()
    mapping = secrets_module.from_config(config)
    literals = secrets_module.literal_values(config)

    if not mapping and not literals:
        output.info(f"No `secrets:` block in {config_module.config_path()}")
        output.info("")
        output.console.print(EXAMPLE, highlight=False)
        return 0

    failures = 0
    for name, reference in mapping.items():
        scheme = secrets_module.scheme_of(reference)
        resolver = secrets_module.RESOLVERS.get(scheme)
        label = resolver.label if resolver else f"{scheme}://"

        result = secrets_module.resolve(name, reference, use_cache=False)
        if result.ok:
            shadowed = _shadowed(name)
            note = " [dim](the environment already sets this)[/dim]" if shadowed else ""
            output.console.print(
                f"  [green]✓[/green] [cyan]{name}[/cyan] "
                f"[dim]{label} → {redact.mask(result.value)}[/dim]{note}"
            )
        else:
            failures += 1
            output.console.print(
                f"  [red]✗[/red] [cyan]{name}[/cyan] [dim]{label}[/dim]"
            )
            if result.detail:
                # A helper's stderr can quote what it was asked for.
                output.console.print(
                    f"      [dim]{secrets_module.safe_reference(result.detail)}[/dim]"
                )
            if result.remedy:
                output.console.print(f"      [dim]{result.remedy}[/dim]")

    for name in literals:
        failures += 1
        output.console.print(f"  [red]✗[/red] [cyan]{name}[/cyan] [dim]not a reference[/dim]")
        output.console.print(
            "      [dim]This looks like the value itself. `config.yaml` is meant "
            "to be safe to print and to commit —[/dim]"
        )
        output.console.print(
            "      [dim]move it into a vault and put the reference here. "
            "`andromeda secrets example`[/dim]"
        )

    output.info(f"\n  {config_module.config_path()}")
    return 1 if failures else 0


def _shadowed(name: str) -> bool:
    """Whether the shell already sets this, so the reference will not be used.

    Worth saying out loud. A reference that resolves perfectly and is then
    ignored because a stale `export` is still in a shell profile is a long
    afternoon.

    "Set by something other than us" — startup has already applied this block
    into the environment of the process asking the question, so a plain
    `os.environ` check reports every working reference as shadowed.
    """
    return bool(os.environ.get(name)) and name not in secrets_module.applied_names()


def get(name: str) -> int:
    """`andromeda secrets get NAME` — masked, always.

    There is no flag to print it whole. Anyone who needs the value has the
    vault, and a CLI that will print a credential on request is a CLI that gets
    asked to, in a shared terminal, on a call.
    """
    mapping = secrets_module.from_config(config_module.load())
    reference = mapping.get(name)
    if not reference:
        output.fail(
            f"No `{name}` in the `secrets:` block.",
            "andromeda secrets   # lists what is configured",
        )
        return 1

    result = secrets_module.resolve(name, reference, use_cache=False)
    if not result.ok:
        output.fail(
            f"Could not read {secrets_module.safe_reference(reference)}",
            result.remedy or result.detail,
        )
        return 1

    # The reference is not repeated: the person asked about `name`, the
    # reference is in their own config file, and a `cmd://` one can carry a
    # credential inline.
    output.console.print(
        f"[cyan]{name}[/cyan] [dim]{redact.mask(result.value)} "
        f"({len(result.value)} characters, via "
        f"{secrets_module.scheme_of(reference)}://)[/dim]"
    )
    return 0


def schemes() -> int:
    """What this build can read, and whether the helper is installed."""
    for scheme in sorted(secrets_module.RESOLVERS):
        resolver = secrets_module.RESOLVERS[scheme]
        installed = resolver.available()
        mark = "[green]✓[/green]" if installed else "[dim]·[/dim]"
        note = "" if installed else f"  [dim]{resolver.install}[/dim]"
        output.console.print(
            f"  {mark} [cyan]{scheme}://[/cyan] [dim]{resolver.label}[/dim]{note}"
        )
    return 0


EXAMPLE = """Add one to config.yaml — references, never values, so the file
stays safe to read, print and commit:

  secrets:
    OPENROUTER_API_KEY: "op://Personal/OpenRouter/credential"
    GITHUB_TOKEN: "keychain://github-token"
    ANTHROPIC_API_KEY: "cmd://pass show anthropic/api"

They are resolved into the environment at startup, so anything that reads an
environment variable — the BYOK lane, an MCP server, a hook, a command you run
— sees the value without knowing a vault was involved. Something your shell
already sets always wins."""


def example() -> int:
    output.console.print(EXAMPLE, highlight=False)
    return 0


# ---------------------------------------------------------------------------
# Secrets that follow a job into a container
# ---------------------------------------------------------------------------


def _endpoint() -> tuple[str, str, str]:
    credentials = config_module.load_credentials()
    base = credentials.base_url or config_module.load().get("base_url", "")
    return base, credentials.device_token, credentials.device_id


def put_cloud(name: str, value: str = "") -> int:
    """Store a credential a hosted job can use.

    Every other scheme this program reads — 1Password, Bitwarden, the keychain,
    a helper command — is a reference to *this* machine. None of them survives a
    container, and the failure is the worst available shape: a missing
    environment variable, at 3am, in a log nobody is reading. This is the way to
    give a cloud job a credential at all.

    The value is prompted for rather than taken as an argument when it is not
    piped, because an argument is a line in a shell history file.
    """
    import sys

    if not value:
        if sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"  value for {name} (not echoed): ")
        else:
            value = sys.stdin.read().strip()

    if not value:
        output.fail("No value given.", f"`andromeda secrets put {name} --cloud` and paste it, or pipe it in.")
        return 2

    base, token, device = _endpoint()
    try:
        cloud_client.put_secret(base, token, device, name, value)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not store that secret.", str(exc))
        return 2

    output.ok(f"Stored {name} for your hosted jobs.")
    output.info(f"  reference it as [cyan]andromeda://{name}[/cyan] in a `secrets:` block")
    output.info("  it is sealed server-side, and no command prints it back")
    return 0


def list_cloud() -> int:
    base, token, device = _endpoint()
    try:
        rows = cloud_client.list_secrets(base, token, device)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not read your hosted secrets.", str(exc))
        return 2

    if not rows:
        output.info("  no hosted secrets. `andromeda secrets put <NAME> --cloud` adds one.")
        return 0

    for row in rows:
        used = row.get("lastUsedAt")
        when = "never used" if not used else "last used by a job"
        output.console.print(f"  [cyan]{row.get('name')}[/cyan]  [dim]{when}[/dim]")
    # Said plainly, because the absence of a `get` is deliberate and otherwise
    # reads as a missing feature.
    output.info("\n  There is no command that prints a hosted secret back.")
    return 0


def forget_cloud(name: str) -> int:
    base, token, device = _endpoint()
    try:
        removed = cloud_client.forget_secret(base, token, device, name)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not remove that secret.", str(exc))
        return 2
    if not removed:
        output.fail(f"No hosted secret called {name!r}.")
        return 2
    output.ok(f"Removed {name}. Jobs that referenced it will now fail on it.")
    return 0
