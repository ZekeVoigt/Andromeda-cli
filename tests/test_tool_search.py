"""Deferred tool disclosure: classification, search, listing, the bridge."""

from __future__ import annotations

import json

import pytest

from andromeda_agent import tool_search
from andromeda_tools import ToolResult, ToolSpec


def spec(name: str, description: str = "", **properties) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"does {name}",
        parameters={
            "type": "object",
            "properties": {key: {"type": "string"} for key in properties},
            "required": [key for key, needed in properties.items() if needed],
        },
        risk_tier="outbound",
        category="write",
        run=lambda **kwargs: ToolResult(content=json.dumps(kwargs)),
    )


def mcp(server: str, name: str, description: str = "", **properties) -> ToolSpec:
    return spec(f"mcp__{server}__{name}", description, **properties)


def recording(calls: list[dict], **properties) -> ToolSpec:
    """An MCP tool that remembers how it was called. `ToolSpec` is frozen, so
    the runner is supplied at construction rather than patched in."""
    from dataclasses import replace

    base = mcp("github", "create_issue", "Open an issue.", **properties)
    return replace(
        base,
        run=lambda **kwargs: (calls.append(kwargs), ToolResult(content="opened"))[1],
    )


CORE = [spec("read_file"), spec("terminal"), spec("write_file")]


# ---------------------------------------------------------------------------
# what defers
# ---------------------------------------------------------------------------


def test_built_in_tools_never_defer():
    """Always available has to mean always available."""
    for name in ("read_file", "terminal", "delegate", "skill_load"):
        assert tool_search.is_deferrable(name) is False


def test_mcp_tools_defer():
    assert tool_search.is_deferrable("mcp__github__create_issue") is True


def test_the_bridge_does_not_defer_itself():
    for name in tool_search.BRIDGE_NAMES:
        assert tool_search.is_deferrable(name) is False
        assert tool_search.is_bridge(name) is True


def test_the_server_name_is_read_from_the_tool_name():
    entry = tool_search.Entry(spec=mcp("github", "create_issue"))
    assert entry.server == "github"


def test_a_tool_with_no_server_segment_is_grouped_as_other():
    entry = tool_search.Entry(spec=spec("mcp__lonely"))
    assert entry.server == "other"


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def test_with_no_mcp_tools_nothing_changes():
    assembly = tool_search.assemble(CORE)
    assert assembly.activated is False
    assert assembly.tier == 0
    assert [item["function"]["name"] for item in assembly.schemas] == [
        "read_file",
        "terminal",
        "write_file",
    ]


def test_mcp_tools_are_replaced_by_the_bridge():
    specs = CORE + [mcp("github", "create_issue"), mcp("github", "list_repos")]
    assembly = tool_search.assemble(specs)

    names = [item["function"]["name"] for item in assembly.schemas]

    assert assembly.activated is True
    assert "mcp__github__create_issue" not in names
    assert set(tool_search.BRIDGE_NAMES) <= set(names)
    assert set(assembly.deferred) == {
        "mcp__github__create_issue",
        "mcp__github__list_repos",
    }


def test_the_built_in_tools_are_still_all_there():
    specs = CORE + [mcp("github", "create_issue")]
    names = [
        item["function"]["name"] for item in tool_search.assemble(specs).schemas
    ]
    for name in ("read_file", "terminal", "write_file"):
        assert name in names


def test_off_lists_everything():
    specs = CORE + [mcp("github", "create_issue")]
    assembly = tool_search.assemble(specs, mode="off")

    assert assembly.activated is False
    assert "mcp__github__create_issue" in [
        item["function"]["name"] for item in assembly.schemas
    ]


def test_a_big_catalogue_saves_more_than_the_bridge_costs():
    specs = CORE + [
        mcp("github", f"tool_{index}", "A tool that does a thing." * 8)
        for index in range(200)
    ]
    assembly = tool_search.assemble(specs, context_window=1_000_000)
    assert assembly.saved_tokens > 5_000


def test_the_assembly_is_rebuilt_rather_than_carried():
    """A catalogue kept across turns drifts out of step with the registry, and
    the failure is silent."""
    first = tool_search.assemble(CORE + [mcp("github", "a")])
    second = tool_search.assemble(CORE + [mcp("github", "a"), mcp("slack", "b")])
    assert set(first.deferred) != set(second.deferred)


# ---------------------------------------------------------------------------
# the listing
# ---------------------------------------------------------------------------


def test_a_small_catalogue_is_listed_in_full():
    specs = CORE + [mcp("github", "create_issue", "Open an issue on a repository.")]
    assembly = tool_search.assemble(specs, context_window=200_000)

    description = _bridge_description(assembly)

    assert assembly.form == "full"
    assert assembly.tier == 1
    assert "mcp__github__create_issue" in description
    assert "Open an issue on a repository" in description


def test_a_larger_catalogue_degrades_to_names():
    # One long sentence, so `short_description` has nothing to clip to and the
    # full form is genuinely expensive — a description with an early full stop
    # collapses to a few words and fits when you expect it not to.
    specs = CORE + [
        mcp("github", f"tool_{index}", "a description that runs on " * 10)
        for index in range(120)
    ]
    assembly = tool_search.assemble(specs, context_window=200_000, listing_max_tokens=1200)

    assert assembly.form == "names"
    assert "mcp__github__tool_1" in _bridge_description(assembly)
    assert "a description that runs on" not in _bridge_description(assembly)


def test_an_enormous_catalogue_degrades_to_a_count_per_server():
    specs = CORE + [
        mcp("cloudflare", f"a_very_long_tool_name_number_{index}") for index in range(4000)
    ]
    assembly = tool_search.assemble(specs, context_window=200_000)

    description = _bridge_description(assembly)

    assert assembly.form == "groups"
    assert assembly.tier == 2
    assert "cloudflare: 4000 tools" in description
    assert "a_very_long_tool_name_number_1" not in description


def test_the_listing_can_be_turned_off_entirely():
    specs = CORE + [mcp("github", "create_issue")]
    assembly = tool_search.assemble(specs, listing_max_tokens=0)
    assert assembly.form == "none"
    assert "mcp__github__create_issue" not in _bridge_description(assembly)


def test_the_listing_degrades_rather_than_truncating():
    """Half a catalogue looks like a whole one, and a model that reads it
    concludes the missing half does not exist."""
    specs = CORE + [mcp("github", f"tool_{index}") for index in range(60)]
    assembly = tool_search.assemble(specs, listing_max_tokens=300)

    description = _bridge_description(assembly)

    if assembly.form == "names":
        for index in range(60):
            assert f"tool_{index}" in description
    else:
        assert assembly.form == "groups"


def test_the_listing_groups_by_server():
    specs = CORE + [mcp("github", "a"), mcp("slack", "b")]
    description = _bridge_description(tool_search.assemble(specs))
    assert "github:" in description
    assert "slack:" in description


def _bridge_description(assembly: tool_search.Assembly) -> str:
    for item in assembly.schemas:
        if item["function"]["name"] == tool_search.SEARCH:
            return item["function"]["description"]
    return ""


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------


def catalog_of(*specs: ToolSpec) -> list[tool_search.Entry]:
    return tool_search.build_catalog(list(specs))


def test_a_query_finds_the_matching_tool():
    catalog = catalog_of(
        mcp("github", "create_issue", "Open an issue on a repository."),
        mcp("slack", "send_message", "Post a message to a channel."),
    )
    hits = tool_search.search(catalog, "post a message to slack")
    assert hits[0].name == "mcp__slack__send_message"


def test_the_tool_name_is_searchable_word_by_word():
    catalog = catalog_of(mcp("github", "create_issue"), mcp("github", "list_repos"))
    hits = tool_search.search(catalog, "create issue")
    assert hits[0].name == "mcp__github__create_issue"


def test_parameter_names_are_searchable():
    catalog = catalog_of(mcp("sheets", "read", "Read a range.", spreadsheet_id=True))
    assert tool_search.search(catalog, "spreadsheet")[0].name == "mcp__sheets__read"


def test_a_term_in_every_document_still_returns_results():
    """BM25 gives a term that appears everywhere an IDF of zero, so the one
    query a person would obviously type scores nothing without the fallback."""
    catalog = catalog_of(
        mcp("github", "create_issue"),
        mcp("github", "list_repos"),
        mcp("github", "open_pr"),
    )
    assert tool_search.search(catalog, "github")


def test_a_query_matching_nothing_returns_nothing():
    catalog = catalog_of(mcp("github", "create_issue"))
    assert tool_search.search(catalog, "photosynthesis") == []


def test_an_empty_query_returns_nothing():
    catalog = catalog_of(mcp("github", "create_issue"))
    assert tool_search.search(catalog, "   ") == []


def test_the_limit_is_honoured():
    catalog = catalog_of(*[mcp("github", f"issue_{index}") for index in range(10)])
    assert len(tool_search.search(catalog, "issue", limit=3)) == 3


def test_results_are_stable_for_equal_scores():
    catalog = catalog_of(*[mcp("github", f"thing_{index}") for index in range(5)])
    first = [entry.name for entry in tool_search.search(catalog, "thing", limit=5)]
    second = [entry.name for entry in tool_search.search(catalog, "thing", limit=5)]
    assert first == second


# ---------------------------------------------------------------------------
# the three bridge calls
# ---------------------------------------------------------------------------


def assembled():
    specs = CORE + [
        mcp("github", "create_issue", "Open an issue.", title=True, body=False),
        mcp("slack", "send_message", "Post a message."),
    ]
    return tool_search.assemble(specs, context_window=200_000)


def test_search_returns_matches():
    payload = json.loads(
        tool_search.dispatch_search(assembled(), {"query": "open an issue"})
    )
    assert payload["matches"][0]["name"] == "mcp__github__create_issue"
    assert payload["matches"][0]["server"] == "github"
    assert payload["available"] == 2


def test_search_needs_a_query():
    payload = json.loads(tool_search.dispatch_search(assembled(), {}))
    assert "error" in payload


def test_a_search_that_matches_nothing_says_what_is_connected():
    """A lexical miss is not evidence a capability is absent, and a model told
    only "no matches" concludes exactly that."""
    payload = json.loads(
        tool_search.dispatch_search(assembled(), {"query": "photosynthesis"})
    )
    assert payload["matches"] == []
    assert {item["server"] for item in payload["connected"]} == {"github", "slack"}
    assert "before concluding" in payload["hint"]


def test_the_search_limit_is_clamped():
    payload = json.loads(
        tool_search.dispatch_search(assembled(), {"query": "a", "limit": 10_000})
    )
    assert len(payload["matches"]) <= tool_search.MAX_SEARCH_LIMIT


def test_a_nonsense_limit_falls_back_to_the_default():
    payload = json.loads(
        tool_search.dispatch_search(assembled(), {"query": "issue", "limit": "lots"})
    )
    assert payload["matches"]


def test_describe_returns_the_parameters():
    payload = json.loads(
        tool_search.dispatch_describe(
            assembled(), {"name": "mcp__github__create_issue"}
        )
    )
    assert payload["name"] == "mcp__github__create_issue"
    assert "title" in payload["parameters"]["properties"]


def test_describing_an_unknown_tool_says_so():
    payload = json.loads(tool_search.dispatch_describe(assembled(), {"name": "nope"}))
    assert "error" in payload


def test_describing_a_listed_tool_points_at_calling_it_directly():
    payload = json.loads(
        tool_search.dispatch_describe(assembled(), {"name": "read_file"})
    )
    assert "call it directly" in payload["error"]


# ---------------------------------------------------------------------------
# resolving a call
# ---------------------------------------------------------------------------


def test_a_call_resolves_to_the_real_tool():
    resolved, arguments, error = tool_search.resolve_call(
        assembled(),
        {"name": "mcp__github__create_issue", "arguments": {"title": "hi"}},
    )
    assert resolved.name == "mcp__github__create_issue"
    assert arguments == {"title": "hi"}
    assert error == ""


def test_arguments_may_arrive_as_a_json_string():
    _, arguments, error = tool_search.resolve_call(
        assembled(),
        {"name": "mcp__github__create_issue", "arguments": '{"title": "hi"}'},
    )
    assert arguments == {"title": "hi"}
    assert error == ""


def test_broken_argument_json_is_reported():
    _, _, error = tool_search.resolve_call(
        assembled(), {"name": "mcp__github__create_issue", "arguments": "{"}
    )
    assert "not valid JSON" in error


def test_arguments_must_be_an_object():
    _, _, error = tool_search.resolve_call(
        assembled(), {"name": "mcp__github__create_issue", "arguments": [1, 2]}
    )
    assert "must be an object" in error


def test_a_call_needs_a_name():
    _, _, error = tool_search.resolve_call(assembled(), {"arguments": {}})
    assert "needs a 'name'" in error


def test_the_bridge_cannot_invoke_itself():
    _, _, error = tool_search.resolve_call(
        assembled(), {"name": tool_search.SEARCH, "arguments": {}}
    )
    assert "part of the bridge" in error


def test_calling_a_listed_tool_through_the_bridge_is_refused():
    _, _, error = tool_search.resolve_call(
        assembled(), {"name": "read_file", "arguments": {}}
    )
    assert "call it directly" in error


def test_a_blind_call_gets_the_schema_back():
    """A deferred tool's parameters are invisible until described, so models
    invoke one by name alone. A failure from inside the tool teaches nothing."""
    resolved, arguments, _ = tool_search.resolve_call(
        assembled(), {"name": "mcp__github__create_issue", "arguments": {}}
    )
    payload = json.loads(tool_search.missing_arguments(resolved, arguments))

    assert "missing required argument(s): title" in payload["error"]
    assert "NOT called" in payload["error"]
    assert "title" in payload["parameters"]["properties"]


def test_a_complete_call_is_not_blocked():
    resolved, arguments, _ = tool_search.resolve_call(
        assembled(),
        {"name": "mcp__github__create_issue", "arguments": {"title": "hi"}},
    )
    assert tool_search.missing_arguments(resolved, arguments) == ""


def test_a_tool_with_no_required_arguments_is_never_blocked():
    resolved, arguments, _ = tool_search.resolve_call(
        assembled(), {"name": "mcp__slack__send_message", "arguments": {}}
    )
    assert tool_search.missing_arguments(resolved, arguments) == ""


# ---------------------------------------------------------------------------
# odds and ends
# ---------------------------------------------------------------------------


def test_a_description_is_shortened_to_its_first_sentence():
    assert tool_search.short_description("Open an issue. Then close it.") == (
        "Open an issue"
    )


def test_a_long_description_is_clipped_on_a_word():
    result = tool_search.short_description("word " * 40, limit=20)
    assert len(result) <= 21
    assert result.endswith("…")


def test_an_empty_description_stays_empty():
    assert tool_search.short_description("") == ""


@pytest.mark.parametrize(
    "window,maximum,expected",
    [
        (200_000, 4000, 4000),
        (20_000, 4000, 1000),
        (None, 4000, 4000),
        (200_000, 0, 0),
    ],
)
def test_the_listing_budget(window, maximum, expected):
    assert tool_search.listing_budget(window, maximum) == expected


# ---------------------------------------------------------------------------
# through the real loop
# ---------------------------------------------------------------------------


def conversation_with(tmp_path, script, mcp_specs, **kwargs):
    from andromeda_agent import Conversation, Policy
    from andromeda_tools import Workspace, build_registry
    from andromeda_tools.todo import TodoList
    from support import ScriptedProvider

    workspace = Workspace(tmp_path)
    todos = TodoList()
    registry = build_registry(workspace, todos)
    for item in mcp_specs:
        registry[item.name] = item

    provider = ScriptedProvider(script=list(script))
    conversation = Conversation(
        provider=provider,  # type: ignore[arg-type]
        policy=Policy(
            mode="auto",
            enabled=frozenset(registry) | {"read_file"},
            max_tier="irreversible",
        ),
        workspace=workspace,
        todos=todos,
        registry=registry,
        **kwargs,
    )
    return conversation, provider


def bridge_call(name, arguments, call_id="call_1"):
    from support import call

    return call(name, arguments, call_id)


def test_the_provider_is_offered_the_bridge_not_the_mcp_tools(tmp_path):
    from support import turn_with

    calls = [mcp("github", "create_issue", "Open an issue.", title=True)]
    conversation, provider = conversation_with(tmp_path, ["done"], calls)

    conversation.send("go")

    offered = {
        item["function"]["name"] for item in provider.seen_tools[0]
    }
    assert tool_search.SEARCH in offered
    assert "mcp__github__create_issue" not in offered
    assert "read_file" in offered


def test_searching_through_the_loop_answers_from_the_catalogue(tmp_path):
    from support import turn_with

    calls = [mcp("github", "create_issue", "Open an issue on a repository.")]
    conversation, _ = conversation_with(
        tmp_path,
        [turn_with(bridge_call(tool_search.SEARCH, {"query": "open an issue"})), "done"],
        calls,
    )

    conversation.send("go")

    result = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "mcp__github__create_issue" in result["content"]


def test_describing_through_the_loop_returns_parameters(tmp_path):
    from support import turn_with

    calls = [mcp("github", "create_issue", "Open an issue.", title=True)]
    conversation, _ = conversation_with(
        tmp_path,
        [
            turn_with(
                bridge_call(tool_search.DESCRIBE, {"name": "mcp__github__create_issue"})
            ),
            "done",
        ],
        calls,
    )

    conversation.send("go")

    result = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "title" in result["content"]


def test_a_bridged_call_actually_runs_the_tool(tmp_path):
    from support import turn_with

    ran: list[dict] = []
    tool = recording(ran, title=True)

    conversation, _ = conversation_with(
        tmp_path,
        [
            turn_with(
                bridge_call(
                    tool_search.CALL,
                    {"name": tool.name, "arguments": {"title": "a bug"}},
                )
            ),
            "done",
        ],
        [tool],
    )

    conversation.send("go")

    assert ran == [{"title": "a bug"}]
    assert "opened" in [m for m in conversation.messages if m["role"] == "tool"][0]["content"]


def test_a_bridged_call_meets_the_approval_gate(tmp_path):
    """The bridge changes what the model can see, never what it may do."""
    from andromeda_agent import Callbacks
    from support import turn_with

    ran: list[dict] = []
    tool = recording(ran, title=True)

    conversation, _ = conversation_with(
        tmp_path,
        [
            turn_with(
                bridge_call(
                    tool_search.CALL,
                    {"name": tool.name, "arguments": {"title": "a bug"}},
                )
            ),
            "done",
        ],
        [tool],
    )
    conversation.policy = conversation.policy.narrow(mode="ask")

    asked: list[str] = []
    conversation.send(
        "go",
        Callbacks(ask_approval=lambda request: (asked.append(request.spec.name), "no")[1]),
    )

    assert asked == ["mcp__github__create_issue"]
    assert ran == []


def test_a_bridged_call_fires_the_tool_hooks(tmp_path):
    from andromeda_agent import hooks
    from support import turn_with

    hooks.reset()
    seen: list[str] = []
    hooks.register("pre_tool_call", lambda **kwargs: seen.append(kwargs["tool_name"]))

    tool = mcp("github", "create_issue", "Open an issue.", title=True)
    conversation, _ = conversation_with(
        tmp_path,
        [
            turn_with(
                bridge_call(
                    tool_search.CALL,
                    {"name": tool.name, "arguments": {"title": "a bug"}},
                )
            ),
            "done",
        ],
        [tool],
    )
    try:
        conversation.send("go")
    finally:
        hooks.reset()

    # The underlying tool, not the bridge: a hook counting calls must see what
    # actually ran.
    assert seen == ["mcp__github__create_issue"]


def test_a_blind_bridged_call_gets_the_schema_rather_than_running(tmp_path):
    from support import turn_with

    ran: list[dict] = []
    tool = recording(ran, title=True)

    conversation, _ = conversation_with(
        tmp_path,
        [turn_with(bridge_call(tool_search.CALL, {"name": tool.name, "arguments": {}})), "done"],
        [tool],
    )

    conversation.send("go")

    assert ran == []
    result = [m for m in conversation.messages if m["role"] == "tool"][0]["content"]
    assert "missing required argument" in result


def test_turning_it_off_lists_the_mcp_tools_directly(tmp_path):
    calls = [mcp("github", "create_issue", "Open an issue.")]
    conversation, provider = conversation_with(
        tmp_path, ["done"], calls, tool_search_mode="off"
    )

    conversation.send("go")

    offered = {item["function"]["name"] for item in provider.seen_tools[0]}
    assert "mcp__github__create_issue" in offered
    assert tool_search.SEARCH not in offered


def test_a_bridge_call_with_no_bridge_active_is_an_unknown_tool(tmp_path):
    from support import turn_with

    conversation, _ = conversation_with(
        tmp_path, [turn_with(bridge_call(tool_search.SEARCH, {"query": "x"})), "done"], []
    )

    conversation.send("go")

    result = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "no tool named" in result["content"]
