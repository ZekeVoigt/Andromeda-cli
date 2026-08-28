"""Automations as a form, not as cron syntax.

A blueprint is one definition of an automation that every surface can render
natively: a terminal prompts for the slots, a form draws a field per slot, and
the agent asks about whatever the person left blank.

The design choice worth keeping: **nobody types raw cron.** A blueprint carries
a fixed recurrence in `schedule_template` and parameterises only the parts a
person actually has an opinion about — a time of day, a weekday set, an
interval. Blueprints that genuinely need full flexibility expose a `schedule`
slot that passes through verbatim.

`fill_blueprint` returns the exact kwargs `Schedule.add` takes, so there is no
second job schema to keep in sync — the same rule as suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SLOT_TYPES = frozenset({"time", "enum", "text", "weekdays"})

WEEKDAY_PRESETS: dict[str, str] = {
    "everyday": "*",
    "weekdays": "1-5",
    "weekends": "0,6",
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class BlueprintError(ValueError):
    pass


@dataclass(frozen=True)
class Slot:
    name: str
    type: str
    label: str
    default: Any = None
    options: tuple = ()
    optional: bool = False
    help: str = ""
    # When False the options are suggestions rather than a closed set. Used
    # where the legal values depend on what the machine has configured, and
    # validating them here would refuse something that will work.
    strict: bool = True

    def __post_init__(self) -> None:
        if self.type not in SLOT_TYPES:
            raise BlueprintError(f"unknown slot type {self.type!r} (slot {self.name})")


@dataclass(frozen=True)
class Blueprint:
    key: str
    title: str
    description: str
    category: str
    # A schedule with `{slot}` placeholders. A literal expression with none is
    # a fixed recurrence.
    schedule_template: str
    prompt_template: str
    slots: list[Slot] = field(default_factory=list)
    deliver_default: str = "none"
    approval_default: str = "ask"
    tags: tuple = ()


_TIME_SLOT = Slot(
    name="time",
    type="time",
    label="What time?",
    default="08:00",
    help="24-hour local time, e.g. 08:00",
)
_DELIVER_SLOT = Slot(
    name="deliver",
    type="enum",
    label="How should it tell you?",
    default="none",
    options=("none", "notify", "stdout", "webhook"),
    # Not strict: `webhook` needs a URL configured, and the set of things this
    # machine can actually deliver to is decided at run time.
    strict=False,
    help="none = saved only; notify = a desktop notification",
)


# ---------------------------------------------------------------------------
# The curated set
#
# Deliberately about *this* machine — a checkout, a filesystem, a shell.
# Briefings and inbox triage would need a messaging gateway and connected
# accounts; offering them here would be offering a form that creates a job that
# cannot work.
# ---------------------------------------------------------------------------

CATALOG: list[Blueprint] = [
    Blueprint(
        key="repo-digest",
        title="Repository digest",
        description="A recap of what changed in a repository, on a schedule.",
        category="daily",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Summarise recent activity in this repository: commits since the "
            "last digest, who made them, and anything that looks like it needs "
            "attention (failing builds mentioned in commit messages, reverts, "
            "large or unusual diffs). Use the shell — `git log`, `git diff "
            "--stat`. Keep it short and scannable. If nothing has changed since "
            "the last run, reply with [SILENT] and nothing else."
        ),
        slots=[
            _TIME_SLOT,
            Slot(
                name="recurrence",
                type="weekdays",
                label="Which days?",
                default="weekdays",
                options=tuple(WEEKDAY_PRESETS),
            ),
            _DELIVER_SLOT,
        ],
        tags=("git", "digest"),
    ),
    Blueprint(
        key="watch-url",
        title="Watch a page",
        description=(
            "Fetch a URL on a schedule and only wake the agent when it changes."
        ),
        category="monitor",
        schedule_template="*/{interval_min} * * * *",
        prompt_template=(
            "The watched page has changed. Say plainly what is different and "
            "whether it matters, in no more than three sentences. Do not "
            "restate the whole page."
        ),
        slots=[
            Slot(
                name="url",
                type="text",
                label="Which URL?",
                help="Fetched each tick; the agent runs only when it changes.",
            ),
            Slot(
                name="interval_min",
                type="enum",
                label="How often?",
                default="30",
                options=("5", "15", "30", "60"),
                help="minutes between checks",
            ),
            _DELIVER_SLOT,
        ],
        tags=("monitor",),
    ),
    Blueprint(
        key="watch-command",
        title="Watch a command",
        description=(
            "Run a script on a schedule and only wake the agent when its "
            "output changes."
        ),
        category="monitor",
        schedule_template="*/{interval_min} * * * *",
        prompt_template=(
            "The watched command's output has changed. Say what changed and "
            "whether it needs action, briefly. If the change is routine noise, "
            "reply with [SILENT] and nothing else."
        ),
        slots=[
            Slot(
                name="script",
                type="text",
                label="Which script?",
                help="A file in ~/.andromeda-cli/scripts/ — named, not pathed.",
            ),
            Slot(
                name="interval_min",
                type="enum",
                label="How often?",
                default="15",
                options=("5", "15", "30", "60"),
            ),
            _DELIVER_SLOT,
        ],
        tags=("monitor", "shell"),
    ),
    Blueprint(
        key="watchdog",
        title="Silent watchdog",
        description=(
            "Run a check script on a schedule with no model at all. It only "
            "speaks when the script prints something."
        ),
        category="monitor",
        schedule_template="*/{interval_min} * * * *",
        prompt_template="",
        slots=[
            Slot(
                name="script",
                type="text",
                label="Which check script?",
                help="Print nothing when everything is fine.",
            ),
            Slot(
                name="interval_min",
                type="enum",
                label="How often?",
                default="10",
                options=("5", "10", "30", "60"),
            ),
            Slot(
                name="deliver",
                type="enum",
                label="How should it tell you?",
                default="notify",
                options=("notify", "none", "stdout", "webhook"),
                strict=False,
            ),
        ],
        tags=("monitor", "cheap"),
    ),
    Blueprint(
        key="follow-up",
        title="Follow up later",
        description="Check on one thing, once, at a time you choose.",
        category="once",
        schedule_template="{schedule}",
        prompt_template="{task}",
        slots=[
            Slot(name="task", type="text", label="Check what?"),
            Slot(
                name="schedule",
                type="text",
                label="When?",
                default="in 2h",
                help="'in 2h', or 'at 2026-09-01T09:00'",
            ),
            _DELIVER_SLOT,
        ],
        tags=("once",),
    ),
]

def all_blueprints() -> list["Blueprint"]:
    """The catalogue, built-in first, plugins appended.

    Ungated on the plugin side: a blueprint is a *form*. Filling it in still
    goes through `Schedule.add`, which refuses `approval_mode="auto"` from an
    agent — so a plugin cannot use one to create a job more permissive than the
    person creating it asked for.
    """
    combined = list(CATALOG)
    known = {blueprint.key for blueprint in CATALOG}
    try:
        from . import plugins as plugins_module

        for blueprint in plugins_module.blueprints():
            key = getattr(blueprint, "key", "")
            if key and key not in known:
                combined.append(blueprint)
                known.add(key)
    except Exception:  # noqa: BLE001 - the catalogue must not depend on plugins
        pass
    return combined


BY_KEY = {blueprint.key: blueprint for blueprint in CATALOG}


def get(key: str) -> Blueprint | None:
    """One blueprint by key, built-in or plugin.

    Normalisation is the caller's usual mistake, not theirs: `cron blueprint
    use Morning-Brief` is what people type.
    """
    wanted = (key or "").strip().lower()
    for blueprint in all_blueprints():
        if blueprint.key == wanted:
            return blueprint
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def form_schema(blueprint: Blueprint) -> dict[str, Any]:
    """What a form renderer needs. One field per slot, in order."""
    return {
        "key": blueprint.key,
        "title": blueprint.title,
        "description": blueprint.description,
        "fields": [
            {
                "name": slot.name,
                "type": slot.type,
                "label": slot.label,
                "default": slot.default,
                "options": list(slot.options),
                "optional": slot.optional,
                "help": slot.help,
                "strict": slot.strict,
            }
            for slot in blueprint.slots
        ],
    }


def command_for(blueprint: Blueprint, values: dict[str, Any] | None = None) -> str:
    """The flattened one-liner, pre-filled. What the terminal shows."""
    values = values or {}
    parts = [f"andromeda cron blueprint use {blueprint.key}"]
    for slot in blueprint.slots:
        value = values.get(slot.name, slot.default)
        if value in (None, ""):
            continue
        parts.append(f"--{slot.name} {value!r}" if " " in str(value) else f"--{slot.name} {value}")
    return " ".join(parts)


def _resolve_schedule(blueprint: Blueprint, values: dict[str, Any]) -> str:
    template = blueprint.schedule_template

    # A free-text `schedule` slot passes through verbatim — the escape hatch
    # for the cases a fixed recurrence cannot express.
    if values.get("schedule"):
        return str(values["schedule"])

    filled: dict[str, str] = {}

    if "{minute}" in template or "{hour}" in template:
        raw = str(values.get("time") or "").strip()
        match = _TIME_RE.match(raw)
        if not match:
            raise BlueprintError(f"invalid time {raw!r} — use HH:MM, 24-hour.")
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise BlueprintError(f"invalid time {raw!r} — use HH:MM, 24-hour.")
        filled["hour"] = str(hour)
        filled["minute"] = str(minute)

    if "{dow}" in template:
        preset = str(values.get("recurrence") or "everyday").lower()
        if preset not in WEEKDAY_PRESETS:
            raise BlueprintError(
                f"unknown recurrence {preset!r} — one of {', '.join(WEEKDAY_PRESETS)}."
            )
        filled["dow"] = WEEKDAY_PRESETS[preset]

    if "{interval_min}" in template:
        raw = str(values.get("interval_min") or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            raise BlueprintError(f"invalid interval {raw!r} — whole minutes.")
        filled["interval_min"] = raw

    for name in _PLACEHOLDER.findall(template):
        if name not in filled and name in values:
            filled[name] = str(values[name])

    try:
        return template.format(**filled)
    except KeyError as exc:
        raise BlueprintError(f"the schedule needs a value for {exc}.") from exc


def fill(blueprint: Blueprint, values: dict[str, Any]) -> dict[str, Any]:
    """Validate, and return the kwargs `Schedule.add` takes.

    Unknown slot names are **rejected**, not ignored. A typo'd `--tiem 07:15`
    that silently creates a job at the default time is the worst possible
    outcome: it works, it is wrong, and nothing says so.
    """
    known = {slot.name for slot in blueprint.slots}
    unknown = sorted(set(values) - known)
    if unknown:
        raise BlueprintError(
            f"unknown option{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)} — "
            f"this blueprint takes {', '.join(sorted(known))}."
        )

    resolved: dict[str, Any] = {}
    for slot in blueprint.slots:
        raw = values.get(slot.name, slot.default)
        if raw in (None, ""):
            if slot.optional:
                continue
            raise BlueprintError(f"{slot.name} is required ({slot.label})")
        if (
            slot.type == "enum"
            and slot.strict
            and slot.options
            and str(raw) not in {str(option) for option in slot.options}
        ):
            raise BlueprintError(
                f"{slot.name}={raw!r} is not one of {', '.join(map(str, slot.options))}."
            )
        resolved[slot.name] = raw

    schedule = _resolve_schedule(blueprint, resolved)
    try:
        prompt = blueprint.prompt_template.format(**resolved)
    except KeyError as exc:
        raise BlueprintError(f"the prompt needs a value for {exc}.") from exc

    spec: dict[str, Any] = {
        "schedule": schedule,
        "prompt": prompt,
        "name": blueprint.title,
        "deliver": str(resolved.get("deliver", blueprint.deliver_default)),
        "approval_mode": blueprint.approval_default,
    }

    # The monitor and script blueprints are the reason the slot names are what
    # they are: a blueprint's job is to produce a spec, not a second vocabulary.
    if blueprint.key == "watch-url":
        spec["monitor_kind"] = "url"
        spec["monitor_source"] = str(resolved["url"])
    elif blueprint.key == "watch-command":
        spec["monitor_kind"] = "script"
        spec["monitor_source"] = str(resolved["script"])
    elif blueprint.key == "watchdog":
        spec["script"] = str(resolved["script"])
        spec["no_agent"] = True

    return spec
