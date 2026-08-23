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
    raise AgentError(
        f"Unknown provider {lane!r}.",
        hint="Set one of: relay, direct — `andromeda config set provider relay`",
    )
