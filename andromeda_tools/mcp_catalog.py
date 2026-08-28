"""A short list of servers that can be connected with one command.

The client in `mcp.py` reaches any MCP server there is. That is not the same as
a person being *able* to connect one, and for a long time it was the whole gap:
you had to already know that a vendor ships a server, know its URL, and hand-
write JSON into a file the CLI only ever told you the path of. Nobody discovers
an integration that way.

A catalog entry is a small YAML file recording the three things you would
otherwise have to go and find — where the server is, how it authenticates, and
anything it needs in its environment. `andromeda mcp install stripe` reads one
and writes the config for you.

**The catalog is a convenience, never a permission.** Everything installed from
it goes through the same `mcp_security` screen and lands in the same
`mcp.json` as a server you added by hand, with tools tiered `outbound` and
gated exactly the same way. Being on the list means somebody checked the URL,
not that the server is trusted.

**Not being on the list means nothing at all.** `andromeda mcp add` takes any
server, and the catalog exists so that the common cases do not require it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import mcp_config

MANIFEST_VERSION = 1

# `${INSTALL_DIR}` in a command or argument, substituted at install time with
# wherever a git-installed server was cloned to. Only that one name: a manifest
# is not a template language, and every other `${…}` is left alone so a server
# that wants a literal one can have it.
INSTALL_DIR = re.compile(r"\$\{INSTALL_DIR\}")


class CatalogError(RuntimeError):
    """A manifest that cannot be used, said in a sentence."""


@dataclass(frozen=True)
class EnvVar:
    """One thing the server needs in its environment before it will start."""

    name: str
    prompt: str = ""
    default: str = ""
    required: bool = True
    # Whether the value is a credential. Decides whether it is echoed while
    # being typed, and whether the install offers to store it as a secret
    # reference rather than as text in `mcp.json`.
    secret: bool = False
    # For remote servers only: the HTTP header this value is sent in, and the
    # prefix it carries. Stated by the manifest rather than guessed, because
    # a server that wants `X-Api-Key: abc` will not accept `Authorization:
    # Bearer abc`, and there is no way to tell which it is from the value.
    header: str = ""
    prefix: str = ""

    @property
    def question(self) -> str:
        return self.prompt or self.name


@dataclass(frozen=True)
class Entry:
    name: str
    description: str
    source: str = ""
    # "http" or "stdio".
    transport: str = "http"
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    # "oauth", "api_key", "header" or "none".
    auth: str = "none"
    env: tuple[EnvVar, ...] = ()
    # Tools to switch on by default. Empty means all of them.
    default_tools: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    after: str = ""
    install_url: str = ""
    install_ref: str = ""
    bootstrap: tuple[str, ...] = ()

    @property
    def needs_clone(self) -> bool:
        return bool(self.install_url)

    @property
    def summary(self) -> str:
        """One line for a list, transport and auth included.

        Which of those two matters depends on the reader — "does this run on my
        machine" and "will this open a browser" are the two questions people
        actually have before installing one.
        """
        parts = [self.transport]
        if self.auth != "none":
            parts.append(self.auth)
        return f"{self.description} ({', '.join(parts)})"


def directory() -> Path:
    """Where the shipped manifests live."""
    return Path(__file__).resolve().parent / "catalog"


def _strings(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _env_var(raw: Any) -> EnvVar:
    """One `auth.env` entry, in either of the two spellings a manifest may use.

    A bare string is the common case — the variable is required, is a secret,
    and the prompt is its own name. The mapping form exists for the ones that
    need a default or are not credentials, like a base URL.
    """
    if isinstance(raw, str):
        return EnvVar(name=raw, secret=True, header="Authorization", prefix="Bearer ")
    if not isinstance(raw, dict):
        raise CatalogError(f"an `auth.env` entry should be a name or a mapping, not {raw!r}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise CatalogError("an `auth.env` entry has no `name`")
    return EnvVar(
        name=name,
        prompt=str(raw.get("prompt") or ""),
        default=str(raw.get("default") or ""),
        required=bool(raw.get("required", True)),
        secret=bool(raw.get("secret", True)),
        header=str(raw.get("header") or ""),
        prefix=str(raw.get("prefix") or ""),
    )


def parse(path: Path) -> Entry:
    """One manifest file. Raises `CatalogError` with the filename on anything wrong."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise CatalogError(f"{path.name} could not be read: {exc}") from exc

    if not isinstance(raw, dict):
        raise CatalogError(f"{path.name} should hold a mapping")

    version = raw.get("manifest_version")
    if version is not None and int(version) > MANIFEST_VERSION:
        raise CatalogError(
            f"{path.name} is manifest version {version}; this build reads "
            f"up to {MANIFEST_VERSION}. Update the CLI."
        )

    name = str(raw.get("name") or path.stem).strip()
    description = str(raw.get("description") or "").strip()
    if not description:
        raise CatalogError(f"{path.name} has no `description`")

    transport = raw.get("transport")
    if not isinstance(transport, dict):
        raise CatalogError(f"{path.name} has no `transport` block")
    kind = str(transport.get("type") or "").strip().lower()
    if kind not in {"http", "stdio"}:
        raise CatalogError(f"{path.name}: `transport.type` should be `http` or `stdio`")

    url = str(transport.get("url") or "").strip()
    command = str(transport.get("command") or "").strip()
    if kind == "http" and not url:
        raise CatalogError(f"{path.name}: an http transport needs a `url`")
    if kind == "stdio" and not command:
        raise CatalogError(f"{path.name}: a stdio transport needs a `command`")

    auth_block = raw.get("auth")
    auth_block = auth_block if isinstance(auth_block, dict) else {}
    auth = str(auth_block.get("type") or "none").strip().lower()
    if auth not in {"oauth", "api_key", "header", "none"}:
        raise CatalogError(f"{path.name}: unknown `auth.type` {auth!r}")

    install = raw.get("install")
    install = install if isinstance(install, dict) else {}
    if install and str(install.get("type") or "git").lower() != "git":
        raise CatalogError(f"{path.name}: only `install.type: git` is supported")
    install_url = str(install.get("url") or "").strip()
    install_ref = str(install.get("ref") or "").strip()
    if install_url and not re.fullmatch(r"[0-9a-f]{40}", install_ref):
        # A branch or a tag can be moved after review; a commit cannot. An
        # entry that pins anything else is pinning nothing.
        raise CatalogError(
            f"{path.name}: `install.ref` must be a full 40-character commit SHA, "
            f"not {install_ref!r}"
        )

    tools = raw.get("tools")
    tools = tools if isinstance(tools, dict) else {}
    suggest = raw.get("suggest")
    suggest = suggest if isinstance(suggest, dict) else {}

    return Entry(
        name=name,
        description=description,
        source=str(raw.get("source") or "").strip(),
        transport=kind,
        url=url,
        command=command,
        args=_strings(transport.get("args")),
        auth=auth,
        env=tuple(_env_var(item) for item in (auth_block.get("env") or [])),
        default_tools=_strings(tools.get("default_enabled")),
        keywords=_strings(suggest.get("keywords")),
        hosts=_strings(suggest.get("hosts")),
        after=str(raw.get("post_install") or "").strip(),
        install_url=install_url,
        install_ref=install_ref,
        bootstrap=_strings(install.get("bootstrap")),
    )


def entries() -> list[Entry]:
    """Every readable manifest, by name.

    A manifest that fails to parse is skipped rather than taking the catalog
    down with it — one bad file shipped in a release must not make `mcp
    catalog` unusable. `problems()` is what reports them.
    """
    out: list[Entry] = []
    for path in sorted(directory().glob("*.yaml")):
        try:
            out.append(parse(path))
        except CatalogError:
            continue
    return out


def problems() -> list[tuple[str, str]]:
    """`(filename, reason)` for every manifest that could not be read."""
    out: list[tuple[str, str]] = []
    for path in sorted(directory().glob("*.yaml")):
        try:
            parse(path)
        except CatalogError as exc:
            out.append((path.name, str(exc)))
    return out


def get(name: str) -> Entry | None:
    wanted = (name or "").strip().lower()
    for entry in entries():
        if entry.name.lower() == wanted:
            return entry
    return None


def search(text: str) -> list[Entry]:
    """Entries matching a word, against name, description, keywords and hosts.

    Hosts are in there so that pasting a domain finds the server for it — the
    thing somebody has in their clipboard when they wonder whether this is
    connectable is usually a URL.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return entries()
    out = []
    for entry in entries():
        haystack = " ".join(
            [entry.name, entry.description, *entry.keywords, *entry.hosts]
        ).lower()
        if needle in haystack:
            out.append(entry)
    return out


def suggest_for(text: str) -> list[Entry]:
    """Entries a piece of text implies, for "you could connect this" prompts.

    Matched on keywords and hosts only, never on the description: "payments"
    appearing in a sentence should not offer to install Stripe, but the word
    "stripe" or the domain `dashboard.stripe.com` reasonably does.
    """
    lowered = (text or "").lower()
    if not lowered:
        return []
    out = []
    for entry in entries():
        triggers = [*entry.keywords, *entry.hosts]
        if any(trigger.lower() in lowered for trigger in triggers if trigger):
            out.append(entry)
    return out


def config_for(entry: Entry, values: dict[str, str], install_dir: Path | None = None) -> dict[str, Any]:
    """The `mcp.json` block this entry becomes.

    `values` are the answers to `entry.env` — either literal values or secret
    references, which `mcp.py` resolves at connect time either way.
    """
    def expand(text: str) -> str:
        if install_dir is None:
            return text
        return INSTALL_DIR.sub(str(install_dir), text)

    config: dict[str, Any] = {}
    if entry.transport == "http":
        config["url"] = entry.url
        if entry.auth == "oauth":
            config["auth"] = "oauth"
        elif entry.auth == "header" and values:
            headers: dict[str, str] = {}
            for spec in entry.env:
                value = values.get(spec.name)
                if not value:
                    continue
                # A spec with no `header` is passed under its own name. That is
                # the honest default: the manifest said what to call it.
                headers[spec.header or spec.name] = f"{spec.prefix}{value}"
            if headers:
                config["headers"] = headers
    else:
        config["command"] = expand(entry.command)
        if entry.args:
            config["args"] = [expand(arg) for arg in entry.args]
        if values:
            config["env"] = dict(values)

    if entry.default_tools:
        # A shipped default, not a ceiling. `mcp configure` rewrites it, and an
        # absent list means every tool the server advertises.
        config["tools"] = {"include": list(entry.default_tools)}
    if entry.source:
        config["source"] = entry.source
    return config


def installed(home: Path) -> dict[str, dict[str, Any]]:
    """Configured servers whose name matches a catalog entry."""
    known = {entry.name for entry in entries()}
    return {
        name: config
        for name, config in mcp_config.servers(home).items()
        if name in known
    }
