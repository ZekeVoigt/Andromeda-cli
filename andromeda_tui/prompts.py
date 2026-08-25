"""The screens that stop the agent and ask.

These are the reason the TUI is built the way it is.

The gate is blocking: `Conversation._dispatch` calls `ask_approval` and does not
continue until it has an answer. Consent that can be raced is not consent. So
while one of these is open, an agent thread is parked in `Pending.wait()` and
the *only* thing that can wake it is an answer or `AgentDriver.shutdown()`.

**Whatever owns the screen is suspended before input is read.** In the REPL
that is literal — the rich `Live` region is stopped, because it redraws over
anything printed beneath it and the question would flicker under the answer
still streaming in. That bug has been found and fixed twice, so it is worth
saying exactly how it is prevented here: these are `ModalScreen`s, which means
Textual routes every key to them and nothing else, *and* the composer is
explicitly disabled while one is open. Two mechanisms rather than one, because
the first is a property of a library we do not control and the second is a
property of this code, which a test can pin.

The other half of the same rule: an unanswered prompt is a refusal. Escape,
Ctrl-C and a UI that dies all resolve to `no`. Walking away from the keyboard
is not consent.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from andromeda_agent.approval import Answer
from andromeda_cli import render

# The answers, in the order they are offered, each with the single key that
# picks it outright. `never` has no key on purpose: it writes a standing
# refusal that survives the session, and a permanent decision should cost more
# than one keystroke sitting next to `n`. It is still reachable — arrow to it
# and press enter — because a gate you cannot permanently close is a gate that
# keeps asking, and that is how people learn to answer without reading.
APPROVAL_CHOICES: tuple[tuple[str, Answer, str], ...] = (
    ("y", "once", "allow once"),
    ("a", "session", "allow for the rest of this session"),
    ("!", "always", "always allow this tool"),
    ("n", "no", "decline"),
    ("", "never", "never allow this tool"),
)


class ApprovalScreen(ModalScreen[Answer]):
    """Consent for one tool call, stated before the call is made."""

    BINDINGS = [
        Binding("escape", "decline", "decline", show=False),
        Binding("ctrl+c", "decline", "decline", show=False),
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("enter", "take", "choose", show=False),
    ]

    def __init__(self, body: dict) -> None:
        super().__init__()
        self.body = body
        self.index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static(self._header(), id="approval-header")
            # `Text` built by hand, never a markup string: the summary is the
            # command itself, and a summary containing `[dim]` or a bracketed
            # path would otherwise be parsed as styling and shown wrong. A
            # prompt that misrenders what it is asking about is not consent.
            yield Static(self._summary(), id="approval-summary")
            if self.body.get("reason"):
                yield Static(self._reason(), id="approval-reason")
            if self.body.get("approvals"):
                yield Static(self._suggestion(), id="approval-suggestion")
            yield Static(self._choices(), id="approval-choices")

    def _header(self) -> Text:
        line = Text()
        line.append(render.eyebrow("approval"), style=f"bold {render.ZINC_200}")
        line.append("  /  ", style=f"dim {render.ZINC_200}")
        line.append(str(self.body.get("tool", "")).upper(), style=render.ZINC_50)
        line.append(
            f"  /  {str(self.body.get('tier', '')).upper()}",
            style=f"dim {render.ZINC_200}",
        )
        return line

    def _summary(self) -> Text:
        return Text(
            str(self.body.get("summary", "")),
            style=render.ZINC_50,
            no_wrap=False,
        )

    def _reason(self) -> Text:
        """Why a call the policy allowed is being asked about anyway.

        Only set when a hook escalated it. Without the line the prompt looks
        arbitrary, and an arbitrary prompt is one people clear rather than read.
        """
        return Text(
            f"HOOK / {self.body.get('reason', '')}",
            style=f"dim {render.ZINC_200}",
        )

    def _suggestion(self) -> Text:
        count = int(self.body.get("approvals") or 0)
        # A count, not a widening. The entry is only ever created by the
        # explicit `!` answer — learned trust never promotes itself.
        return Text(
            f"you have approved this {count} times — ! stops the asking",
            style=f"dim {render.ZINC_200}",
        )

    def _choices(self) -> Text:
        """One per line, not one row.

        A row wraps on a narrow terminal, and a consent prompt whose options
        wrap mid-label is a consent prompt people stop reading. Stacked, the
        selection marker is also visible without relying on colour.
        """
        block = Text()
        for position, (key, _answer, label) in enumerate(APPROVAL_CHOICES):
            selected = position == self.index
            block.append("  ")
            block.append(
                "❯ " if selected else "  ",
                style=render.ZINC_50,
            )
            block.append(
                f"{key or ' '}  ",
                style=(
                    f"bold {render.ZINC_50}"
                    if key
                    else f"dim {render.ZINC_200}"
                ),
            )
            block.append(
                label,
                style=(
                    f"bold {render.ZINC_50}"
                    if selected
                    else f"dim {render.ZINC_200}"
                ),
            )
            if not key:
                block.append("  (enter only)", style=f"dim {render.ZINC_200}")
            block.append("\n")
        block.append("  ESC DECLINES", style=f"dim {render.ZINC_200}")
        return block

    def _refresh_choices(self) -> None:
        self.query_one("#approval-choices", Static).update(self._choices())

    # ---- answering --------------------------------------------------------

    def action_move(self, delta: int) -> None:
        self.index = (self.index + delta) % len(APPROVAL_CHOICES)
        self._refresh_choices()

    def action_take(self) -> None:
        self.dismiss(APPROVAL_CHOICES[self.index][1])

    def action_decline(self) -> None:
        self.dismiss("no")

    def on_key(self, event) -> None:
        """Single-key answers, which is how anyone actually uses this.

        Handled before the bindings above because a bare letter is not a
        binding, and after them for the keys that are — Textual dispatches
        bindings first, so `escape` never reaches here.
        """
        pressed = event.key
        for key, answer, _label in APPROVAL_CHOICES:
            if not key:
                # `never` has no quick key. Skipping the blank rather than
                # comparing against it matters: `event.character` is None for
                # a bare arrow press, and `None == ""` is false only by luck.
                continue
            # Textual names `!` as `exclamation_mark`; `event.character` is the
            # literal, which is what the hint on screen tells the user to press.
            if pressed == key or event.character == key:
                event.stop()
                self.dismiss(answer)
                return
        if pressed == "q":
            event.stop()
            self.dismiss("no")


class ClarifyScreen(ModalScreen[list]):
    """`clarify`: the agent asking a question, one at a time.

    Sequential rather than a form. A terminal has no form, and inventing a
    multi-field editor here would be a worse version of what the desktop app
    already does well — the same call the REPL makes.
    """

    BINDINGS = [
        Binding("escape", "abandon", "skip", show=False),
        Binding("ctrl+c", "abandon", "skip", show=False),
    ]

    def __init__(self, body: dict) -> None:
        super().__init__()
        self.questions: list[dict] = list(body.get("questions") or [])
        self.answers: list[str] = []
        self.index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="clarify-box"):
            yield Static(self._question(), id="clarify-question")
            yield Static(self._choices(), id="clarify-choices")
            yield Input(placeholder="type an answer, or pick a number", id="clarify-input")

    def on_mount(self) -> None:
        self.query_one("#clarify-input", Input).focus()

    @property
    def current(self) -> dict:
        return self.questions[self.index] if self.index < len(self.questions) else {}

    def _question(self) -> Text:
        line = Text()
        if len(self.questions) > 1:
            line.append(
                f"{self.index + 1}/{len(self.questions)}  ",
                style=f"dim {render.ZINC_200}",
            )
        line.append(
            render.eyebrow("question") + "  /  ",
            style=f"bold {render.ZINC_200}",
        )
        line.append(str(self.current.get("text", "")), style=render.ZINC_50)
        return line

    def _choices(self) -> Text:
        choices = self.current.get("choices") or []
        line = Text()
        for position, choice in enumerate(choices, start=1):
            line.append(f"  {position}", style=render.ZINC_50)
            line.append(f"  {choice}", style=render.ZINC_100)
            # First is the recommendation, and an empty answer takes it — which
            # is why the tool's schema insists the recommended option goes
            # first rather than being labelled.
            line.append(
                " (recommended)\n" if position == 1 else "\n",
                style=f"dim {render.ZINC_200}",
            )
        return line

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._record(event.value.strip())
        event.input.value = ""

    def _record(self, raw: str) -> None:
        choices = self.current.get("choices") or []
        if choices and not raw:
            answer = choices[0]
        elif choices and raw.isdigit() and 1 <= int(raw) <= len(choices):
            answer = choices[int(raw) - 1]
        else:
            # Anything else is the "Other" case: a typed answer is an answer.
            answer = raw
        self.answers.append(answer)
        self.index += 1
        if self.index >= len(self.questions):
            self.dismiss(self.answers)
            return
        self.query_one("#clarify-question", Static).update(self._question())
        self.query_one("#clarify-choices", Static).update(self._choices())

    def action_abandon(self) -> None:
        """Dismissing is silence, not a default.

        The remaining questions come back empty and the tool reports them as
        "(no answer)". A default here would be exactly the guess `clarify`
        exists to replace.
        """
        self.dismiss(self.answers + [""] * (len(self.questions) - len(self.answers)))
