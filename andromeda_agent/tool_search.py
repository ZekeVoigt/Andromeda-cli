"""Tools the model finds rather than tools it is handed.

Every tool in the array costs tokens on every request, whether or not it is
used. Thirty built-in tools is a few thousand tokens and worth it. Then
somebody connects three MCP servers and the array is four hundred tools, most
of which will never be called in this session — and that cost is paid on every
turn of every conversation, forever.

So MCP tools are not listed. Three bridge tools stand in for all of them:

    tool_search    find a capability by describing it
    tool_describe  load one tool's parameters
    tool_call      invoke it

**Built-in tools never defer.** They are the surface this program is; hiding
them behind a search would make the agent slower at everything it does most.
Only the unbounded catalog defers, which is exactly the part that is unbounded.

How much is shown depends on how much fits:

  tier 0   no MCP tools at all — nothing changes, everything is listed
  tier 1   the bridge, plus a listing of every deferred tool by name and one
           line of description, so the model knows what exists
  tier 2   the bridge, plus a count per server — a catalogue whose *names*
           alone do not fit the budget

Tier 1 is the important one. A bridge with no listing produces a model that
does not know what it does not know: it says a capability is unavailable
rather than searching for it. The listing is the same trick the skills
manifest uses — names are cheap, schemas are not.

A call through `tool_call` goes through the ordinary dispatch path: the same
policy check, the same approval prompt, the same hooks, the same transforms.
The bridge changes what the model can *see*, never what it is allowed to do.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from andromeda_tools import ToolSpec

SEARCH = "tool_search"
DESCRIBE = "tool_describe"
CALL = "tool_call"

BRIDGE_NAMES: frozenset[str] = frozenset({SEARCH, DESCRIBE, CALL})

MCP_PREFIX = "mcp__"

# Chars per token, for deciding whether a listing fits. A rule of thumb rather
# than a tokenizer: the number decides between three renderings, and being
# 15% out changes nothing about which one is right.
CHARS_PER_TOKEN = 4.0

# The listing may take this share of the context window, capped by the
# configured absolute budget. Both, because a small window and a large one
# have different right answers and neither is a fraction of the other.
LISTING_CONTEXT_SHARE = 0.05
FALLBACK_CONTEXT_BUDGET = 10_000

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 25

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
SENTENCE_END_RE = re.compile(r"[.!?\n]")


def is_deferrable(name: str) -> bool:
    """Whether a tool belongs in the catalogue rather than the array.

    MCP only. Every built-in stays listed — "always available" has to mean
    always available, or the rule is not a rule.
    """
    return name.startswith(MCP_PREFIX) and name not in BRIDGE_NAMES


def is_bridge(name: str) -> bool:
    return name in BRIDGE_NAMES


@dataclass
class Entry:
    """One deferred tool, in the form the bridge searches and serves."""

    spec: ToolSpec
    tokens: list[str] = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def description(self) -> str:
        return self.spec.description or ""

    @property
    def server(self) -> str:
        """The MCP server a tool came from — `mcp__<server>__<tool>`."""
        rest = self.name[len(MCP_PREFIX):]
        return rest.split("__", 1)[0] if "__" in rest else "other"


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def search_text(spec: ToolSpec) -> str:
    """What a tool is indexed on.

    Name, description, and the top-level parameter names. Separators in the
    name are broken into words so a query of "create issue" reaches
    `mcp__github__create_issue`. The schema body is deliberately left out —
    it adds noise and no recall.
    """
    schema = spec.to_openai().get("function", {})
    properties = (schema.get("parameters") or {}).get("properties") or {}
    words = re.sub(r"[_.:\-]+", " ", spec.name)
    return f"{words} {spec.description or ''} {' '.join(properties)}"


def build_catalog(specs: list[ToolSpec]) -> list[Entry]:
    return [Entry(spec=spec, tokens=tokenize(search_text(spec))) for spec in specs]


def estimate_tokens(schemas: list[dict[str, Any]]) -> int:
    total = 0
    for schema in schemas:
        try:
            total += len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total += len(str(schema))
    return int(math.ceil(total / CHARS_PER_TOKEN))


def listing_budget(context_window: int | None, maximum: int) -> int:
    share = (
        int(context_window * LISTING_CONTEXT_SHARE)
        if context_window and context_window > 0
        else FALLBACK_CONTEXT_BUDGET
    )
    return max(0, min(maximum, share))


# ---------------------------------------------------------------------------
# Finding one
# ---------------------------------------------------------------------------


def bm25(
    query_tokens: list[str],
    doc_tokens: list[str],
    average_length: float,
    document_frequency: dict[str, int],
    total_documents: int,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Ordinary BM25, written out rather than imported.

    A few dozen lines against a dependency that would have to be installed,
    pinned and updated for a catalogue of a few hundred short documents.
    """
    if not doc_tokens:
        return 0.0

    counts: dict[str, int] = {}
    for token in doc_tokens:
        counts[token] = counts.get(token, 0) + 1

    length = len(doc_tokens)
    score = 0.0
    for token in query_tokens:
        frequency = document_frequency.get(token, 0)
        if frequency == 0:
            continue
        term_count = counts.get(token, 0)
        if term_count == 0:
            continue
        idf = math.log(1 + (total_documents - frequency + 0.5) / (frequency + 0.5))
        norm = term_count * (k1 + 1) / (
            term_count + k1 * (1 - b + b * length / max(average_length, 1.0))
        )
        score += idf * norm
    return score


def search(catalog: list[Entry], query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[Entry]:
    """The best matches for a query, by BM25, then by substring.

    The fallback matters more than it looks. BM25 gives a term that appears in
    every document an IDF of zero, so a catalogue where every tool is named
    `github_*` scores *nothing* for the query "github" — the one query a
    person would expect to work.
    """
    if not catalog or limit <= 0:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    lengths = [len(entry.tokens) for entry in catalog]
    average = sum(lengths) / max(len(lengths), 1)
    frequency: dict[str, int] = {}
    for entry in catalog:
        for token in set(entry.tokens):
            frequency[token] = frequency.get(token, 0) + 1

    scored: list[tuple[float, int, Entry]] = []
    for index, entry in enumerate(catalog):
        score = bm25(query_tokens, entry.tokens, average, frequency, len(catalog))
        if score > 0:
            scored.append((score, index, entry))

    if not scored:
        lowered = query.lower()
        scored = [
            (0.1, index, entry)
            for index, entry in enumerate(catalog)
            if lowered in entry.name.lower()
        ]

    # Index is the tie-break, so equal scores come back in catalogue order
    # rather than in whatever order the sort happened to leave them.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]]


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


def short_description(text: str, limit: int = 60) -> str:
    """One terse line, the way the skills manifest does it."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    end = SENTENCE_END_RE.search(collapsed)
    if end and end.start() > 0:
        collapsed = collapsed[: end.start()]
    if len(collapsed) <= limit:
        return collapsed
    clipped = collapsed[:limit].rsplit(" ", 1)[0]
    return (clipped or collapsed[:limit]) + "…"


def render_listing(catalog: list[Entry], form: str) -> str:
    """The catalogue, in one of three densities."""
    if not catalog or form == "none":
        return ""

    grouped: dict[str, list[Entry]] = {}
    for entry in catalog:
        grouped.setdefault(entry.server, []).append(entry)

    lines: list[str] = []
    for server in sorted(grouped):
        entries = sorted(grouped[server], key=lambda item: item.name)
        if form == "groups":
            plural = "" if len(entries) == 1 else "s"
            lines.append(f"  {server}: {len(entries)} tool{plural}")
            continue
        lines.append(f"  {server}:")
        for entry in entries:
            if form == "names":
                lines.append(f"    {entry.name}")
            else:
                detail = short_description(entry.description)
                lines.append(f"    {entry.name}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)


def choose_listing(catalog: list[Entry], budget: int) -> tuple[str, str]:
    """The densest listing that fits. Returns (form, text).

    Degrading rather than truncating: half a catalogue looks like a whole one,
    and a model that reads it concludes the missing half does not exist.
    """
    if not catalog or budget <= 0:
        return "none", ""
    for form in ("full", "names", "groups"):
        text = render_listing(catalog, form)
        if estimate_tokens([{"listing": text}]) <= budget:
            return form, text
    return "none", ""


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


def bridge_schemas(count: int, listing: str = "", form: str = "none") -> list[dict[str, Any]]:
    """The three tools that stand in for all the others.

    Kept short on purpose — every byte here is paid on every turn, which is
    the cost this whole module exists to avoid.
    """
    plural = "" if count == 1 else "s"
    description = (
        f"Find one of {count} additional tool{plural} that are loaded on demand. "
        f"Returns matches with a name and a description. Then `{DESCRIBE}` for "
        f"its parameters, then `{CALL}` to invoke it. The tools listed directly "
        f"in this array are already available and need no search."
    )

    if listing and form == "groups":
        description += (
            "\n\nThese servers are connected and their tools ARE reachable "
            "through this bridge. For anything in these domains, search here "
            "first — do not say the capability is missing, and do not reach "
            "for the terminal instead without searching.\n\n" + listing
        )
    elif listing:
        description += (
            "\n\nEverything deferred is listed below. If a name appears here it "
            f"exists — load it with `{DESCRIBE}` and skip `{SEARCH}` when you "
            "already know the exact name.\n\n" + listing
        )

    return [
        {
            "type": "function",
            "function": {
                "name": SEARCH,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "What you need the tool to do, in words — "
                                "'create a github issue', 'read a sheet'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"How many matches. Default {DEFAULT_SEARCH_LIMIT}.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": DESCRIBE,
                "description": (
                    f"Load the full parameter schema for one deferred tool. "
                    f"Do this before `{CALL}` unless you already know its arguments."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The exact tool name.",
                        }
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CALL,
                "description": (
                    "Invoke a deferred tool. Its approval, its risk tier and "
                    "every hook apply exactly as they would if the tool were "
                    "listed directly — this only changes what you can see."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The exact tool name."},
                        "arguments": {
                            "type": "object",
                            "description": "Arguments matching that tool's schema.",
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]


@dataclass
class Assembly:
    """What one turn's tool array ended up being."""

    schemas: list[dict[str, Any]]
    deferred: dict[str, ToolSpec] = field(default_factory=dict)
    activated: bool = False
    tier: int = 0
    form: str = "none"
    saved_tokens: int = 0

    @property
    def catalog(self) -> list[Entry]:
        return build_catalog(list(self.deferred.values()))


def assemble(
    specs: list[ToolSpec],
    *,
    context_window: int | None = None,
    mode: str = "auto",
    listing_max_tokens: int = 4000,
) -> Assembly:
    """Split a tool list into what is listed and what is searchable.

    Stateless, and rebuilt every turn from the live registry. A catalogue kept
    across turns drifts out of step with the tools that actually exist, and
    the failure is silent: a tool the model can see and cannot call.
    """
    direct: list[ToolSpec] = []
    deferred: list[ToolSpec] = []
    for spec in specs:
        (deferred if is_deferrable(spec.name) else direct).append(spec)

    if mode == "off" or not deferred:
        return Assembly(schemas=[spec.to_openai() for spec in specs])

    deferred_schemas = [spec.to_openai() for spec in deferred]
    catalog = build_catalog(deferred)
    budget = listing_budget(context_window, listing_max_tokens)
    form, listing = choose_listing(catalog, budget)

    bridges = bridge_schemas(len(deferred), listing, form)
    schemas = [spec.to_openai() for spec in direct] + bridges

    return Assembly(
        schemas=schemas,
        deferred={spec.name: spec for spec in deferred},
        activated=True,
        tier=1 if form in {"full", "names"} else 2,
        form=form,
        saved_tokens=max(
            0, estimate_tokens(deferred_schemas) - estimate_tokens(bridges)
        ),
    )


# ---------------------------------------------------------------------------
# Answering the three bridge calls
# ---------------------------------------------------------------------------


def dispatch_search(assembly: Assembly, arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    raw = arguments.get("limit")
    try:
        limit = int(raw) if raw is not None else DEFAULT_SEARCH_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_LIMIT
    limit = max(1, min(MAX_SEARCH_LIMIT, limit))

    catalog = assembly.catalog
    hits = search(catalog, query, limit)
    result: dict[str, Any] = {
        "query": query,
        "available": len(catalog),
        "matches": [
            {
                "name": entry.name,
                "server": entry.server,
                "description": entry.description[:400],
            }
            for entry in hits
        ],
    }

    if not hits and catalog:
        # A lexical miss is not evidence that a capability is absent, and a
        # model told only "no matches" concludes exactly that. Say what is
        # connected so the next query can be a better one.
        counts: dict[str, int] = {}
        for entry in catalog:
            counts[entry.server] = counts.get(entry.server, 0) + 1
        result["connected"] = [
            {"server": server, "tools": counts[server]} for server in sorted(counts)
        ]
        result["hint"] = (
            "Nothing matched those words, but the servers above are connected "
            "and their tools are reachable. Try the service name with a "
            "concrete action before concluding this cannot be done."
        )
    return json.dumps(result, ensure_ascii=False)


def dispatch_describe(assembly: Assembly, arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"})

    spec = assembly.deferred.get(name)
    if spec is None:
        return json.dumps(
            {
                "error": (
                    f"{name!r} is not one of the deferred tools. If you can see "
                    f"it in the tools array, call it directly; otherwise check "
                    f"the name with {SEARCH}."
                )
            }
        )

    schema = spec.to_openai().get("function", {})
    return json.dumps(
        {
            "name": name,
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {}),
        },
        ensure_ascii=False,
    )


def resolve_call(
    assembly: Assembly, arguments: dict[str, Any]
) -> tuple[ToolSpec | None, dict[str, Any], str]:
    """Turn a `tool_call` into the real tool and its arguments.

    Returns (spec, arguments, error). The caller then dispatches that spec
    through the ordinary path — which is the point: policy, approval and hooks
    must not have a second implementation reachable through the bridge.
    """
    name = str(arguments.get("name") or "").strip()
    if not name:
        return None, {}, f"{CALL} needs a 'name'."
    if is_bridge(name):
        return None, {}, f"{CALL} cannot invoke {name!r} — it is part of the bridge."

    raw = arguments.get("arguments")
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, {}, f"{CALL} 'arguments' is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, {}, f"{CALL} 'arguments' must be an object."

    spec = assembly.deferred.get(name)
    if spec is None:
        return None, {}, (
            f"{name!r} is not a deferred tool. If it is listed in the tools "
            f"array, call it directly."
        )

    return spec, raw, ""


def missing_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """The required arguments a blind call left out, if any.

    A deferred tool's parameters are invisible until `tool_describe` is
    called, so models routinely invoke one by name alone. Dispatching that
    produces a failure from inside the tool that says nothing about what was
    expected, and a cheap model will loop on it. Handing back the schema
    instead repairs the call in one round trip.

    Key absence only — no type checking. Types are the tool's own business,
    and a validator that gets clever here blocks legitimate calls.
    """
    schema = spec.to_openai().get("function", {})
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return ""
    required = parameters.get("required")
    if not isinstance(required, list) or not required:
        return ""
    missing = [
        name for name in required if isinstance(name, str) and name not in arguments
    ]
    if not missing:
        return ""
    return json.dumps(
        {
            "error": (
                f"{CALL} to {spec.name!r} is missing required argument(s): "
                f"{', '.join(missing)}. The tool was NOT called."
            ),
            "parameters": parameters,
            "hint": "Call again with arguments matching the schema above.",
        },
        ensure_ascii=False,
    )
