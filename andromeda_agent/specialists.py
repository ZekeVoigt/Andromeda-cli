"""Specialist belts for delegated work.

Ported from `lib/agent-runtime/subagents/specialists.ts`, including the
predicates the belts are written in terms of. Four of the five port; `operator`
(connected accounts) has no equivalent surface here and is absent rather than
stubbed — a belt that admits nothing useful is worse than no belt, because it
looks like a capability.

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


def _verifier_admits(tool: ToolSpec) -> bool:
    if is_session_tool(tool):
        return False
    if is_browser_tool(tool):
        return False
    # Read-only, and that includes not writing memory: a checker that can store
    # facts is a checker whose opinion outlives the run it was hired to judge.
    return is_read_only(tool) and not tool.name.startswith("memory_store")


SPECIALISTS: dict[str, Specialist] = {
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

SPECIALIST_IDS = tuple(SPECIALISTS)


def resolve(specialist_id: str) -> Specialist | None:
    return SPECIALISTS.get((specialist_id or "").strip().lower())
