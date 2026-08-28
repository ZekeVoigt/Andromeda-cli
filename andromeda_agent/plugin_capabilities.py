"""What a plugin is allowed to take over, and how it asks.

**This is not a sandbox, and nothing here pretends otherwise.** A plugin is
Python imported into this process. It can `import os`, monkey-patch the loop,
and ignore every check in this file. Saying so plainly matters more than the
mechanism does: a capability model described as a security boundary is worse
than none, because people install things they would otherwise read first.

What this *is*: the host decides which of its own seams it hands out. Every
capability below maps one-to-one onto a place in this codebase that would
otherwise be taken silently — the tool registry, the model provider, the secret
resolver, the scheduler, the memory backend, the system prompt, the slash
commands. Ungranted, the registration is refused and the plugin is told why.

Two rules govern this list, and both are why it is short:

**No capability without an enforcing gate.** Every id here is checked at a real
call site in `plugins.py`; `tests/test_plugin_capabilities.py` asserts that.
An id that reads like a permission and gates nothing teaches people that
consent screens are noise.

**Consent is to a set, not to a plugin.** The grant records a hash of exactly
the ids that were declared when the person said yes. An update that adds one
changes the hash, and the plugin stops loading until they look again. Without
that, "installed once" becomes permanent authority over whatever the plugin
decides to want next.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from . import plugin_store


@dataclass(frozen=True)
class CapabilitySpec:
    """One capability, and the seam it opens."""

    id: str
    #: What the user is agreeing to, in words that name the consequence rather
    #: than the mechanism. This string is what a person reads at the moment
    #: they decide, so it says what the plugin can *do to them*.
    description: str
    #: The `ctx` method this gates. Named so `plugins capabilities` can print
    #: it and so the enforcement test can find the call site.
    gate: str


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        id="tools.override",
        gate="ctx.register_tool(override=True)",
        description=(
            "Replace a built-in tool. Everything the model routes through that "
            "name — including `terminal` and `write_file` — goes to this "
            "plugin's code instead"
        ),
    ),
    CapabilitySpec(
        id="model.provider",
        gate="ctx.register_model_provider",
        description=(
            "Answer as the model provider. It sees every prompt, every tool "
            "result, and whichever credential this install signs in with"
        ),
    ),
    CapabilitySpec(
        id="secrets.source",
        gate="ctx.register_secret_source",
        description=(
            "Resolve `secrets:` references. It is asked for your secrets by "
            "name and returns their values"
        ),
    ),
    CapabilitySpec(
        id="cron.provider",
        gate="ctx.register_cron_provider",
        description=(
            "Decide when scheduled jobs are due. A provider that never fires "
            "is a scheduler that silently stops"
        ),
    ),
    CapabilitySpec(
        id="memory.backend",
        gate="ctx.register_memory_backend",
        description=(
            "Own where memories are stored and which ones recall finds"
        ),
    ),
    CapabilitySpec(
        id="prompt.inject",
        gate="ctx.register_system_prompt_section",
        description=(
            "Add text to the system prompt, inside a bounded region, on every "
            "single turn"
        ),
    ),
    CapabilitySpec(
        id="commands.override",
        gate="ctx.register_command(override=True)",
        description=(
            "Replace a built-in slash command, so typing it runs this plugin"
        ),
    ),
    CapabilitySpec(
        id="model.auxiliary",
        gate="ctx.register_auxiliary_task / ctx.llm",
        description=(
            "Make side calls to a model on your credential, outside the "
            "conversation — you see the bill, not the calls"
        ),
    ),
    CapabilitySpec(
        id="browser.provider",
        gate="ctx.register_browser_provider",
        description=(
            "Answer as the browser. Every page the agent opens, and every "
            "session cookie it carries, goes through this plugin"
        ),
    ),
    CapabilitySpec(
        id="lanes.specialist",
        gate="ctx.register_specialist",
        description=(
            "Define a delegation lane, including which tools its children are "
            "allowed to call — a belt is a permission boundary"
        ),
    ),
    CapabilitySpec(
        id="approvals.transport",
        gate="ctx.register_approval_transport",
        description=(
            "Answer approval prompts. It decides what you would have said to "
            "'may I run this', and you are not necessarily there"
        ),
    ),
    CapabilitySpec(
        id="runtime.middleware",
        gate="ctx.register_middleware",
        description=(
            "Wrap every tool call and every model call — rewrite the request, "
            "retry it, cache it, or change the answer on its way back"
        ),
    ),
)

REGISTRY: dict[str, CapabilitySpec] = {spec.id: spec for spec in CAPABILITIES}
VALID_IDS: frozenset[str] = frozenset(REGISTRY)

#: Ledger field names. Kept here so `plugin_store` stays a dumb file and this
#: module owns the meaning of what is in it.
GRANTED_KEY = "granted_capabilities"
HASH_KEY = "capability_hash"
GRANTED_AT_KEY = "granted_at"


class CapabilityError(PermissionError):
    """A plugin reached for a seam it was not granted."""


def parse_declared(raw: Any) -> tuple[list[str], list[str]]:
    """Split a manifest's `capabilities:` into (known, unknown).

    Unknown ids are returned rather than raised on. A manifest written against
    a newer Andromeda must still load here — the plugin simply does not get
    the seam it asked for, and `plugins show` names what was not understood.
    Silently dropping them would make that failure invisible at exactly the
    moment someone is debugging why their plugin does nothing.
    """
    if raw is None:
        return [], []
    if isinstance(raw, str):
        items: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return [], []

    known: list[str] = []
    unknown: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        target = known if name in VALID_IDS else unknown
        if name not in target:
            target.append(name)
    return known, unknown


def set_hash(ids: Iterable[str]) -> str:
    """A stable fingerprint of a capability set.

    Sorted and deduplicated first, so reordering a manifest's list does not
    read as a change and re-prompt someone for nothing.
    """
    normalised = ",".join(sorted({str(item) for item in ids}))
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:32]}"


def granted(plugin_id: str) -> frozenset[str]:
    """What this plugin currently holds.

    A grant whose recorded hash does not match the ids it lists is discarded
    whole. That combination means the ledger was edited by hand, and a
    half-trusted grant record is not a thing this can reason about.
    """
    row = plugin_store.entry(plugin_id)
    ids = row.get(GRANTED_KEY)
    if not isinstance(ids, list):
        return frozenset()
    valid = {item for item in ids if isinstance(item, str) and item in VALID_IDS}
    recorded = row.get(HASH_KEY)
    if recorded != set_hash(valid):
        return frozenset()
    return frozenset(valid)


def is_granted(plugin_id: str, capability: str) -> bool:
    return capability in granted(plugin_id)


def grant(plugin_id: str, ids: Iterable[str]) -> frozenset[str]:
    """Record consent to exactly `ids`.

    Replaces rather than merges. A grant is a snapshot of one decision, and
    accumulating them across updates is how a plugin ends up holding a
    capability nobody remembers approving.
    """
    valid = sorted({item for item in ids if item in VALID_IDS})
    plugin_store.update(
        plugin_id,
        **{
            GRANTED_KEY: valid,
            HASH_KEY: set_hash(valid),
            GRANTED_AT_KEY: plugin_store.now_iso(),
        },
    )
    return frozenset(valid)


def revoke(plugin_id: str) -> None:
    grant(plugin_id, ())


def needs_consent(plugin_id: str, declared: Iterable[str]) -> list[str]:
    """Which declared capabilities are not yet granted.

    Empty means the plugin may load without asking. Note the direction: a
    plugin that declares *fewer* capabilities than it was granted needs no new
    consent — dropping authority is never a thing to prompt about.
    """
    wanted = {item for item in declared if item in VALID_IDS}
    return sorted(wanted - granted(plugin_id))


def describe(capability: str) -> str:
    spec = REGISTRY.get(capability)
    return spec.description if spec else capability


def require(plugin_id: str, plugin_name: str, capability: str) -> None:
    """Raise unless this plugin holds `capability`.

    The message names the exact command that would fix it, because the person
    reading it is usually not the person who wrote the plugin.
    """
    if capability not in VALID_IDS:  # pragma: no cover - programming error
        raise ValueError(f"unknown capability {capability!r}")
    if is_granted(plugin_id, capability):
        return
    spec = REGISTRY[capability]
    raise CapabilityError(
        f"Plugin {plugin_name!r} needs the {capability!r} capability to call "
        f"{spec.gate}, and it has not been granted. It would let the plugin: "
        f"{spec.description}. Run `andromeda plugins enable {plugin_id}` to "
        f"review and grant it."
    )
