"""Durable memory, on the user's own disk.

Names and schemas mirror the TypeScript registry's `memory_search`,
`memory_store` and `memory_forget` exactly — same parameters, same `standing`
vs `episode` split, same supersession behaviour — because a person who taught
one surface something reasonably expects the other to know it.

**One divergence, stated rather than hidden:** the hosted runtime scores recall
semantically. This scores it *lexically* — term overlap, normalized to 0..1. So
`minScore` is comparable in range but not in meaning: a paraphrase the hosted
side would recall may score zero here. That is the honest cost of not shipping
an embedding model with a terminal client, and it is why the default threshold
is low and why `memory_forget` matches generously.

Storage is pluggable — see `memory_backends`. The scoring above is *not*, on
purpose: a backend that changed what `minScore` means would keep the parameter
name while silently retuning every threshold set against it.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import memory_backends
from .spec import ToolResult, failure

SCOPES = ("standing", "episode")
DEFAULT_LIMIT = 5
DEFAULT_MIN_SCORE = 0.3
# Standing memories are injected into every system prompt, so the space is
# deliberately small — the bar for "this defines how I work with you" is high.
MAX_STANDING = 40
CONSOLIDATE_AT = 0.85

WORD = re.compile(r"[a-z0-9']+")
STOPWORDS = frozenset(
    """a an and are as at be by for from has have i in is it its of on or that the
    their they this to was were will with you your me my""".split()
)


def _terms(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower()) if word not in STOPWORDS}


def score(query: str, content: str) -> float:
    """Fraction of the query's meaningful terms present in the content.

    Coverage of the *query*, not symmetric overlap: a long memory that contains
    everything asked about should score 1.0, and dividing by its length would
    punish it for being detailed.
    """
    wanted = _terms(query)
    if not wanted:
        return 0.0
    return len(wanted & _terms(content)) / len(wanted)


@dataclass
class Memory:
    id: str
    content: str
    scope: str = "episode"
    category: str = ""
    tags: list[str] = field(default_factory=list)
    path: str = ""
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "scope": self.scope,
            "category": self.category,
            "tags": self.tags,
            "path": self.path,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Memory | None":
        content = str(raw.get("content") or "").strip()
        if not content:
            return None
        scope = str(raw.get("scope") or "episode")
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            content=content,
            scope=scope if scope in SCOPES else "episode",
            category=str(raw.get("category") or ""),
            tags=[str(tag) for tag in (raw.get("tags") or []) if str(tag).strip()],
            path=str(raw.get("path") or ""),
            created_at=float(raw.get("created_at") or time.time()),
        )


class MemoryStore:
    """The operations. Where the memories physically live is the backend's.

    Supersession, consolidation and the standing-memory budget are policy and
    stay here, identical on every backend — a store that consolidated on one
    backend and accumulated near-duplicates on another would make the tool
    description true only sometimes.
    """

    def __init__(
        self, root: Path, backend: "str | memory_backends.MemoryBackend | None" = None
    ) -> None:
        self.root = Path(root)
        if isinstance(backend, memory_backends.MemoryBackend):
            self.backend = backend
            self.backend_note = ""
        else:
            self.backend, self.backend_note = memory_backends.build(
                backend or memory_backends.DEFAULT_BACKEND, self.root
            )

    @property
    def file(self) -> Path:
        """Where this store's memories are, whichever backend is in use."""
        return self.backend.file

    # ---- persistence -----------------------------------------------------

    def load(self) -> list[Memory]:
        return self.backend.load()

    def save(self, memories: Iterable[Memory]) -> None:
        self.backend.replace(memories)

    # ---- operations ------------------------------------------------------

    def standing(self) -> list[Memory]:
        return [memory for memory in self.load() if memory.scope == "standing"]

    def store(
        self,
        content: str,
        scope: str = "episode",
        category: str | None = None,
        replaces: str | None = None,
        path: str | None = None,
        tags: list[str] | None = None,
    ) -> ToolResult:
        content = (content or "").strip()
        if not content:
            return failure("A memory needs content.")
        if scope not in SCOPES:
            return failure(f"scope must be one of {', '.join(SCOPES)} — got {scope!r}.")

        memories = self.load()
        removed = 0

        # `replaces` first: the caller is telling us what this makes untrue.
        if replaces and replaces.strip():
            keep = [m for m in memories if score(replaces, m.content) < DEFAULT_MIN_SCORE]
            removed += len(memories) - len(keep)
            memories = keep

        # Then consolidation. Restating a known fact should reinforce it, not
        # accumulate near-duplicates — the tool description promises this.
        survivors = []
        for memory in memories:
            if memory.scope == scope and score(content, memory.content) >= CONSOLIDATE_AT:
                removed += 1
                continue
            survivors.append(memory)
        memories = survivors

        memories.append(
            Memory(
                id=uuid.uuid4().hex,
                content=content,
                scope=scope,
                category=(category or "").strip(),
                tags=[tag.strip() for tag in (tags or []) if str(tag).strip()],
                path=(path or "").strip(),
            )
        )

        # Trim standing by age, never episodes. Standing is a budget on every
        # turn's prompt; episodes only cost when they are recalled.
        standing = [m for m in memories if m.scope == "standing"]
        if len(standing) > MAX_STANDING:
            oldest = sorted(standing, key=lambda m: m.created_at)[: len(standing) - MAX_STANDING]
            dropped = {m.id for m in oldest}
            memories = [m for m in memories if m.id not in dropped]

        self.save(memories)

        note = f" ({removed} superseded)" if removed else ""
        return ToolResult(
            content=f"Remembered ({scope}){note}.",
            display=f"remembered: {content[:80]}",
            metadata={"superseded": removed, "scope": scope},
        )

    def search(
        self, query: str, limit: int = DEFAULT_LIMIT, min_score: float = DEFAULT_MIN_SCORE
    ) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return failure("A search needs a query.")

        limit = max(1, int(limit or DEFAULT_LIMIT))
        threshold = max(0.0, min(float(min_score), 1.0))

        # Candidates from the backend rather than the whole store: an index
        # narrows this, and a backend without one returns everything, which is
        # the same set in a different order.
        ranked = sorted(
            ((score(query, m.content), m) for m in self.backend.candidates(query)),
            key=lambda pair: (pair[0], pair[1].created_at),
            reverse=True,
        )
        hits = [(value, memory) for value, memory in ranked if value >= threshold][:limit]

        if not hits:
            return ToolResult(
                content=f"Nothing remembered about {query!r}.",
                display="no memories matched",
            )

        lines = [
            f"[{memory.scope}] {memory.content}"
            + (f" (tags: {', '.join(memory.tags)})" if memory.tags else "")
            for _, memory in hits
        ]
        return ToolResult(
            content="\n".join(lines),
            display=f"{len(hits)} memor{'y' if len(hits) == 1 else 'ies'}",
            metadata={"count": len(hits)},
        )

    def forget(self, query: str, scope: str = "any") -> ToolResult:
        query = (query or "").strip()
        if not query:
            return failure("Say what to forget.")
        if scope not in (*SCOPES, "any"):
            return failure(f"scope must be standing, episode or any — got {scope!r}.")

        memories = self.load()
        keep, removed = [], 0
        for memory in memories:
            in_scope = scope == "any" or memory.scope == scope
            if in_scope and score(query, memory.content) >= DEFAULT_MIN_SCORE:
                removed += 1
                continue
            keep.append(memory)

        if not removed:
            return ToolResult(
                content=f"Nothing remembered matched {query!r}.", display="nothing to forget"
            )

        self.save(keep)
        return ToolResult(
            content=f"Forgot {removed} memor{'y' if removed == 1 else 'ies'}.",
            display=f"forgot {removed}",
            metadata={"removed": removed},
        )
