"""Specialist belts for delegated work.

Ported from `lib/agent-runtime/subagents/specialists.ts`, including the
predicates the belts are written in terms of. Four of the five port; `operator`
(connected accounts) has no equivalent surface here and is absent rather than
stubbed — a belt that admits nothing useful is worse than no belt, because it
looks like a capability.

`builder` is the one belt that is not a port. Every other specialist reads, so
for a long time delegation could not be used for the work people actually
delegate: making the change. It writes files and nothing else — no shell, no
network — and with `worktree_isolation` on it does that in a copy of the tree
of its own.

The load-bearing property, carried over verbatim: **a belt is a hard denial,
read before anything else.** A child runs in `auto` mode, so a tool that a belt
rejects must come back `denied` and not `needs_approval` — otherwise a Writer
reaching a send tool would not be a Writer that cannot send, it would be a
Writer that sends after a pause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from andromeda_tools import ToolSpec

# Names that reach off this machine even though they change nothing. Tiered
# `safe_local` because that is the right answer for approval, and the wrong
# answer for a helper defined as having no network path at all.
NETWORK_READS = frozenset({"web_fetch", "web_search"})

# The delegation family. A child never holds these: `canSpawn` is false for
# every specialist, so depth stops at one.
SESSION_TOOLS = frozenset(
    {"delegate", "subagents_list", "subagents_status", "subagents_wait"}
)

BROWSER_PREFIX = "browser_"


def is_browser_tool(tool: ToolSpec) -> bool:
    """The browser is one surface, with one specialist.

    Two lanes driving the same browser is worse than two lanes in one mailbox,
    because neither can see that it is happening. Every other belt refuses the
    family outright rather than being merely polite about it.
    """
    return tool.name == "browser" or tool.name.startswith(BROWSER_PREFIX)


def is_read_only(tool: ToolSpec) -> bool:
    """Reads the world without changing it.

    Both halves are required. `memory_store` is tier `safe_local` and category
    `write`; a predicate that checked only the tier would hand every read-only
    specialist the ability to write durable facts.
    """
    return tool.category == "read" and tool.risk_tier == "safe_local"


def is_egress(tool: ToolSpec) -> bool:
    """Anything that leaves this machine.

    Tier first, because every tool declares one and anything unclassified
    defaults to the safe direction. The named family on top catches the web
    reads, which are `safe_local` precisely because they change nothing.
    """
    if tool.risk_tier != "safe_local":
        return True
    if is_browser_tool(tool):
        return True
    return tool.name in NETWORK_READS


def is_session_tool(tool: ToolSpec) -> bool:
    return tool.name in SESSION_TOOLS


@dataclass(frozen=True)
class Specialist:
    id: str
    label: str
    purpose: str
    max_turns: int
    admits: Callable[[ToolSpec], bool]
    # Every specialist here is false. Kept as a field rather than assumed, so
    # that raising depth later is a visible edit and not an emergent one.
    can_spawn: bool = False
    # Whether this belt changes the working tree. Two such lanes must not run
    # at once in the same directory, so unless each has a worktree of its own
    # they take the tree surface in turn — the same rule the browser has, for
    # the same reason.
    writes_tree: bool = False

    def brief_line(self) -> str:
        return f"{self.id} — {self.purpose}"


def _scout_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    # The browser is a single-occupancy surface with its own specialist.
    # Letting the read-only helper drive it makes that exclusivity meaningless.
    if is_browser_tool(tool):
        return False
    return is_read_only(tool) or tool.name in NETWORK_READS


def _browser_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    return is_browser_tool(tool) or is_read_only(tool) or tool.name == "web_fetch"


def _writer_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    # The whole point of this specialist. A Writer that can send is a Writer
    # that will eventually send a draft.
    if is_egress(tool):
        return False
    return is_read_only(tool)


# What a Builder may change. Named, not derived from a tier: `terminal` is the
# one that matters and it is deliberately absent — a shell can `cd` anywhere,
# so a lane holding one is a lane whose isolation is a suggestion rather than a
# boundary. Everything here writes through the workspace, which is checked.
FILE_WRITES = frozenset({"write_file", "patch"})


def _builder_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    if is_browser_tool(tool):
        return False
    # The web reads are `safe_local` and category `read`, so they pass the
    # read-only predicate — the same trap the Writer's egress check exists to
    # close. Named here, because this belt cannot use `is_egress` at all: that
    # predicate is tier-based, and the writes this belt exists for are
    # `destructive`, so filtering on it would deny the whole point.
    if tool.name in NETWORK_READS:
        return False
    # A closed list rather than a filter. Everything admitted is named, so a
    # tool added later is denied until somebody decides otherwise — which is
    # what keeps a send tool out, by omission rather than by predicate.
    return is_read_only(tool) or tool.name in FILE_WRITES


def _verifier_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    if is_browser_tool(tool):
        return False
    # Read-only, and that includes not writing memory: a checker that can store
    # facts is a checker whose opinion outlives the run it was hired to judge.
    return is_read_only(tool) and not tool.name.startswith("memory_store")


BUILTIN_SPECIALISTS: dict[str, Specialist] = {
    "scout": Specialist(
        id="scout",
        label="Scout",
        purpose="Finds things out. Reads, searches and reports; changes nothing, anywhere.",
        # 12, matching the hosted lane. A repair pass costs two turns a line —
        # do the thing, then go and look — plus one to orient and one to report.
        max_turns=12,
        admits=_scout_admits,
    ),
    "writer": Specialist(
        id="writer",
        label="Writer",
        purpose=(
            "Drafts and shapes text from what it was given. Cannot reach "
            "anything outside this machine."
        ),
        # The smallest budget, deliberately: it has nothing to fetch and
        # nowhere to go, so a larger one buys revisions of a draft nobody has
        # read yet.
        max_turns=10,
        admits=_writer_admits,
    ),
    "browser": Specialist(
        id="browser",
        label="Browser",
        purpose="Works a website. One at a time, because there is one browser.",
        # Browser work is many small steps — navigate, snapshot, click, snapshot
        # again — and a step here buys far less than a step anywhere else. This
        # is the one belt whose budget is genuinely larger than the rest.
        max_turns=20,
        admits=_browser_admits,
    ),
    "builder": Specialist(
        id="builder",
        label="Builder",
        purpose=(
            "Makes a change. Reads, edits and writes files — in its own copy "
            "of the tree, so other lanes are not editing underneath it."
        ),
        # Larger than the readers, because writing is a cycle: read the file,
        # change it, read it back. A budget that runs out mid-cycle leaves a
        # half-applied edit, which is the one outcome worse than no edit.
        max_turns=16,
        admits=_builder_admits,
        writes_tree=True,
    ),
    "verifier": Specialist(
        id="verifier",
        label="Checker",
        purpose="Re-reads the world and reports what it found. Changes nothing.",
        # This lane's whole job is the second look, so a budget that runs out
        # before the last item is checked converts a settleable question into
        # an unsettled one by construction.
        max_turns=12,
        admits=_verifier_admits,
    ),
}

def specialist_ids() -> tuple[str, ...]:
    """Every belt id, built-in and plugin.

    A function rather than the constant it used to be: a plugin's belt arrives
    after this module is imported, and a tuple frozen at import time would list
    the built-ins forever.
    """
    return tuple(all_specialists())


def resolve(specialist_id: str) -> Specialist | None:
    return all_specialists().get((specialist_id or "").strip().lower())


def all_specialists() -> dict[str, Specialist]:
    """Every belt, built-in first.

    A plugin cannot shadow a built-in id. The belts are how a delegated child's
    permissions are decided, so replacing `scout` would mean quietly widening
    what every read-only lane in the install is allowed to touch.
    """
    combined = dict(BUILTIN_SPECIALISTS)
    try:
        from . import plugins as plugins_module

        for identifier, specialist in plugins_module.specialists().items():
            if identifier not in combined:
                combined[identifier] = specialist
    except Exception:  # noqa: BLE001 - delegation must not depend on plugins
        pass
    return combined


class _SpecialistsProxy(dict):
    """`SPECIALISTS` kept as a name, resolving through `all_specialists()`."""

    def __getitem__(self, key):
        return all_specialists()[key]

    def get(self, key, default=None):
        return all_specialists().get(key, default)

    def __iter__(self):
        return iter(all_specialists())

    def __len__(self):
        return len(all_specialists())

    def __contains__(self, key):
        return key in all_specialists()

    def keys(self):
        return all_specialists().keys()

    def values(self):
        return all_specialists().values()

    def items(self):
        return all_specialists().items()

    def __repr__(self):
        return repr(all_specialists())


SPECIALISTS = _SpecialistsProxy()
