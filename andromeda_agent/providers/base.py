"""The provider seam.

One shape for every lane, so the loop above never learns which one it is
talking to. Both lanes are OpenAI-compatible on the wire — the hosted relay is
shaped that way on purpose, so the client is the stock SDK rather than a
bespoke transport.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Generator

from openai import APIStatusError, OpenAI

from .. import credits
from ..errors import AgentError, from_status
from ..models import reasoning_for


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    # Set when the model emitted arguments that are not valid JSON. The call is
    # still surfaced, because the honest recovery is to hand the parse error
    # back to the model as that call's result rather than to drop it silently
    # and leave an unanswered tool_call in the transcript.
    parse_error: str = ""


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments},
                }
                for call in self.tool_calls
            ]
        return message


@dataclass
class Provider:
    name: str
    model: str
    client: OpenAI
    label: str
    # `off` by default, so nothing is sent unless it was asked for.
    thinking: str = "off"
    # The balance as of the last call, read off its response headers. Unknown
    # until a call has been made, and unknown is not zero — the BYOK lane never
    # sets it at all, because there is no account here to have a balance.
    balance: credits.Balance = field(default_factory=credits.Balance)

    def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
    ) -> Generator[str, None, AssistantTurn]:
        """Yield text deltas; return the assembled turn.

        A generator with a return value, so a caller writes
        `turn = yield from provider.stream_turn(...)` and gets both the live
        stream and the tool calls without a second channel.

        `stream=True` is not an optimization. The relay forces
        `stream_options.include_usage` upstream and settles the credit
        reservation when the stream ends, and streaming is the only way a REPL
        feels alive.
        """
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        # Two conditions, both load-bearing.
        #
        # It is sent only to a model that declares it accepts one: some
        # providers reject the whole request rather than ignoring an unknown
        # field, so a speculative `reasoning` is a broken turn, not a no-op.
        #
        # And it goes in `extra_body`, not as a named argument: `reasoning` is
        # an OpenRouter extension, and the OpenAI SDK raises TypeError on a
        # parameter it does not know.
        reasoning = reasoning_for(self.model, self.thinking)
        if reasoning is not None:
            request["extra_body"] = {"reasoning": reasoning}

        content: list[str] = []
        # Keyed by the delta's `index`, which is the only thing that ties
        # argument fragments to the call they belong to. `id` arrives once, on
        # the first fragment, and is absent from every one after it.
        partial: dict[int, dict[str, Any]] = {}
        finish_reason = ""

        try:
            # `with_raw_response` so the response headers are reachable: the
            # balance rides on them, and the plain call throws the whole
            # response away once it has the stream. `.parse()` returns exactly
            # what the plain call would have returned.
            #
            # Falling back when it is absent, rather than requiring it. The
            # balance is a display, and an SDK that does not expose headers
            # should cost someone their credit read-out, never their session.
            completions = self.client.chat.completions
            raw_client = getattr(completions, "with_raw_response", None)
            if raw_client is None:
                stream = completions.create(**request)
            else:
                raw = raw_client.create(**request)
                self.balance = credits.parse(getattr(raw, "headers", None))
                stream = raw.parse()

            for chunk in stream:
                if not chunk.choices:
                    # The final usage-only frame carries no choices. It is what
                    # the relay settles against, so it is expected, not an error.
                    continue

                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta
                text = getattr(delta, "content", None)
                if text:
                    content.append(text)
                    yield text

                for fragment in getattr(delta, "tool_calls", None) or []:
                    slot = partial.setdefault(
                        fragment.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if fragment.id:
                        slot["id"] = fragment.id
                    function = getattr(fragment, "function", None)
                    if function is not None:
                        if function.name:
                            slot["name"] = function.name
                        if function.arguments:
                            slot["arguments"] += function.arguments

        except APIStatusError as exc:
            # A refusal carries the balance too, and this is the response most
            # worth explaining to someone: "out of credit" reads very
            # differently from "out of credit, renews on the 3rd".
            response = getattr(exc, "response", None)
            self.balance = credits.parse(getattr(response, "headers", None))
            raise from_status(exc.status_code, _message_of(exc)) from exc
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a message, not a traceback
            raise AgentError(f"The request failed: {exc}") from exc

        return AssistantTurn(
            content="".join(content),
            tool_calls=[_finish_call(slot) for _, slot in sorted(partial.items())],
            finish_reason=finish_reason,
        )


def _finish_call(slot: dict[str, Any]) -> ToolCall:
    raw = slot["arguments"] or "{}"
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return ToolCall(
                id=slot["id"],
                name=slot["name"],
                arguments={},
                raw_arguments=raw,
                parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}",
            )
        return ToolCall(id=slot["id"], name=slot["name"], arguments=parsed, raw_arguments=raw)
    except json.JSONDecodeError as exc:
        return ToolCall(
            id=slot["id"],
            name=slot["name"],
            arguments={},
            raw_arguments=raw,
            parse_error=f"arguments are not valid JSON: {exc}",
        )


def _message_of(exc: APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return str(exc)
