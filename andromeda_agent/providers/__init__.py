"""Provider lanes.

`relay`  — the hosted endpoint. Holds the provider key, enforces the model
           allowlist, and reserves/settles credit per call. The CLI never sees
           a provider key and never touches the ledger.
`direct` — BYOK against any OpenAI-compatible base. The user's own key, the
           user's own bill, no Andromeda account required.
"""

from __future__ import annotations

from typing import Any

from ..errors import AgentError
from ..models import is_allowed, refusal
from .base import Provider
from .direct import build_direct
from .relay import build_relay

__all__ = ["Provider", "build_provider", "build_direct", "build_relay"]


def build_provider(config: dict[str, Any]) -> Provider:
    # The last gate before a request is made. `--model` never passes through
    # `config.set_value`, and the BYOK lane has no server-side backstop.
    model = config.get("model")
    if not is_allowed(model):
        raise AgentError(
            refusal(model),
            hint="andromeda model deepseek/deepseek-v4-flash-0731",
        )

    lane = str(config.get("provider") or "relay").strip().lower()
    if lane == "relay":
        return build_relay(config)
    if lane == "direct":
        return build_direct(config)

    factory = _plugin_providers().get(lane)
    if factory is not None:
        # The model lock above has already passed, so a plugin provider cannot
        # be used to reach a model this build refuses. What it *can* do is see
        # every prompt and spend whichever credential this install holds, which
        # is why `model.provider` is a granted capability rather than a config
        # value.
        return factory(config)

    known = ", ".join(sorted({"relay", "direct", *_plugin_providers()}))
    raise AgentError(
        f"Unknown provider {lane!r}.",
        hint=f"Set one of: {known} — `andromeda config set provider relay`",
    )


def _plugin_providers() -> dict[str, Any]:
    """Providers a plugin registered, or nothing."""
    try:
        from .. import plugins as plugins_module
    except ImportError:  # pragma: no cover - half-installed package
        return {}
    return plugins_module.model_providers()
