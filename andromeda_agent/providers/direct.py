"""The BYOK lane."""

from __future__ import annotations

import os
from typing import Any

from openai import OpenAI

from ..errors import AgentError
from .base import Provider


def build_direct(config: dict[str, Any]) -> Provider:
    env_name = str(config.get("direct_api_key_env") or "OPENROUTER_API_KEY")
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise AgentError(
            f"No API key found in ${env_name}.",
            hint=(
                f"Export it (`export {env_name}=...`), or switch lanes with "
                "`andromeda config set provider relay`."
            ),
        )

    base = str(config.get("direct_base_url") or "").rstrip("/")
    if not base:
        raise AgentError("`direct_base_url` is not set.")

    return Provider(
        name="direct",
        model=str(config.get("model")),
        thinking=str(config.get("thinking") or "off"),
        client=OpenAI(base_url=base, api_key=key, max_retries=2),
        label=f"BYOK ({base})",
    )
