"""Looking at an image.

Routed to an auxiliary model, because the conversation model is text-only. What
comes back is a *description* the agent can reason about — the image itself
never enters the transcript, which keeps a screenshot from costing thousands of
tokens on every subsequent turn.

`browser_screenshot` deliberately does not exist. The browser is driven by refs
against a structured snapshot, and that is not a limitation to work around: a
model reasoning about a page from pixels clicks whatever is actually at the
coordinates it guessed. This tool is for images that *are* the subject — a
design mock, a chart in a PDF, a photo of a whiteboard — not for reading a UI
the harness can already read properly.
"""

from __future__ import annotations

from pathlib import Path

from .spec import ToolResult, failure
from .workspace import PathOutsideWorkspace, Workspace

DEFAULT_PROMPT = (
    "Describe this image in enough detail that someone who cannot see it could "
    "act on it. Include any text verbatim. If it is a chart or a diagram, state "
    "the values and the relationships, not just that it is a chart."
)


def analyze(
    workspace: Workspace,
    auxiliary,
    path: str,
    prompt: str = "",
) -> ToolResult:
    if auxiliary is None:
        return failure(
            "No vision model is configured for this build, so images cannot be read."
        )

    try:
        target = workspace.resolve(path)
    except PathOutsideWorkspace as exc:
        return failure(str(exc))

    try:
        from andromeda_agent.auxiliary import read_image

        data, mime_type = read_image(target)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return failure(str(exc))

    question = (prompt or "").strip() or DEFAULT_PROMPT

    try:
        description = auxiliary.ask(question, image=data, mime_type=mime_type)
    except Exception as exc:  # noqa: BLE001 - a failed side call is a result
        return failure(f"Could not read {workspace.relative(target)}: {exc}")

    # Stripped here rather than trusting the caller: a whitespace-only answer is
    # an empty answer, and returning it as a description would have the agent
    # act on nothing.
    description = (description or "").strip()
    if not description:
        return failure(f"The vision model returned nothing for {workspace.relative(target)}.")

    return ToolResult(
        content=description,
        display=f"{workspace.relative(target)} — {len(description)} chars",
        metadata={"bytes": len(data), "mime_type": mime_type},
    )
