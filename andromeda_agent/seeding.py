"""Where suggestions come from.

Four sources, all of them ending in `Suggestions.propose` and none of them ever
creating a job. The set is deliberately about what this machine actually has
— a checkout, a filesystem, a shell — rather than messaging or connected
accounts.

Seeding runs on a schedule of its own — once per interactive session start, at
most — and it is cheap: proposing is a dict comparison against the dedup keys
already on disk, and a key that has been decided on is skipped before anything
else happens.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import blueprints as blueprints_module
from .suggestions import Suggestions

# The curated starters. Each one is a blueprint plus the values that make it
# useful without asking anything — the point of a catalog entry is that it can
# be accepted with one word.
CATALOG: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "repo-digest",
        {"time": "08:30", "recurrence": "weekdays", "deliver": "notify"},
    ),
    (
        "watchdog",
        {"script": "disk-check.sh", "interval_min": "30", "deliver": "notify"},
    ),
)


def seed_catalog(store: Suggestions) -> list:
    """Offer the curated starters, once each, ever."""
    made = []
    for key, values in CATALOG:
        blueprint = blueprints_module.get(key)
        if blueprint is None:
            continue
        try:
            spec = blueprints_module.fill(blueprint, values)
        except blueprints_module.BlueprintError:
            # A catalog entry that no longer fills its own blueprint is a bug
            # in this file, not something to inflict on the user's list.
            continue
        suggestion = store.propose(
            title=blueprint.title,
            description=blueprint.description,
            source="catalog",
            spec=spec,
            dedup_key=f"catalog:{key}",
        )
        if suggestion is not None:
            made.append(suggestion)
    return made


# ---------------------------------------------------------------------------
# Skills that carry a blueprint
# ---------------------------------------------------------------------------
#
# A skill can ship an automation the same way it ships instructions, by putting
# a `blueprint:` block in its frontmatter:
#
#     blueprint:
#       schedule: "0 9 * * 1-5"
#       prompt: "Run the morning triage described in this skill."
#       name: "Morning triage"
#       deliver: notify
#
# Installing the skill registers a *suggestion*, never a job. That is the whole
# reason this goes through the same store: a skill somebody installed should
# not be able to schedule work on their machine without being asked.

BLUEPRINT_KEYS = ("schedule", "prompt", "name", "deliver", "script", "no_agent")


def blueprint_from_skill(skill) -> dict[str, Any] | None:
    """The `blueprint:` block of a skill, if it has a usable one."""
    raw = getattr(skill, "metadata", None) or {}
    block = raw.get("blueprint") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return None
    spec = {key: block[key] for key in BLUEPRINT_KEYS if key in block}
    if not spec.get("schedule"):
        return None
    if not (spec.get("prompt") or spec.get("script")):
        return None
    return spec


def seed_skill_blueprints(store: Suggestions, skills: dict) -> list:
    made = []
    for skill in (skills or {}).values():
        spec = blueprint_from_skill(skill)
        if spec is None:
            continue
        suggestion = store.propose(
            title=str(spec.get("name") or f"{skill.name} automation"),
            description=(
                f"The {skill.name} skill ships this automation. It was not "
                "scheduled — installing a skill does not schedule work."
            ),
            source="blueprint",
            spec=spec,
            dedup_key=f"blueprint:{skill.name}",
        )
        if suggestion is not None:
            made.append(suggestion)
    return made


# ---------------------------------------------------------------------------
# Things you keep asking for by hand
# ---------------------------------------------------------------------------

# Deliberately narrow. A recurrence phrase plus a task, in the user's own words,
# repeated across sessions. Anything looser proposes automations for one-off
# questions, and a list full of those is a list nobody opens.
_RECURRING = re.compile(
    r"\b(every (?:morning|day|week|hour|monday|friday)|each (?:morning|day|week)|daily|weekly|hourly)\b",
    re.I,
)
MIN_REPEATS = 3


def seed_from_usage(store: Suggestions, prompts: list[str]) -> list:
    """Propose a job for a thing asked for repeatedly, by hand, on a cadence.

    Evidence, not inference: the same recurrence phrase in the same shape three
    times. The alternative — a model deciding what you probably want automated —
    proposes confidently and wrongly, and every wrong proposal costs the list
    some of its credibility.
    """
    seen: dict[str, list[str]] = {}
    for prompt in prompts:
        match = _RECURRING.search(prompt or "")
        if not match:
            continue
        phrase = match.group(1).lower()
        key = f"{phrase}:{' '.join((prompt or '').lower().split())[:60]}"
        seen.setdefault(key, []).append(prompt)

    made = []
    for key, examples in seen.items():
        if len(examples) < MIN_REPEATS:
            continue
        phrase = key.split(":", 1)[0]
        suggestion = store.propose(
            title=f"Automate: {examples[0][:48]}",
            description=(
                f"You have asked for this {len(examples)} times and it says "
                f"'{phrase}'. A scheduled job would do it without you asking."
            ),
            source="usage",
            spec={
                "schedule": _schedule_for(phrase),
                "prompt": examples[0],
                "name": examples[0][:48],
                "deliver": "notify",
            },
            dedup_key=f"usage:{key}",
        )
        if suggestion is not None:
            made.append(suggestion)
    return made


def _schedule_for(phrase: str) -> str:
    if "hour" in phrase:
        return "every 1h"
    if "week" in phrase or "monday" in phrase:
        return "0 9 * * 1"
    if "friday" in phrase:
        return "0 17 * * 5"
    return "0 9 * * *"


# ---------------------------------------------------------------------------
# Capabilities that became available
# ---------------------------------------------------------------------------

INTEGRATION_OFFERS: dict[str, dict[str, Any]] = {
    "browser": {
        "title": "Watch a page",
        "description": (
            "The browser tools are installed, so a job can watch a page and "
            "wake only when it changes."
        ),
        "spec_key": "watch-url",
    },
}


def seed_from_capabilities(store: Suggestions, available: set[str]) -> list:
    """Offer the obvious automation for something that just became possible."""
    made = []
    for capability, offer in INTEGRATION_OFFERS.items():
        if capability not in available:
            continue
        blueprint = blueprints_module.get(str(offer["spec_key"]))
        if blueprint is None:
            continue
        suggestion = store.propose(
            title=str(offer["title"]),
            description=str(offer["description"]),
            source="integration",
            # Stored unfilled on purpose: this one genuinely needs a URL from
            # the person, so accepting it should ask rather than invent one.
            spec={"blueprint": blueprint.key},
            dedup_key=f"integration:{capability}",
        )
        if suggestion is not None:
            made.append(suggestion)
    return made


def seed_all(
    store: Suggestions,
    skills: dict | None = None,
    prompts: list[str] | None = None,
    capabilities: set[str] | None = None,
) -> list:
    return [
        *seed_catalog(store),
        *seed_skill_blueprints(store, skills or {}),
        *seed_from_usage(store, prompts or []),
        *seed_from_capabilities(store, capabilities or set()),
    ]
