"""What a tool is, and how dangerous it is.

The vocabulary is deliberately the one the TypeScript runtime already uses —
tiers `safe_local | outbound | destructive | irreversible`, decisions
`allowed | needs_approval | denied`. Two harnesses that grade the same action
differently is how a user learns that "approved" means nothing.

The local tools below have no counterpart in that registry: the hosted runtime
never had access to a user's own filesystem or shell. This is new surface, so
its tiers are set here rather than mirrored, and they are set pessimistically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

RiskTier = Literal["safe_local", "outbound", "destructive", "irreversible"]

# The TypeScript registry's `category`, carried over because the specialist
# belts are written in terms of it: "read-only" there means category `read`
# *and* tier `safe_local`, and neither half alone is the same predicate.
# `memory_store` is the case that proves it — tier `safe_local`, category
# `write` — which is exactly why a Verifier cannot call it.
Category = Literal["read", "write", "admin"]

# Ordered least to most dangerous. Comparisons use the index, so a policy can
# say "nothing above `outbound`" without enumerating tiers.
TIER_ORDER: tuple[RiskTier, ...] = (
    "safe_local",
    "outbound",
    "destructive",
    "irreversible",
)


def tier_rank(tier: RiskTier) -> int:
    return TIER_ORDER.index(tier)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk_tier: RiskTier
    category: Category
    run: Callable[..., "ToolResult"]
    # Shown in the approval prompt instead of raw JSON. A person cannot consent
    # to something they have to parse.
    summarize: Callable[[dict[str, Any]], str] | None = None

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def summary(self, arguments: dict[str, Any]) -> str:
        if self.summarize is not None:
            try:
                return self.summarize(arguments)
            except Exception:  # noqa: BLE001 - a bad summary must not block the gate
                pass
        return self.name


@dataclass
class ToolResult:
    """What the model sees back.

    `content` is what goes into the transcript. `display` is what the terminal
    shows, which is usually shorter — a 4000-line file is a legitimate tool
    result and an illegitimate thing to print.
    """

    content: str
    display: str = ""
    ok: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.display:
            self.display = self.content


def failure(message: str) -> ToolResult:
    """An error the model should read and recover from, not a crash.

    Returned as a normal tool result on purpose: a tool that raises ends the
    turn, while a tool that reports "no such file" lets the model try the right
    path. Only a bug in the harness itself should propagate.
    """
    return ToolResult(content=f"Error: {message}", ok=False)
