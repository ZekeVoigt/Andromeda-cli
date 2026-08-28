"""`andromeda plugins` — see, install and consent to plugins.

Nine verbs. The ordering of the install flow is the design, so it is stated
here rather than left to be inferred from the code:

    clone at a pinned ref
        │
    security scan          dangerous → refused, and --force does not help
        │                  caution   → shown, and requires an explicit yes
        │
    capability consent     each declared capability, named, one decision
        │
    "enable now?"          default NO

Nothing is imported until the last step. That is the whole reason consent
happens where it does: a plugin that has been enabled has already run its
`register()`, so asking afterwards would be asking about something that
already happened.

`doctor` is the developer's half. It loads a plugin through the real runtime
with the network disabled and reports what failed, so "it does not work" has an
answer before it is published rather than after.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from andromeda_agent import plugin_capabilities as caps
from andromeda_agent import plugin_guard, plugin_index, plugin_packs, plugin_store
from andromeda_agent import plugins as plugins_module

from .. import output

SOURCE_LABELS = {
    "bundled": "bundled",
    "user": "installed",
    "project": "project",
    "entrypoint": "pip",
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def cmd_list(args: Any) -> int:
    """Every plugin this install can see, and its state."""
    manifests = plugins_module.discover()
    if not manifests:
        output.info("No plugins found.")
        output.info(
            f"Install one with `andromeda plugins install <owner/repo>`, or put "
            f"a directory in {plugins_module.user_dir()}."
        )
        return 0

    for plugin_id in sorted(manifests):
        manifest = manifests[plugin_id]
        enabled = plugin_store.is_enabled(plugin_id)
        mark = "[green]●[/green]" if enabled else "[dim]○[/dim]"
        source = SOURCE_LABELS.get(manifest.source, manifest.source)
        kind = " · no code" if manifest.portable else ""
        output.console.print(
            f"  {mark} [cyan]{manifest.id}[/cyan] "
            f"[dim]{manifest.version} · {source}{kind}[/dim]"
        )
        if manifest.description:
            output.console.print(f"      {manifest.description}")
        if manifest.capabilities:
            granted = caps.granted(plugin_id)
            rendered = ", ".join(
                f"[green]{item}[/green]" if item in granted else f"[yellow]{item}[/yellow]"
                for item in manifest.capabilities
            )
            output.console.print(f"      [dim]capabilities:[/dim] {rendered}")
        missing = manifest.missing_env()
        if missing:
            output.console.print(
                f"      [yellow]needs:[/yellow] {', '.join(missing)} [dim](not set)[/dim]"
            )
    output.console.print()
    output.info("● enabled   ○ installed, not enabled")
    return 0


def cmd_show(args: Any) -> int:
    """One plugin, in full."""
    manifests = plugins_module.discover()
    manifest = manifests.get(args.name.lower())
    if manifest is None:
        output.fail(
            f"No plugin called {args.name!r}.",
            "`andromeda plugins list` shows what is installed.",
        )
        return 1

    plugin_id = manifest.id
    row = plugin_store.entry(plugin_id)
    granted = caps.granted(plugin_id)

    output.console.print(f"  [cyan]{manifest.id}[/cyan] [dim]{manifest.version}[/dim]")
    if manifest.description:
        output.console.print(f"  {manifest.description}")
    output.console.print()
    _field("source", SOURCE_LABELS.get(manifest.source, manifest.source))
    _field("location", str(manifest.directory))
    _field(
        "kind",
        "portable — skills and MCP servers, no code"
        if manifest.portable
        else manifest.kind,
    )
    _field("enabled", "yes" if plugin_store.is_enabled(plugin_id) else "no")
    if manifest.author:
        _field("author", manifest.author)
    if manifest.license:
        _field("license", manifest.license)
    if manifest.homepage:
        _field("homepage", manifest.homepage)
    if row.get("ref"):
        _field("ref", str(row["ref"]))
    if row.get("installed_at"):
        _field("installed", str(row["installed_at"]))

    if manifest.requires_plugins:
        _field("requires", ", ".join(manifest.requires_plugins))
    if manifest.requires_env:
        missing = set(manifest.missing_env())
        rendered = ", ".join(
            f"[yellow]{name} (unset)[/yellow]" if name in missing else name
            for name in manifest.requires_env
        )
        _field("environment", rendered)

    if manifest.capabilities:
        output.console.print()
        output.console.print("  capabilities")
        for capability in manifest.capabilities:
            mark = "[green]granted[/green]" if capability in granted else "[yellow]not granted[/yellow]"
            output.console.print(f"    {capability} — {mark}")
            output.console.print(f"      [dim]{caps.describe(capability)}[/dim]")
    if manifest.unknown_capabilities:
        output.console.print()
        output.console.print(
            "  [yellow]declares capabilities this version does not know:[/yellow] "
            + ", ".join(manifest.unknown_capabilities)
        )
    if manifest.unknown_fields:
        output.console.print()
        output.console.print(
            "  [yellow]unrecognised manifest fields:[/yellow] "
            + ", ".join(manifest.unknown_fields)
        )

    loaded = plugins_module.manager().loaded.get(plugin_id)
    if loaded is not None and loaded.error:
        output.console.print()
        output.console.print(f"  [red]not loaded:[/red] {loaded.error}")
    if loaded is not None and loaded.notes:
        output.console.print()
        for note in loaded.notes:
            output.console.print(f"  [yellow]![/yellow] {note}")
    return 0


def _field(label: str, value: str) -> None:
    output.console.print(f"  [dim]{label:<12}[/dim] {value}")


def cmd_capabilities(args: Any) -> int:
    """What every capability means, or what one plugin holds."""
    if getattr(args, "name", None):
        plugin_id = args.name.lower()
        manifests = plugins_module.discover()
        manifest = manifests.get(plugin_id)
        if manifest is None:
            output.fail(f"No plugin called {args.name!r}.")
            return 1
        granted = caps.granted(plugin_id)
        if not manifest.capabilities:
            output.info(f"{plugin_id} declares no capabilities.")
            return 0
        for capability in manifest.capabilities:
            mark = "[green]✓[/green]" if capability in granted else "[yellow]✗[/yellow]"
            output.console.print(f"  {mark} [cyan]{capability}[/cyan]")
            output.console.print(f"      {caps.describe(capability)}")
        return 0

    output.console.print(
        "  [dim]Capabilities gate the seams a plugin can take over. None of "
        "this is a sandbox:[/dim]"
    )
    output.console.print(
        "  [dim]a plugin is Python in this process and can ignore all of it. "
        "Read the code you install.[/dim]"
    )
    output.console.print()
    for spec in caps.CAPABILITIES:
        output.console.print(f"  [cyan]{spec.id}[/cyan] [dim]{spec.gate}[/dim]")
        output.console.print(f"      {spec.description}")
    return 0


# ---------------------------------------------------------------------------
# Enabling
# ---------------------------------------------------------------------------


def cmd_enable(args: Any) -> int:
    """Consent to a plugin's capabilities and turn it on."""
    manifests = plugins_module.discover()
    plugin_id = args.name.lower()
    manifest = manifests.get(plugin_id)
    if manifest is None:
        output.fail(
            f"No plugin called {args.name!r}.",
            "`andromeda plugins list` shows what is installed.",
        )
        return 1

    if manifest.unknown_capabilities:
        output.console.print(
            f"  [yellow]note:[/yellow] {plugin_id} asks for "
            f"{', '.join(manifest.unknown_capabilities)}, which this version of "
            f"Andromeda does not have. It will load without them."
        )

    wanted = list(manifest.capabilities)
    if wanted:
        if not _consent(plugin_id, wanted, assume_yes=bool(getattr(args, "yes", False))):
            output.fail(f"{plugin_id} was not enabled.")
            return 1
        caps.grant(plugin_id, wanted)
    else:
        # Grant an empty set rather than leaving the previous one in place: a
        # plugin that dropped a capability in an update must not keep holding it.
        caps.grant(plugin_id, ())

    problem, added = _inspect(manifest, offline=False)
    if problem:
        # The grant stands — it was a real decision, and revoking it silently
        # would mean the next attempt asks again for no reason. What does not
        # happen is switching the plugin on when it does not load.
        output.fail(
            f"{plugin_id} was granted its capabilities but did not load: {problem}",
            f"`andromeda plugins doctor {manifest.directory}` says more.",
        )
        return 1

    plugin_store.update(plugin_id, enabled=True, source=manifest.source)
    switched = _set_tools_enabled(added.get("tools", []), on=True)

    output.ok(f"{plugin_id} is enabled. It loads on the next `andromeda`.")
    if added.get("summary"):
        output.info(f"It adds: {added['summary']}.")
    if switched:
        output.info(
            f"Turned on {', '.join(switched)} — `andromeda tools disable <name>` "
            f"to switch one back off."
        )
    return 0


def cmd_disable(args: Any) -> int:
    plugin_id = args.name.lower()
    if not plugin_store.entry(plugin_id):
        output.fail(f"No plugin called {args.name!r}.")
        return 1

    # Its tools go back off with it. Read from the live manager when the
    # plugin is loaded, and from a trial load when it is not — a disable has
    # to work from a fresh process, which is the case where nothing is loaded.
    owned = [
        name
        for name, registration in plugins_module.manager().tools().items()
        if registration.plugin_id == plugin_id
    ]
    if not owned:
        manifest = plugins_module.discover().get(plugin_id)
        if manifest is not None:
            _problem, added = _inspect(manifest, offline=True)
            owned = added.get("tools", [])

    plugin_store.update(plugin_id, enabled=False)
    switched = _set_tools_enabled(owned, on=False)
    output.ok(f"{plugin_id} is disabled. Its capability grants are kept.")
    if switched:
        output.info(f"Turned off {', '.join(switched)}.")
    return 0


def cmd_revoke(args: Any) -> int:
    plugin_id = args.name.lower()
    caps.revoke(plugin_id)
    plugin_store.update(plugin_id, enabled=False)
    output.ok(
        f"{plugin_id}'s capabilities are withdrawn and it is disabled. "
        f"Enabling it again will ask."
    )
    return 0


def _consent(plugin_id: str, wanted: list[str], *, assume_yes: bool) -> bool:
    """Ask about each capability. Returns whether every one was granted.

    All-or-nothing on purpose. A plugin that declared two capabilities was
    written expecting both; loading it with one is running code in a state its
    author never tested, and the failure would land on the user as a bug.
    """
    already = caps.granted(plugin_id)
    fresh = [item for item in wanted if item not in already]
    if not fresh:
        return True

    output.console.print()
    output.console.print(f"  [cyan]{plugin_id}[/cyan] is asking to:")
    for capability in fresh:
        output.console.print(f"    • {caps.describe(capability)}")
        output.console.print(f"      [dim]{capability}[/dim]")
    output.console.print()
    output.console.print(
        "  [dim]A plugin runs as you, in this process. These gates decide what "
        "the harness hands it,[/dim]"
    )
    output.console.print(
        "  [dim]not what it is able to do — that is what reading the code is "
        "for.[/dim]"
    )
    output.console.print()

    if assume_yes:
        output.info("--yes given; granting.")
        return True
    return _ask("  Grant these? [y/N]: ")


def _ask(prompt: str) -> bool:
    """A yes/no question at the real terminal.

    Read from `/dev/tty`, not stdin. Under `curl | bash` the script's stdin is
    its own source, so `input()` reads shell source as the answer — that bug
    shipped once in the setup wizard and is not being reintroduced here.
    """
    handle = None
    try:
        handle = open("/dev/tty", "r", encoding="utf-8")
    except OSError:
        if not sys.stdin.isatty():
            output.fail(
                "Nothing to ask on: this is not a terminal.",
                "Pass --yes if you have already reviewed what is being granted.",
            )
            return False
        handle = sys.stdin

    try:
        output.console.print(prompt, end="")
        answer = handle.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        output.console.print()
        return False
    finally:
        if handle is not sys.stdin:
            try:
                handle.close()
            except OSError:
                pass
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


def _resolve_git_url(identifier: str) -> str:
    """Accept `owner/repo`, a full git URL, or a local path."""
    value = identifier.strip()
    if value.startswith(("http://", "https://", "git@", "ssh://", "file://")):
        return value
    if value.count("/") == 1 and not value.startswith((".", "/", "~")):
        return f"https://github.com/{value}.git"
    return value


def cmd_install(args: Any) -> int:
    """Clone, scan, consent, and optionally enable."""
    identifier = args.source
    ref = getattr(args, "ref", None)
    force = bool(getattr(args, "force", False))

    if plugin_index.looks_like_bare_name(identifier):
        entry = plugin_index.resolve(identifier)
        if entry is None:
            output.fail(
                f"Nothing in the index is called {identifier!r}.",
                f"`andromeda plugins search {identifier}` looks for near "
                f"matches, or pass owner/repo directly.",
            )
            return 1
        identifier = entry.repo
        # The index's pin is a default, not a floor: `--ref` always wins,
        # because someone who typed one has a reason and the index does not
        # know it.
        if ref is None:
            ref = entry.ref
        output.info(f"{entry.name} → {entry.repo} at {entry.ref[:12]}")
        output.info(plugin_index.SECURITY_NOTE)

    local = Path(identifier).expanduser()
    with tempfile.TemporaryDirectory(prefix="andromeda-plugin-") as workdir:
        staged = Path(workdir) / "plugin"
        from andromeda_agent import portable as portable_module

        if local.is_dir() and (
            (local / plugins_module.MANIFEST_FILENAME).exists()
            or portable_module.is_portable(local)
        ):
            shutil.copytree(local, staged, symlinks=False)
            resolved_ref = ""
        elif local.is_dir():
            # A directory that exists is a directory the person meant. Falling
            # through to `git clone` here reports "repository does not exist"
            # about a path they are looking straight at, which is the least
            # useful true sentence available.
            output.fail(
                f"{local} has neither a {plugins_module.MANIFEST_FILENAME} nor "
                f"a {portable_module.MANIFEST_FILENAME}, so it is not a plugin.",
                "A plugin is either a directory with plugin.yaml and an "
                "__init__.py defining register(ctx), or a plugin.json package "
                "of skills and MCP servers.",
            )
            return 1
        else:
            url = _resolve_git_url(identifier)
            output.info(f"Cloning {url}…")
            code = _clone(url, staged, ref)
            if code != 0:
                return code
            resolved_ref = _head_sha(staged)

        from andromeda_agent import portable

        is_portable = portable.is_portable(staged)
        if not (staged / plugins_module.MANIFEST_FILENAME).exists() and not is_portable:
            output.fail(
                f"That has neither a {plugins_module.MANIFEST_FILENAME} nor a "
                f"{portable.MANIFEST_FILENAME} at its root, so it is not a "
                f"plugin.",
                "A plugin is either a directory with plugin.yaml and an "
                "__init__.py defining register(ctx), or a plugin.json package "
                "of skills and MCP servers.",
            )
            return 1

        try:
            manifest = (
                plugins_module.read_portable_manifest(staged, "user")
                if is_portable
                else plugins_module.read_manifest(staged, "user")
            )
        except (plugins_module.PluginError, portable.PortableError) as exc:
            output.fail(str(exc))
            return 1

        scan = plugin_guard.scan_plugin(staged, manifest.id)
        if scan.decision == "block":
            output.fail(plugin_guard.refusal(scan))
            output.console.print(plugin_guard.format_report(scan))
            return 1
        if scan.decision == "confirm":
            output.console.print()
            output.console.print("  [yellow]⚠ The security scan flagged this plugin:[/yellow]")
            output.console.print(plugin_guard.format_report(scan))
            output.console.print()
            if force:
                output.info("--force given; continuing.")
            elif not _ask("  Install anyway? Only if you trust the source. [y/N]: "):
                output.fail(f"{manifest.id} was not installed.")
                return 1

        destination = plugins_module.user_dir() / manifest.id
        if destination.exists():
            if not force:
                output.fail(
                    f"{manifest.id} is already installed at {destination}.",
                    "Use `andromeda plugins update` to refresh it, or --force to "
                    "replace it.",
                )
                return 1
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, destination, symlinks=False)
        # `.git` is not part of what was reviewed and lets an `update` pull a
        # different tree than the ref that was scanned.
        git_dir = destination / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)

    plugin_store.update(
        manifest.id,
        enabled=False,
        source="user",
        installed_at=plugin_store.now_iso(),
        ref=resolved_ref,
        origin=identifier,
    )
    output.ok(f"Installed {manifest.id} {manifest.version} to {destination}.")
    if manifest.portable:
        # Said before consent is asked, because it changes what is being
        # consented to: there is no code in this one, so nothing it carries can
        # run in this process.
        output.info(
            "This is a portable package: skills and MCP servers, no code. "
            "Nothing in it is imported."
        )
    if scan.findings:
        output.info(scan.summary())

    enable = getattr(args, "enable", None)
    if enable is False:
        output.info(f"Not enabled. Run `andromeda plugins enable {manifest.id}`.")
        return 0
    if enable is True or _ask("  Enable it now? [y/N]: "):
        return cmd_enable(_Namespace(name=manifest.id, yes=bool(getattr(args, "yes", False))))
    output.info(f"Not enabled. Run `andromeda plugins enable {manifest.id}`.")
    return 0


class _Namespace:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _clone(url: str, destination: Path, ref: str | None) -> int:
    command = ["git", "clone", "--depth", "1"]
    if ref:
        command += ["--branch", ref]
    command += [url, str(destination)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        output.fail("git is not installed, and installing a plugin needs it.")
        return 1
    except subprocess.TimeoutExpired:
        output.fail("The clone timed out after three minutes.")
        return 1
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        output.fail(
            "The clone failed.",
            detail[-1] if detail else "git gave no reason.",
        )
        return 1
    return 0


def _head_sha(directory: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def cmd_search(args: Any) -> int:
    """Look a plugin up by name or description."""
    matched, source = plugin_index.search(args.query or "", limit=int(getattr(args, "limit", 20) or 20))

    if source != "network":
        # Said plainly rather than logged. Entries from a stale cache may name
        # versions that have since been yanked, and entries from the bundled
        # seed are whatever shipped with this install.
        output.info(f"Index source: {source}.")

    if not matched:
        output.info(
            f"Nothing in the index matches {args.query!r}."
            if args.query
            else "The index is empty."
        )
        return 0

    for entry in matched:
        output.console.print(f"  [cyan]{entry.name}[/cyan] [dim]{entry.repo}[/dim]")
        if entry.description:
            output.console.print(f"      {entry.description}")
        if entry.capabilities:
            output.console.print(
                f"      [yellow]asks for:[/yellow] {', '.join(entry.capabilities)}"
            )
    output.console.print()
    output.info(plugin_index.SECURITY_NOTE)
    return 0


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------


def cmd_pack_show(args: Any) -> int:
    try:
        pack = plugin_packs.load(Path(args.file))
    except plugin_packs.PackError as exc:
        output.fail(str(exc))
        return 1

    output.console.print(f"  [cyan]{pack.name}[/cyan] [dim]{pack.version}[/dim]")
    if pack.description:
        output.console.print(f"  {pack.description}")
    if pack.author:
        output.console.print(f"  [dim]by {pack.author}[/dim]")
    output.console.print()
    for entry in pack.entries:
        output.console.print(f"    {entry.label} [dim]@ {entry.ref[:12]}[/dim]")
    if pack.config:
        output.console.print()
        output.console.print("  seeds settings for: " + ", ".join(sorted(pack.config)))
    output.console.print()
    output.info(
        "Installing this asks for each plugin's capabilities separately. A "
        "pack cannot grant one."
    )
    return 0


def cmd_pack_install(args: Any) -> int:
    """Install every plugin in a pack, then seed their settings.

    Each one goes through the ordinary install: clone at the pinned commit,
    scan, consent, enable. A pack is a list, not an authority — nothing here
    can approve on the user's behalf.
    """
    try:
        pack = plugin_packs.load(Path(args.file))
    except plugin_packs.PackError as exc:
        output.fail(str(exc))
        return 1

    output.console.print(f"  [cyan]{pack.name}[/cyan] — {len(pack.entries)} plugin(s)")
    output.console.print()

    failed: list[str] = []
    for entry in pack.entries:
        output.console.print(f"  [dim]→ {entry.label}[/dim]")
        code = cmd_install(
            _Namespace(
                source=entry.source,
                ref=entry.ref,
                force=bool(getattr(args, "force", False)),
                enable=None if not getattr(args, "yes", False) else True,
                yes=bool(getattr(args, "yes", False)),
            )
        )
        if code != 0:
            failed.append(entry.label)

    for plugin_id, settings in pack.config.items():
        if plugin_id not in plugins_module.discover():
            output.info(f"Skipping settings for {plugin_id}: it is not installed.")
            continue
        for key, value in settings.items():
            plugin_store.set_plugin_config(plugin_id, key, value)

    output.console.print()
    if failed:
        # A partial install is reported as one, and the successful half is
        # kept. Rolling back plugins the user already consented to would undo
        # a decision they made.
        output.fail(
            f"{len(failed)} of {len(pack.entries)} did not install: "
            f"{', '.join(failed)}.",
            "The rest were installed. Re-run to retry just those.",
        )
        return 1
    output.ok(f"{pack.name} installed.")
    return 0


def cmd_pack_export(args: Any) -> int:
    """Write a pack describing what is enabled here."""
    document = plugin_packs.export(
        getattr(args, "name", None) or "my-plugins",
        getattr(args, "description", "") or "",
    )
    rendered = plugin_packs.to_yaml(document)

    destination = getattr(args, "out", None)
    if destination:
        path = Path(destination).expanduser()
        path.write_text(rendered, encoding="utf-8")
        output.ok(f"Wrote {path}.")
    else:
        output.console.print(rendered, soft_wrap=True)

    if document.get("$skipped"):
        output.info(str(document["$skipped"]))
        output.info(
            "A plugin installed from a local path has no commit to pin, and a "
            "pack that named a placeholder would fail on the machine you "
            "shared it with."
        )
    return 0


def cmd_remove(args: Any) -> int:
    plugin_id = args.name.lower()
    manifests = plugins_module.discover()
    manifest = manifests.get(plugin_id)
    if manifest is None:
        output.fail(f"No plugin called {args.name!r}.")
        return 1
    if manifest.source != "user":
        output.fail(
            f"{plugin_id} is {SOURCE_LABELS.get(manifest.source, manifest.source)}, "
            f"not installed by you, so there is nothing to remove.",
            f"`andromeda plugins disable {plugin_id}` turns it off instead.",
        )
        return 1

    _problem, added = _inspect(manifest, offline=True)
    _set_tools_enabled(added.get("tools", []), on=False)

    directory = manifest.directory
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    plugin_store.remove(plugin_id)
    output.ok(f"Removed {plugin_id} and forgot its capability grants.")
    return 0


def cmd_update(args: Any) -> int:
    """Reinstall from where it came from, keeping the enabled state."""
    plugin_id = args.name.lower()
    row = plugin_store.entry(plugin_id)
    origin = row.get("origin")
    if not origin:
        output.fail(
            f"{plugin_id} has no recorded origin, so there is nothing to update "
            f"from.",
            "Reinstall it with `andromeda plugins install <source> --force`.",
        )
        return 1

    was_enabled = plugin_store.is_enabled(plugin_id)
    code = cmd_install(
        _Namespace(
            source=str(origin),
            ref=getattr(args, "ref", None),
            force=True,
            enable=False,
            yes=bool(getattr(args, "yes", False)),
        )
    )
    if code != 0:
        return code
    if was_enabled:
        # Deliberately re-runs consent. An update that added a capability must
        # not inherit the old grant, and `cmd_enable` is where that is checked.
        return cmd_enable(_Namespace(name=plugin_id, yes=bool(getattr(args, "yes", False))))
    return 0


# ---------------------------------------------------------------------------
# Starting one
# ---------------------------------------------------------------------------


def cmd_new(args: Any) -> int:
    """Write a working plugin to start from. See `plugin_scaffold`."""
    from andromeda_agent import plugin_scaffold

    plugin_id = str(args.name).strip().lower()
    if not plugins_module.PLUGIN_ID_RE.match(plugin_id):
        output.fail(
            f"{args.name!r} is not usable as a plugin id.",
            "Lowercase letters, digits, '.', '_' or '-', starting with a "
            "letter or digit, at most 64 characters.",
        )
        return 1

    directory = Path(getattr(args, "into", None) or ".").expanduser() / plugin_id
    if directory.exists() and any(directory.iterdir()):
        output.fail(
            f"{directory} already exists and is not empty.",
            "Pass --into to write it somewhere else.",
        )
        return 1

    description = (
        getattr(args, "description", "") or ""
    ).strip() or f"The {plugin_id} plugin."

    directory.mkdir(parents=True, exist_ok=True)
    for name, content in plugin_scaffold.files(plugin_id, description).items():
        (directory / name).write_text(content, encoding="utf-8")

    output.ok(f"Wrote {directory}.")
    output.console.print()
    output.console.print(f"  [dim]andromeda plugins doctor {directory}[/dim]")
    output.console.print(f"  [dim]andromeda plugins install {directory} --enable[/dim]")
    return 0


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: Any) -> int:
    """Load a plugin through the real runtime and report what failed.

    The network is disabled for the duration. A plugin that phones home at
    import time is exactly what a person runs this to find out about, and doing
    it for them while checking would defeat the check.
    """
    from andromeda_agent import portable

    target = Path(getattr(args, "path", ".") or ".").expanduser().resolve()
    if not (target / plugins_module.MANIFEST_FILENAME).exists():
        if portable.is_portable(target):
            return _doctor_portable(target, args)
        output.fail(
            f"{target} has no {plugins_module.MANIFEST_FILENAME}.",
            "Run this from a plugin directory, or pass its path.",
        )
        return 1

    problems: list[str] = []
    notes: list[str] = []

    try:
        manifest = plugins_module.read_manifest(target, "project")
    except plugins_module.PluginError as exc:
        output.fail(str(exc))
        return 1

    output.console.print(f"  [cyan]{manifest.id}[/cyan] [dim]{manifest.version}[/dim]")
    output.console.print()

    if not manifest.description:
        notes.append("no `description:` — `plugins list` will show a bare name")
    if manifest.unknown_fields:
        notes.append(
            f"unrecognised manifest fields (typo?): {', '.join(manifest.unknown_fields)}"
        )
    if manifest.unknown_capabilities:
        notes.append(
            f"capabilities this version does not know: "
            f"{', '.join(manifest.unknown_capabilities)}"
        )
    if manifest.api_version > plugins_module.SUPPORTED_API_VERSION:
        problems.append(
            f"api_version {manifest.api_version} is newer than this install's "
            f"{plugins_module.SUPPORTED_API_VERSION}"
        )
    if not (target / "__init__.py").exists():
        problems.append("no __init__.py, so there is nothing to import")

    scan = plugin_guard.scan_plugin(target, manifest.id)
    if scan.decision == "block":
        problems.append(f"security scan: {scan.summary()}")
    elif scan.findings:
        notes.append(f"security scan: {scan.summary()}")

    if not problems:
        loaded = _trial_load(target, manifest)
        if loaded:
            problems.append(loaded)

    for problem in problems:
        output.console.print(f"  [red]✗[/red] {problem}")
    for note in notes:
        output.console.print(f"  [yellow]![/yellow] {note}")
    if not problems:
        if notes:
            output.console.print()
        output.console.print("  [green]✓[/green] it loads")

    if scan.findings and getattr(args, "verbose", False):
        output.console.print()
        output.console.print(plugin_guard.format_report(scan, limit=50))

    return 1 if problems else 0


def _doctor_portable(target: Path, args: Any) -> int:
    """Check a `plugin.json` package.

    Shorter than the Python path and structurally so: there is nothing to
    import, so the only questions are whether the manifest parses and whether
    each part it names is usable.
    """
    from andromeda_agent import portable

    try:
        package = portable.load(target)
    except portable.PortableError as exc:
        output.fail(str(exc))
        return 1

    output.console.print(f"  [cyan]{package.name}[/cyan] [dim]{package.version}[/dim]")
    output.console.print("  [dim]portable — skills and MCP servers, no code[/dim]")
    output.console.print()

    parts = []
    if package.skills:
        parts.append(f"{len(package.skills)} skill{'' if len(package.skills) == 1 else 's'}")
    if package.mcp_servers:
        count = len(package.mcp_servers)
        parts.append(f"{count} MCP server{'' if count == 1 else 's'}")
    if parts:
        output.console.print(f"  [dim]carries:[/dim] {', '.join(parts)}")
    for skill in package.skills:
        output.console.print(f"    [dim]{package.name}:{skill.name}[/dim]")
    for name in sorted(package.mcp_servers):
        output.console.print(f"    [dim]{name}[/dim]")

    for note in package.notes:
        output.console.print(f"  [yellow]![/yellow] {note.scope}: {note.message}")
    if package.unknown_fields:
        output.console.print(
            "  [yellow]![/yellow] fields this version does not read: "
            + ", ".join(package.unknown_fields)
        )

    if package.empty:
        output.console.print()
        output.console.print(
            "  [yellow]![/yellow] it carries nothing, so enabling it does nothing"
        )
        return 1

    output.console.print()
    output.console.print("  [green]✓[/green] it reads")
    return 0


def _inspect(manifest: Any, *, offline: bool) -> tuple[str, dict[str, Any]]:
    """Load a plugin into a throwaway manager. Returns (problem, what it added).

    Used by two commands with different reasons for wanting the same thing.
    `doctor` wants to know whether it works; `enable` wants to know which tool
    names to switch on, and to be able to say so before anyone consents.

    The grant is faked for the duration, in memory only, so a developer can
    check their own plugin without consenting to it first and so `enable` can
    see what a plugin *would* register. Nothing is written to the ledger.

    `offline` cuts the network. A plugin that phones home at import time is
    exactly what `doctor` is run to find out about, and doing it for them while
    checking would defeat the check. `enable` leaves it on: the user has
    already decided to run this code, and a provider plugin that probes its
    endpoint at import would otherwise fail here and nowhere else.
    """
    import socket

    real_socket = socket.socket
    real_create = socket.create_connection

    def _refuse(*_args: Any, **_kwargs: Any):
        raise RuntimeError("network access is disabled while plugins doctor runs")

    trial = plugins_module.PluginManager()
    granted_real = caps.granted
    if getattr(manifest, "portable", False):
        # Nothing to import and no grants to fake. Loaded through the same
        # manager so the caller's summary counts what it carries.
        entry = trial._load_one(manifest)
        added = {"tools": [], "skills": sorted(trial.skills()), "summary": _summarise(trial)}
        error = entry.error
        trial.unload()
        return error, added

    def _granted(plugin_id: str) -> frozenset[str]:
        if plugin_id == manifest.id:
            return frozenset(manifest.capabilities)
        return granted_real(plugin_id)

    if offline:
        socket.socket = _refuse  # type: ignore[assignment]
        socket.create_connection = _refuse  # type: ignore[assignment]
    caps.granted = _granted  # type: ignore[assignment]
    try:
        entry = trial._load_one(manifest)
        added = {
            "tools": sorted(trial.tools()),
            "commands": sorted(trial.commands()),
            "cli_commands": sorted(trial.cli_commands()),
            "skills": sorted(trial.skills()),
            "summary": _summarise(trial),
        }
        return entry.error, added
    except Exception as exc:  # noqa: BLE001 - a trial load must not raise at the user
        return f"loading it raised: {exc}", {}
    finally:
        if offline:
            socket.socket = real_socket  # type: ignore[assignment]
            socket.create_connection = real_create  # type: ignore[assignment]
        caps.granted = granted_real  # type: ignore[assignment]
        trial.unload()


def _trial_load(target: Path, manifest: Any) -> str:
    """`doctor`'s half: report the problem, print what it registered."""
    problem, added = _inspect(manifest, offline=True)
    if problem:
        return problem
    if added.get("summary"):
        output.console.print(f"  [dim]registers:[/dim] {added['summary']}")
    else:
        output.console.print(
            "  [yellow]![/yellow] register(ctx) ran and registered nothing"
        )
    return ""


def _set_tools_enabled(names: list[str], *, on: bool) -> list[str]:
    """Add or remove tool names in `enabled_tools`. Returns what changed.

    `enabled_tools` is an allowlist, so a plugin tool that is not in it is a
    tool the model is never offered — the plugin registers, the tool exists,
    and nothing ever calls it. Asking the user to enable a plugin and then
    enable each of its tools separately is two consents for one decision, and
    the second one is invisible until they wonder why nothing happened.

    So enabling a plugin switches its tools on, and disabling switches them
    back off. `andromeda tools disable <name>` still works on them afterwards,
    because by then they are ordinary entries in the same list.
    """
    from .. import config as config_module

    if not names:
        return []
    current = list(config_module.load()["enabled_tools"])
    changed: list[str] = []
    for name in names:
        if on and name not in current:
            current.append(name)
            changed.append(name)
        elif not on and name in current:
            current.remove(name)
            changed.append(name)
    if changed:
        config_module.set_value("enabled_tools", ",".join(sorted(set(current))))
    return changed


def _summarise(manager: Any) -> str:
    """What a plugin registered, in one line.

    This is read at the moment somebody decides whether to enable it, so it
    counts *everything*. A summary that lists a plugin's one slash command and
    stays silent about its middleware and its delegation lane understates the
    thing being consented to, which is the one direction a consent screen must
    never be wrong in.
    """
    counts = (
        ("tool", len(manager.tools())),
        ("command", len(manager.commands())),
        ("cli command", len(manager.cli_commands())),
        ("skill", len(manager.skills())),
        ("memory backend", len(manager.memory_backends())),
        ("cron provider", len(manager.cron_providers())),
        ("model provider", len(manager.model_providers())),
        ("browser provider", len(manager.browser_providers())),
        ("search provider", len(manager.web_search_providers())),
        ("secret source", len(manager.secret_sources())),
        ("delivery mode", len(manager.delivery_modes())),
        ("language server", len(manager.lsp_servers())),
        ("delegation lane", len(manager.specialists())),
        ("blueprint", len(manager.blueprints())),
        ("eval", len(manager.evals())),
        ("auxiliary task", len(manager.auxiliary_tasks())),
        ("approval transport", len(manager.approval_transports())),
        ("MCP server", len(manager.mcp_servers())),
        ("prompt section", len(manager.prompt_sections())),
        ("redaction pattern", len(manager.redaction_patterns())),
    )

    parts = [
        f"{count} {label}{'' if count == 1 else 's'}"
        for label, count in counts
        if count
    ]

    from andromeda_agent import middleware as middleware_module

    kinds = sorted(
        kind
        for kind in middleware_module.VALID_KINDS
        if manager.middleware(kind)
    )
    if kinds:
        parts.append(f"middleware on {', '.join(kinds)}")

    events = sorted(
        {event for entries in manager._hooks_by_plugin.values() for event, _ in entries}
    )
    if events:
        parts.append(f"hooks on {', '.join(events)}")
    return ", ".join(parts)
