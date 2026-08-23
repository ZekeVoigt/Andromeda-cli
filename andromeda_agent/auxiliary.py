"""A second model, for what the conversation model cannot do.

The model lock governs the model that *reasons* — the one whose output the user
reads and whose tokens dominate the bill. Some tools need something else: the
locked model is text-only, so describing an image is not a matter of prompting
it better, it is a capability it does not have.

The rules:

  - An auxiliary model is reachable **only** through the tool that needs it. It
    is never selectable as the conversation model, so `andromeda model` and the
    lock are unaffected.
  - It gets one call and one answer. No tools, no loop, no conversation — a side
    model that can call tools is a second agent nobody is watching.
  - It costs more than the main model, so the tool that uses it says so.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import APIStatusError, OpenAI

from .errors import AgentError, from_status
from .models import auxiliary_model

MAX_IMAGE_BYTES = 8_000_000
DEFAULT_MAX_TOKENS = 1_500

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@dataclass
class Auxiliary:
    """A bound client for one purpose. Built alongside the main provider."""

    purpose: str
    model: str
    client: OpenAI

    def ask(
        self,
        prompt: str,
        image: bytes | None = None,
        mime_type: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
                # Not streamed: nobody is watching a side call, and the caller
                # wants the whole answer before it can do anything with it.
                stream=False,
            )
        except APIStatusError as exc:
            raise from_status(exc.status_code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as a message
            raise AgentError(f"The {self.purpose} model failed: {exc}") from exc

        choices = response.choices or []
        return (choices[0].message.content or "").strip() if choices else ""


def build(purpose: str, provider) -> Auxiliary | None:
    """An auxiliary bound to the same lane the conversation is using.

    Same client, same credentials, same relay: an auxiliary that reached a
    different endpoint would be a second billing path, and the relay is the only
    thing allowed to spend credit.
    """
    model = auxiliary_model(purpose)
    if model is None:
        return None

    # A provider with no client cannot make a side call, so there is no
    # auxiliary to build. Read rather than required, so a lane or a test double
    # that only implements `stream_turn` still works — it simply has no vision.
    client = getattr(provider, "client", None)
    if client is None:
        return None
    return Auxiliary(purpose=purpose, model=model, client=client)


def read_image(path: Path) -> tuple[bytes, str]:
    """Load an image file, or say precisely why not."""
    if not path.exists():
        raise AgentError(f"{path} does not exist.")
    if path.is_dir():
        raise AgentError(f"{path} is a directory.")

    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise AgentError(
            f"{path.name} is {size / 1_000_000:.1f}MB — too large to send. "
            "Resize it below 8MB."
        )

    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type not in IMAGE_TYPES:
        raise AgentError(
            f"{path.name} is {mime_type or 'an unknown type'}. "
            f"Supported: {', '.join(sorted(IMAGE_TYPES))}."
        )
    return path.read_bytes(), mime_type
