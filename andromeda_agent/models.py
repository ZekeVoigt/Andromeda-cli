"""Which models this build will serve.

Mirrors `lib/inference-relay/policy.ts`. The relay holds the provider key and
so its allowlist is enforced server-side no matter what a client asks for; the
BYOK lane has no such backstop — the key is the user's and the request goes
straight to OpenRouter — so the same list has to be enforced here, at both the
place a model is written and the place one is used.

Kept deliberately small and explicit rather than derived from a catalogue: at
launch the answer is one model, and a list that can only grow by someone typing
an id into it is the point.
"""

from __future__ import annotations

# The dated snapshot, not the rolling `deepseek/deepseek-v4-flash`. The rolling
# id's row in the generated price table reads 0.0882/0.1764 per M against a live
# feed of 0.14/0.28, so anything costing against it undercharges by ~59%.
ALLOWED_MODEL_IDS: tuple[str, ...] = ("deepseek/deepseek-v4-flash-0731",)

# The model used for side tasks the conversation model cannot do itself.
#
# The lock above governs the model that *reasons* — the one whose output the
# user reads and whose cost dominates. It is text-only, so a vision tool bolted
# onto it would be inert: the API would reject the image part outright.
#
# The answer is an auxiliary client: one narrow model, reachable only by the
# tools that need it, never selectable as the conversation model. That keeps the lock meaningful
# while making the capability real.
AUXILIARY_MODELS: dict[str, str] = {
    "vision": "deepseek/deepseek-v4-flash-vision-exp",
}


def auxiliary_model(purpose: str) -> str | None:
    return AUXILIARY_MODELS.get(purpose)


# Models that accept a reasoning budget. Sending `reasoning` to one that does
# not is not harmless: some providers reject the whole request rather than
# ignoring the field.
REASONING_MODELS: frozenset[str] = frozenset(
    {
        "deepseek/deepseek-v4-flash-0731",
        "deepseek/deepseek-v4-flash-vision-exp",
    }
)

# OpenRouter's vocabulary. `off` is ours: the absence of the field, which is
# not the same as asking for minimal effort.
THINKING_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")


def supports_reasoning(model_id: object) -> bool:
    return isinstance(model_id, str) and model_id.strip().lower() in REASONING_MODELS


def reasoning_for(model_id: object, level: str) -> dict | None:
    """The `reasoning` field for a request, or nothing.

    Returns None for `off` and for any model that cannot reason, so the caller
    can spread it without a conditional and never sends a field the provider
    would refuse.
    """
    level = (level or "off").strip().lower()
    if level == "off" or level not in THINKING_LEVELS:
        return None
    if not supports_reasoning(model_id):
        return None
    return {"effort": level}

# Context windows, from OpenRouter's own model listing rather than from memory.
# Getting this wrong is not harmless in either direction: too low and compaction
# fires early, spends a summarisation call and discards context the model could
# have held; too high and the provider refuses a request compaction should have
# prevented.
CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek/deepseek-v4-flash-0731": 1_310_720,
    "deepseek/deepseek-v4-flash-vision-exp": 1_048_576,
}

# Used when a model is not in the table. Deliberately conservative: compacting
# earlier than necessary costs a call, while overrunning the window costs the
# turn.
DEFAULT_CONTEXT_WINDOW = 128_000


def context_window(model_id: object) -> int:
    if isinstance(model_id, str):
        return CONTEXT_WINDOWS.get(model_id.strip().lower(), DEFAULT_CONTEXT_WINDOW)
    return DEFAULT_CONTEXT_WINDOW


def is_allowed(model_id: object) -> bool:
    return (
        isinstance(model_id, str)
        and model_id.strip().lower() in ALLOWED_MODEL_IDS
    )


def refusal(model_id: object) -> str:
    served = ", ".join(ALLOWED_MODEL_IDS)
    return f"This build serves {served}. Got {str(model_id).strip()!r}."
