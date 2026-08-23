"""Guard against the two registries drifting apart.

The Python tools here and the TypeScript tools in `lib/agent-runtime/tools` are
separate implementations. Four names now exist in both, deliberately: a person
who taught one surface something expects the other to know it, and a tool that
takes different arguments depending on which harness you are in is a bug the
user experiences as the agent being unreliable.

For those four, the JSON schema is compared field by field. For every other
name, the guard is that the overlap does not grow by accident — a new shared
name has to be added to SHARED below, which is the moment to reconcile it.
"""

from __future__ import annotations

import os
import re

import pytest
import ts_registry

from andromeda_agent.delegation import make_delegate_tool, make_lane_tools
from andromeda_agent.lanes import LaneRegistry
from andromeda_tools import MemoryStore, Workspace, build_registry, skills
from andromeda_tools.todo import TodoList

# Names defined in both registries on purpose. Adding to this list means you
# have checked that the two schemas agree.
SHARED = (
    "skill_load",
    "memory_search",
    "memory_store",
    "memory_forget",
    "web_fetch",
    "web_search",
    # The lane-inspection family. Unlike `sessions_spawn`, these three
    # contracts are ones this harness can honour in full, so they take the
    # hosted names.
    "subagents_list",
    "subagents_status",
    "subagents_wait",
)


@pytest.fixture(scope="module")
def python_registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("registry")
    # A search provider key, so `web_search` is registered and can be compared.
    # It is one of the shared names; a fixture that omits it would let that pair
    # drift unchecked.
    previous = os.environ.get("BRAVE_SEARCH_API_KEY")
    os.environ["BRAVE_SEARCH_API_KEY"] = "fixture-key"
    try:
        return build_registry(
            Workspace(root),
            TodoList(),
            {},
            MemoryStore(root),
            delegate=make_delegate_tool(lambda **_: None),
            lane_tools=make_lane_tools(LaneRegistry()),
        )
    finally:
        if previous is None:
            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
        else:
            os.environ["BRAVE_SEARCH_API_KEY"] = previous


@pytest.fixture(autouse=True)
def _require_monorepo():
    if ts_registry.repo_root() is None:
        pytest.skip("running outside the monorepo checkout")


def test_the_typescript_registry_parses():
    """Asserted separately: a silent parse failure makes every guard vacuous."""
    assert len(ts_registry.tool_names()) > 20


@pytest.mark.parametrize("name", SHARED)
def test_each_shared_tool_still_exists_on_both_sides(name, python_registry):
    assert name in python_registry
    assert name in ts_registry.tool_names()


@pytest.mark.parametrize("name", SHARED)
def test_shared_schemas_agree(name, python_registry):
    theirs = ts_registry.parameters_for(name)
    assert theirs is not None, f"could not parse the TypeScript schema for {name}"

    ours = python_registry[name].parameters

    assert set(ours.get("properties", {})) == set(theirs.get("properties", {})), (
        f"{name} takes different parameters in the two registries"
    )
    assert set(ours.get("required", [])) == set(theirs.get("required", [])), (
        f"{name} requires different parameters in the two registries"
    )

    for key, theirs_property in theirs.get("properties", {}).items():
        ours_property = ours["properties"][key]
        assert ours_property.get("type") == theirs_property.get("type"), (
            f"{name}.{key} has a different type in the two registries"
        )
        if "enum" in theirs_property or "enum" in ours_property:
            assert ours_property.get("enum") == theirs_property.get("enum"), (
                f"{name}.{key} accepts different values in the two registries"
            )


@pytest.mark.parametrize("name", SHARED)
def test_shared_risk_tiers_agree(name, python_registry):
    """The gap that let memory_store ship as `destructive` here and
    `safe_local` there: a tool's tier lives in TOOL_RISK_TIERS, nowhere near
    its schema, so a schema-only comparison passes while the gate diverges."""
    theirs = ts_registry.resolved_tier(name)
    assert theirs is not None, f"could not resolve a tier for {name}"
    assert python_registry[name].risk_tier == theirs, (
        f"{name} is {python_registry[name].risk_tier} here and {theirs} in "
        "the TypeScript registry — the same action would be gated differently "
        "depending on which surface the user is on."
    )


@pytest.mark.parametrize("name", SHARED)
def test_shared_categories_agree(name, python_registry):
    """The belts are written in terms of category, not only tier.

    A tool that is `read` here and `write` there is admitted by different lanes
    on the two surfaces.
    """
    theirs = ts_registry.category_for(name)
    assert theirs is not None, f"could not read a category for {name}"
    assert python_registry[name].category == theirs, (
        f"{name} is category {python_registry[name].category} here and "
        f"{theirs} in the TypeScript registry"
    )


def test_the_typescript_tier_map_parses():
    assert len(ts_registry.risk_tiers()) > 10


def test_the_overlap_does_not_grow_by_accident(python_registry):
    overlap = set(python_registry) & ts_registry.tool_names()
    unexpected = overlap - set(SHARED)
    assert unexpected == set(), (
        f"These names are now defined in both registries: {sorted(unexpected)}.\n"
        "Reconcile their JSON schemas and risk tiers, then add them to SHARED."
    )


def test_every_shared_name_is_actually_shared(python_registry):
    """SHARED must not accumulate names that one side has since dropped."""
    for name in SHARED:
        assert name in python_registry and name in ts_registry.tool_names(), name


def test_python_tool_names_are_snake_case(python_registry):
    for name in python_registry:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name


def test_every_tool_declares_a_schema_and_a_tier(python_registry):
    for name, spec in python_registry.items():
        assert spec.description.strip(), f"{name} has no description"
        assert spec.parameters.get("type") == "object", f"{name} has no object schema"
        assert spec.risk_tier in {
            "safe_local",
            "outbound",
            "destructive",
            "irreversible",
        }, f"{name} has an unknown tier"


def test_required_parameters_are_declared_properties(python_registry):
    for name, spec in python_registry.items():
        properties = set(spec.parameters.get("properties", {}))
        required = set(spec.parameters.get("required", []))
        assert required <= properties, f"{name} requires undeclared {required - properties}"


def test_nothing_is_registered_but_off_by_default(python_registry):
    """The direction that can surprise someone.

    A name in DEFAULT_ENABLED that is not registered is harmless — enabling a
    tool that does not exist does nothing. A tool that IS registered and not in
    DEFAULT_ENABLED is silently unavailable on a fresh install, which reads as
    a broken agent rather than a configuration choice.
    """
    from andromeda_tools import DEFAULT_ENABLED

    assert set(python_registry) <= set(DEFAULT_ENABLED)


def test_delegate_is_deliberately_not_a_shared_name():
    """It cannot honour `sessions_spawn`'s contract, so it does not take the name.

    No background lanes, no run ids, no operator or browser specialists. A
    model that learned `background: true` on the hosted surface would find it
    silently ignored here.
    """
    assert "delegate" not in ts_registry.tool_names()
    assert "sessions_spawn" in ts_registry.tool_names()
