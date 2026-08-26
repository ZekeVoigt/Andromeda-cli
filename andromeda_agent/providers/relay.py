"""The hosted lane."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from andromeda_cli import config as config_module

from .. import redact
from ..errors import NotSignedIn
from .base import Provider

# The relay is mounted OpenAI-style, so the SDK's own path building lands on
# `/api/inference/v1/chat/completions` without any override.
RELAY_PATH = "/api/inference/v1"

# `authenticateDevice` reads the token from `Authorization: Bearer` and the id
# from this header. Both are required; a token without an id is rejected.
DEVICE_ID_HEADER = "X-Device-Id"


def build_relay(config: dict[str, Any]) -> Provider:
    credentials = config_module.load_credentials()
    if not credentials.paired:
        raise NotSignedIn()

    # The base URL recorded at pairing time wins over the config default: the
    # device token was minted by that deployment and is meaningless to another.
    base = (credentials.base_url or str(config.get("base_url") or "")).rstrip("/")

    # Registered here rather than at the leak site, because the tool call that
    # leaks a token is never the one that needed it. A relay token has no
    # vendor prefix, so exact-match is the only pass that can ever catch it.
    redact.register_known(credentials.device_token, "device-token")

    client = OpenAI(
        base_url=f"{base}{RELAY_PATH}",
        api_key=credentials.device_token,
        default_headers={DEVICE_ID_HEADER: credentials.device_id},
        max_retries=2,
    )
    return Provider(
        name="relay",
        model=str(config.get("model")),
        thinking=str(config.get("thinking") or "off"),
        client=client,
        label=f"Andromeda ({base})",
    )
