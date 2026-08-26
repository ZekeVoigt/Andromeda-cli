"""Errors the user can act on.

The relay speaks OpenAI's error envelope, so a raw SDK exception already
carries a usable ``message``. What it does not carry is what to *do*, and the
distinction that matters most — out of credit versus something broke — is a
status code the SDK buries. These map it back out.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """Anything the CLI should print as a plain message, not a traceback.

    Three fields ride along for the retry layer, and all three are optional so
    every existing raise site keeps working unchanged:

    `status` is the HTTP status this came from, kept because deciding whether
    to try again is a question about the code and the prose has already thrown
    it away. `retry_after` is what the provider asked for, in seconds, when it
    asked. `partial` is whatever text had already been streamed when the
    failure happened — an answer that died at ninety per cent is worth ninety
    per cent more than nothing, and without this it is discarded.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        status: int = 0,
        retry_after: float | None = None,
        partial: str = "",
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.status = status
        self.retry_after = retry_after
        self.partial = partial


class NotSignedIn(AgentError):
    def __init__(self) -> None:
        super().__init__(
            "This machine is not signed in to an Andromeda account.",
            hint="Run `andromeda auth login` — it signs you in through your browser.",
        )


class OutOfCredit(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, hint="Top up in the app, then try again.")


def from_status(
    status: int, message: str, *, retry_after: float | None = None
) -> AgentError:
    """Translate a relay status into something the terminal can act on.

    402 is the relay's deliberate signal for "you have run out", kept distinct
    from 403 so a client can tell depletion from a policy refusal without
    parsing prose. Do NOT infer depletion from a zero balance anywhere else:
    a pending renewal reads as zero while access is still live.
    """
    if status == 401:
        return NotSignedIn()
    if status == 402:
        return OutOfCredit(message or "You are out of credit.")
    if status == 429:
        # Named rather than left to the generic branch, because "slow down" is
        # the one failure a person can act on by waiting, and it is the one the
        # retry layer above needs to recognise by status rather than by prose.
        return AgentError(
            message or "The model service is rate-limiting this account.",
            hint="Usually transient — it will be retried automatically.",
            status=status,
            retry_after=retry_after,
        )
    if status == 403:
        if "free hosted execution is not enabled" in message.lower():
            return AgentError(
                "Hosted chat on the Free plan is not available yet.",
                hint=(
                    "Use `andromeda config set provider direct` with your own "
                    "OpenRouter key (provider charges apply), or upgrade for "
                    "hosted Andromeda Credits."
                ),
            )
        return AgentError(
            message or "That request is not permitted on this account.",
            status=status,
        )
    if status == 503:
        return AgentError(
            message or "Andromeda's model service is not configured.",
            hint="This is a server-side problem, not a local one.",
            status=status,
        )
    if status in (408, 409, 500, 502, 504):
        return AgentError(
            message or "Andromeda could not reach the model service.",
            hint="Usually transient — try again.",
            status=status,
            retry_after=retry_after,
        )
    return AgentError(
        message or f"The model service returned HTTP {status}.", status=status
    )
