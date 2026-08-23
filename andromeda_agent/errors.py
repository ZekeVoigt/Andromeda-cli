"""Errors the user can act on.

The relay speaks OpenAI's error envelope, so a raw SDK exception already
carries a usable ``message``. What it does not carry is what to *do*, and the
distinction that matters most — out of credit versus something broke — is a
status code the SDK buries. These map it back out.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """Anything the CLI should print as a plain message, not a traceback."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class NotSignedIn(AgentError):
    def __init__(self) -> None:
        super().__init__(
            "This device is not paired with an Andromeda account.",
            hint="Run `andromeda auth login` and paste the code from the app.",
        )


class OutOfCredit(AgentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, hint="Top up in the app, then try again.")


def from_status(status: int, message: str) -> AgentError:
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
    if status == 403:
        return AgentError(
            message or "That request is not permitted on this account.",
        )
    if status == 503:
        return AgentError(
            message or "Andromeda's model service is not configured.",
            hint="This is a server-side problem, not a local one.",
        )
    if status in (502, 504):
        return AgentError(
            message or "Andromeda could not reach the model service.",
            hint="Usually transient — try again.",
        )
    return AgentError(message or f"The model service returned HTTP {status}.")
