"""Read tool definitions out of the TypeScript registry.

Enough of a parser to pull `name`, `description` and the `parameters` schema
for one tool. The definitions are prettier-formatted object literals, which are
JSON apart from bare keys and trailing commas — both mechanical to fix.

Fragile by nature, so `test_registry_drift.py` asserts the parse worked before
it asserts anything about the contents. A silently-empty parse would make the
whole guard vacuous, which is worse than no guard.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SOURCES = (
    "lib/agent-runtime/tools/definitions.ts",
    "lib/agent-runtime/tools/local-gateway-tools.ts",
    # The delegation family. Included so the overlap check sees
    # `sessions_spawn` — otherwise a future port could take that name without
    # anything noticing.
    "lib/agent-runtime/subagents/tool-definitions.ts",
)

TRAILING_COMMA = re.compile(r",(\s*[}\]])")
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def repo_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    return root if (root / SOURCES[0]).exists() else None


def _sources() -> str:
    root = repo_root()
    if root is None:
        return ""
    return "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in SOURCES
        if (root / relative).exists()
    )


def _balanced_block(text: str, start: int) -> str | None:
    """Return the {...} beginning at `start`, respecting strings."""
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                in_string = False
            continue
        if character in {'"', "'", "`"}:
            in_string, quote = True, character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _quote_bare_keys(block: str) -> str:
    """Quote object keys that TypeScript leaves bare.

    Scanned rather than regexed: descriptions in this file are full of colons
    and commas, and a pattern that ignores string boundaries rewrites the
    middle of a sentence. Keys are only recognised outside a string, and only
    where an identifier is followed by a colon.
    """
    out: list[str] = []
    index = 0
    in_string = False
    quote = ""
    escaped = False

    while index < len(block):
        character = block[index]

        if in_string:
            out.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                in_string = False
            index += 1
            continue

        if character in {'"', "'", "`"}:
            in_string, quote = True, character
            out.append(character)
            index += 1
            continue

        match = IDENTIFIER.match(block, index)
        if match:
            word = match.group(0)
            after = match.end()
            while after < len(block) and block[after] in " \t\n":
                after += 1
            if after < len(block) and block[after] == ":":
                out.append(f'"{word}"')
                index = match.end()
                continue
            # A bare word that is not a key: `true`, `false`, `null`.
            out.append(word)
            index = match.end()
            continue

        out.append(character)
        index += 1

    return "".join(out)


def _to_json(block: str) -> dict | None:
    cleaned = _quote_bare_keys(block)
    cleaned = TRAILING_COMMA.sub(r"\1", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def tool_names() -> set[str]:
    return set(re.findall(r'^\s*name:\s*"([a-z0-9_]+)"', _sources(), re.MULTILINE))


def parameters_for(name: str) -> dict | None:
    """The `parameters` schema declared for `name`, as a dict."""
    text = _sources()
    anchor = re.search(rf'^\s*name:\s*"{re.escape(name)}",\s*$', text, re.MULTILINE)
    if anchor is None:
        return None

    marker = text.find("parameters:", anchor.end())
    if marker == -1:
        return None

    # `parameters:` is sometimes a named constant rather than an inline object
    # — the subagents family declares `SUBAGENTS_LIST_SCHEMA` and friends. Take
    # the constant's own definition in that case; searching for the next `{`
    # would find some unrelated block further down the file.
    tail = text[marker + len("parameters:") :]
    reference = re.match(r"\s*([A-Z][A-Z0-9_]*),", tail)
    if reference:
        return _named_schema(text, reference.group(1))

    brace = text.find("{", marker)
    if brace == -1:
        return None

    block = _balanced_block(text, brace)
    return _to_json(block) if block else None


def _named_schema(text: str, constant: str) -> dict | None:
    declaration = re.search(rf"(?:const|let|var)\s+{re.escape(constant)}\b", text)
    if declaration is None:
        return None
    brace = text.find("{", declaration.end())
    if brace == -1:
        return None
    block = _balanced_block(text, brace)
    return _to_json(block) if block else None


TIER_MAP = re.compile(r"^\s*([a-z0-9_]+):\s*\"(safe_local|outbound|destructive|irreversible)\",", re.MULTILINE)


def risk_tiers() -> dict[str, str]:
    """`TOOL_RISK_TIERS` — the explicit tier assignments.

    Read separately from the definitions because a tool's tier is not declared
    beside its schema; it lives in one map at the top of the file, and a Python
    port that reads only the schema will get the tier wrong without noticing.
    """
    root = repo_root()
    if root is None:
        return {}
    text = (root / SOURCES[0]).read_text(encoding="utf-8")
    start = text.find("export const TOOL_RISK_TIERS")
    if start == -1:
        return {}
    block = _balanced_block(text, text.find("{", start))
    return dict(TIER_MAP.findall(block or ""))


def category_for(name: str) -> str | None:
    """The `category` declared beside a tool's definition.

    Worth comparing as well as the tier: the specialist belts are written in
    terms of category, so a tool that is `read` in one registry and `write` in
    the other is admitted by different lanes on the two surfaces.
    """
    text = _sources()
    anchor = re.search(rf'^\s*name:\s*"{re.escape(name)}",\s*$', text, re.MULTILINE)
    if anchor is None:
        return None
    window = text[anchor.end() : anchor.end() + 4000]
    found = re.search(r'^\s*category:\s*"(\w+)"', window, re.MULTILINE)
    return found.group(1) if found else None


def resolved_tier(name: str) -> str | None:
    """`resolveToolRiskTier` for one name.

    An explicit entry in TOOL_RISK_TIERS wins; otherwise the fallbacks apply,
    and `category: "read"` resolves to `safe_local`. The `subagents_*` family
    reaches its tier that way, so a test that only read the map would think
    they had none.
    """
    explicit = risk_tiers().get(name)
    if explicit:
        return explicit
    category = category_for(name)
    if category == "read":
        return "safe_local"
    if category == "admin":
        return "destructive"
    if category is None:
        return None
    return "outbound"
