"""Reading and editing what the agent remembers, by hand.

The agent reaches memory through `memory_search`, `memory_store` and
`memory_forget`. This is the other side of the same store, for the person: what
does it think it knows, and take that back.

It matters because standing memories go into **every** prompt. A wrong one is
not a stale note in a file, it is a false premise the agent argues from until
somebody removes it — and until now the only way to remove one was to ask the
agent to, which requires knowing it is there.

Every command here goes through `MemoryStore`, never the file, so it behaves
identically on both backends and cannot leave the two disagreeing.
"""

from __future__ import annotations

import time
from datetime import datetime

from andromeda_tools import MemoryStore
from andromeda_tools.memory import SCOPES

from .. import config as config_module
from .. import output


def _store() -> MemoryStore:
    config = config_module.load()
    store = MemoryStore(config_module.home() / "memory", config["memory_backend"])
    if store.backend_note:
        # Said once, up front: every listing below would otherwise be silently
        # from a different backend than the one configured.
        output.info(f"  {store.backend_note}")
    return store


def _age(stamp: float) -> str:
    seconds = max(0.0, time.time() - stamp)
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 129600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def _print(memories) -> None:
    for memory in memories:
        mark = "[yellow]★[/yellow]" if memory.scope == "standing" else " "
        tags = f" [dim]({', '.join(memory.tags)})[/dim]" if memory.tags else ""
        output.console.print(
            f" {mark} [dim]{_age(memory.created_at).rjust(4)}[/dim]  "
            f"{memory.content}{tags}"
        )


def show_list(scope: str = "") -> int:
    store = _store()
    memories = sorted(store.load(), key=lambda item: item.created_at, reverse=True)
    if scope:
        if scope not in SCOPES:
            output.fail(f"scope must be one of {', '.join(SCOPES)} — got {scope!r}.")
            return 2
        memories = [item for item in memories if item.scope == scope]

    if not memories:
        output.info("Nothing remembered yet." if not scope else f"No {scope} memories.")
        return 0

    _print(memories)
    output.console.print()
    standing = sum(1 for item in memories if item.scope == "standing")
    output.info(
        f"  {len(memories)} remembered · {standing} standing (★, in every prompt)"
    )
    output.console.print(f"  [dim]{store.file}[/dim]", soft_wrap=True)
    return 0


def find(query: str, limit: int = 10) -> int:
    store = _store()
    result = store.search(query, limit=limit)
    # Printed as the agent would receive it, deliberately: "why did it not
    # recall that" is answered by seeing the same thing it saw.
    output.console.print(result.content, markup=False, highlight=False)
    return 0 if result.ok else 1


def forget(query: str, scope: str = "any", force: bool = False) -> int:
    store = _store()
    if scope not in (*SCOPES, "any"):
        output.fail(f"scope must be standing, episode or any — got {scope!r}.")
        return 2

    from andromeda_tools.memory import DEFAULT_MIN_SCORE, score

    doomed = [
        memory
        for memory in store.load()
        if (scope == "any" or memory.scope == scope)
        and score(query, memory.content) >= DEFAULT_MIN_SCORE
    ]
    if not doomed:
        output.info(f"Nothing remembered matched {query!r}.")
        return 1

    if not force:
        # Shown before it happens, because forgetting matches generously by
        # design — the count alone does not tell you it caught the right ones.
        _print(doomed)
        output.console.print()
        output.fail(
            f"This would forget {len(doomed)} memor"
            f"{'y' if len(doomed) == 1 else 'ies'}.",
            "Pass --force to do it. There is no undo.",
        )
        return 2

    result = store.forget(query, scope)
    output.ok(result.content)
    return 0


def remember(content: str, scope: str = "episode", tags: str = "") -> int:
    """Teach it something directly, without spending a turn on it."""
    store = _store()
    result = store.store(
        content,
        scope=scope,
        tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
    )
    if not result.ok:
        output.fail(result.content)
        return 2
    output.ok(result.content)
    if scope == "standing":
        output.info("  Standing memories load into every prompt. Keep them few.")
    return 0


def export(path: str = "") -> int:
    """Every memory as JSON, whichever backend holds them.

    Separate from `andromeda export`, which carries a whole install. This is
    for reading, diffing or editing them by hand — and it is the only way to
    see a sqlite-backed store as text.
    """
    import json
    from pathlib import Path

    store = _store()
    payload = json.dumps(
        [memory.to_json() for memory in store.load()], indent=2, ensure_ascii=False
    )
    if not path:
        print(payload)
        return 0
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload + "\n", encoding="utf-8")
    output.ok(f"Wrote {destination}")
    return 0


def stats() -> int:
    from andromeda_tools.memory import MAX_STANDING

    store = _store()
    memories = store.load()
    standing = [item for item in memories if item.scope == "standing"]
    output.console.print(f"  [dim]backend[/dim]   {store.backend.name}")
    output.console.print(f"  [dim]file[/dim]      {store.file}")
    output.console.print(f"  [dim]total[/dim]     {len(memories)}")
    output.console.print(
        f"  [dim]standing[/dim]  {len(standing)} of {MAX_STANDING} "
        "(the cap; oldest are dropped past it)"
    )
    if memories:
        oldest = min(item.created_at for item in memories)
        newest = max(item.created_at for item in memories)
        output.console.print(
            f"  [dim]span[/dim]      {datetime.fromtimestamp(oldest):%Y-%m-%d}"
            f" → {datetime.fromtimestamp(newest):%Y-%m-%d}"
        )
    return 0
