"""Every read query over the index: search, listing, and anchored reads.

Named `queries` rather than `search` because the package re-exports
`search` as a function, and a module and a function of one name in one
namespace means `state.search` silently resolves to whichever was bound
last. One name, one thing.

Searching every past session costs no model tokens.

The expensive way to answer "what did we decide about the retry budget in
March" is to hand a model a pile of transcripts. The cheap way is an index,
and this is it: one SQLite query, no inference, no tokens.

Three routes, chosen by what the query is rather than by configuration:

  ``messages_fts``      the default. Ranked by bm25, with a snippet showing
                        the match in context.
  ``messages_trigram``  for CJK. The default tokenizer splits Chinese,
                        Japanese and Korean into single characters, so phrase
                        matching against it does not work at all.
  ``LIKE``              the fallback, for a SQLite without FTS5, a query the
                        sanitizer empties out, and short CJK terms the trigram
                        tokenizer cannot represent. Slower, still correct.

**A query that raises must fall back, never return nothing.** FTS5 has its own
grammar, and an unescaped ``:`` or ``'`` makes MATCH raise; catching that and
reporting zero results is indistinguishable to the user from "there is nothing
there", which is the one wrong answer available.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db as db_module

# Search queries do not need to be arbitrarily long, and bounding the input
# keeps the sanitizer's behaviour predictable on hostile text.
MAX_QUERY_CHARS = 2048

# Every character FTS5's query grammar rejects outside a quoted phrase.
# Anything missing here reaches MATCH raw and raises. Assembled through
# `re.escape` so the backslash cannot be eaten as a regex escape inside the
# class. `%` is deliberately absent: the LIKE fallback needs it preserved as a
# literal and escapes wildcards itself.
_FTS5_SPECIAL_CHARS = '+{}():"^@/#&|~[]<>,;!?$=\\\''
_FTS5_SPECIAL_RE = re.compile(f"[{re.escape(_FTS5_SPECIAL_CHARS)}]")

_OPERATORS = {"AND", "OR", "NOT"}


# ---- query shaping --------------------------------------------------------


def sanitize(query: str) -> str:
    """Make user text safe to hand to MATCH, preserving intent.

    Balanced quoted phrases survive. Unquoted hyphenated and dotted terms are
    quoted, because FTS5's tokenizer splits on both — `chat-send` would
    otherwise mean `chat AND send`, and `P2.2` would mean `p2 AND 2`.
    """
    query = (query or "")[:MAX_QUERY_CHARS]

    # Step 1 — protect balanced quoted phrases. A linear scan rather than a
    # regex, so a pathological run of quotes cannot induce backtracking.
    quoted: list[str] = []
    pieces: list[str] = []
    position = 0
    while position < len(query):
        character = query[position]
        if character != '"':
            pieces.append(character)
            position += 1
            continue
        end = query.find('"', position + 1)
        if end == -1:
            # An unmatched quote is not an instruction; drop it.
            pieces.append(" ")
            position += 1
            continue
        quoted.append(query[position:end + 1])
        pieces.append(f"\x00Q{len(quoted) - 1}\x00")
        position = end + 1
    sanitized = "".join(pieces)

    # Step 2 — strip what the grammar rejects. `:` matters most: with a
    # multi-column table an unquoted `TODO: fix` parses as a column filter and
    # raises "no such column".
    sanitized = _FTS5_SPECIAL_RE.sub(" ", sanitized)
    if "%" in sanitized and not contains_cjk(sanitized):
        # Only the CJK LIKE route needs `%` kept. Everywhere else `50%` would
        # sail into MATCH and raise like the rest.
        sanitized = sanitized.replace("%", " ")

    # Step 3 — collapse wildcard runs; a leading `*` is not a valid prefix.
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

    # Step 4 — a dangling boolean operator is a syntax error.
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

    # Step 5 — quote dotted/hyphenated/underscored terms. One pass, because
    # applying the three patterns in sequence double-quotes `my-app.config`.
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

    # Step 6 — put the preserved phrases back.
    for number, phrase in enumerate(quoted):
        sanitized = sanitized.replace(f"\x00Q{number}\x00", phrase)

    return sanitized.strip()


def _is_cjk(code_point: int) -> bool:
    return (
        0x4E00 <= code_point <= 0x9FFF       # CJK Unified Ideographs
        or 0x3400 <= code_point <= 0x4DBF    # Extension A
        or 0x20000 <= code_point <= 0x2A6DF  # Extension B
        or 0x3000 <= code_point <= 0x303F    # CJK symbols
        or 0x3040 <= code_point <= 0x309F    # Hiragana
        or 0x30A0 <= code_point <= 0x30FF    # Katakana
        or 0xAC00 <= code_point <= 0xD7AF    # Hangul syllables
    )


def contains_cjk(text: str) -> bool:
    return any(_is_cjk(ord(character)) for character in text)


def trigram_eligible(query: str) -> bool:
    """Whether every searchable token is long enough for the trigram index.

    The tokenizer indexes overlapping 3-character sequences, so a token
    shorter than three characters produces no trigrams and can never match.
    FTS5 ANDs tokens implicitly, so one short token makes the whole query
    return nothing — the route is only worth taking when all of them qualify.
    """
    tokens = [
        token
        for token in query.strip('"').strip().split()
        if token.upper() not in _OPERATORS
    ]
    return bool(tokens) and all(len(token) >= 3 for token in tokens)


def escape_like(text: str) -> str:
    """Escape LIKE wildcards so operator text matches literally.

    Pair with ``ESCAPE '\\'``. `_` in particular is common in the values these
    run against — tool names, paths, branch names — and a match documented as
    "contains" must not silently widen.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- shapes ---------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """Narrowing that applies to every route identically."""

    workspace: str = ""
    model: str = ""
    provider: str = ""
    since: float = 0.0
    until: float = 0.0
    roles: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()

    def clauses(self, alias: str = "s") -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if self.workspace:
            where.append(f"{alias}.workspace LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(self.workspace)}%")
        if self.model:
            where.append(f"{alias}.model LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(self.model)}%")
        if self.provider:
            where.append(f"{alias}.provider = ?")
            params.append(self.provider)
        if self.since:
            where.append(f"{alias}.updated_at >= ?")
            params.append(self.since)
        if self.until:
            where.append(f"{alias}.updated_at <= ?")
            params.append(self.until)
        if self.session_ids:
            marks = ",".join("?" for _ in self.session_ids)
            where.append(f"{alias}.id IN ({marks})")
            params.extend(self.session_ids)
        return where, params


@dataclass
class Hit:
    session_id: str
    # The value to hand back to `anchored()`. Stable across compaction.
    anchor: int
    position: int
    role: str
    tool_name: str
    snippet: str
    content: str
    title: str
    updated_at: float
    workspace: str = ""
    model: str = ""
    route: str = ""
    # True when this text was compacted out of the live conversation. The model
    # is told, because "I read this earlier" and "this is in my context right
    # now" are different claims.
    archived: bool = False


@dataclass
class SessionRow:
    id: str
    title: str
    created_at: float
    updated_at: float
    turns: int
    message_count: int
    provider: str = ""
    model: str = ""
    workspace: str = ""
    live: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _session_row(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id=row["id"],
        title=row["title"],
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        turns=int(row["turns"] or 0),
        message_count=int(row["message_count"] or 0),
        provider=row["provider"],
        model=row["model"],
        workspace=row["workspace"],
    )


# ---- the routes -----------------------------------------------------------

# `m.id` is the anchor callers pass back to `anchored()`. It is a global
# autoincrement, so it stays unique after compaction restarts a transcript's
# positions — which `position` does not.
_SELECT = (
    "SELECT m.id, m.session_id, m.position, m.role, m.tool_name, m.content, "
    "m.archived, s.title, s.updated_at, s.workspace, s.model"
)


def _role_clause(roles: tuple[str, ...]) -> tuple[str, list[Any]]:
    if not roles:
        return "", []
    marks = ",".join("?" for _ in roles)
    return f" AND m.role IN ({marks})", list(roles)


def _excerpt(content: str, query: str, width: int = 160) -> str:
    """The line the match is on, for routes that have no `snippet()`."""
    needle = query.strip().lower()
    text = content or ""
    for line in text.splitlines():
        if needle and needle in line.lower():
            return line.strip()[:width]
    return " ".join(text.split())[:width]


def _hits(rows: list[sqlite3.Row], route: str, query: str) -> list[Hit]:
    out: list[Hit] = []
    for row in rows:
        keys = row.keys()
        snippet = row["snippet"] if "snippet" in keys else ""
        out.append(
            Hit(
                session_id=row["session_id"],
                anchor=int(row["id"]),
                position=int(row["position"]),
                role=row["role"],
                tool_name=row["tool_name"],
                snippet=snippet or _excerpt(row["content"], query),
                content=row["content"],
                title=row["title"],
                updated_at=float(row["updated_at"] or 0),
                workspace=row["workspace"],
                model=row["model"],
                route=route,
                archived=bool(row["archived"]),
            )
        )
    return out


def _fts_search(
    conn: sqlite3.Connection,
    table: str,
    match: str,
    *,
    filters: Filters,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    where, params = filters.clauses()
    role_sql, role_params = _role_clause(filters.roles)
    clause = ("".join(f" AND {piece}" for piece in where)) + role_sql
    sql = (
        f"{_SELECT}, snippet({table}, 0, '»', '«', '…', 14) AS snippet "
        f"FROM {table} "
        f"JOIN messages m ON m.id = {table}.rowid "
        f"JOIN sessions s ON s.id = m.session_id "
        f"WHERE {table} MATCH ?{clause} "
        f"ORDER BY bm25({table}), s.updated_at DESC "
        f"LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, (match, *params, *role_params, limit, offset)).fetchall()


def _like_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    filters: Filters,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    """Substring search, ANDed across whitespace-separated terms.

    Deliberately not a single `%query%`: a person typing two words expects
    both to be present, which is what every other route does.
    """
    terms = [term for term in query.split() if term.strip()]
    if not terms:
        return []
    where, params = filters.clauses()
    role_sql, role_params = _role_clause(filters.roles)
    term_sql = " AND ".join("m.content LIKE ? ESCAPE '\\'" for _ in terms)
    term_params = [f"%{escape_like(term)}%" for term in terms]
    clause = "".join(f" AND {piece}" for piece in where) + role_sql
    sql = (
        f"{_SELECT} "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        f"WHERE ({term_sql}){clause} "
        "ORDER BY s.updated_at DESC, m.id ASC "
        "LIMIT ? OFFSET ?"
    )
    return conn.execute(
        sql, (*term_params, *params, *role_params, limit, offset)
    ).fetchall()


def route_for(conn: sqlite3.Connection, query: str) -> str:
    """Which index this query will actually use.

    Reported by `sessions doctor`, so a surprising result set can be explained
    rather than guessed at.
    """
    if not db_module.has_fts(conn):
        return "like"
    if contains_cjk(query):
        if db_module.has_trigram(conn) and trigram_eligible(query):
            return "trigram"
        return "like"
    return "fts" if sanitize(query) else "like"


def search(
    query: str,
    *,
    filters: Filters | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Hit]:
    query = (query or "").strip()
    if not query:
        return []
    filters = filters or Filters()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        route = route_for(conn, query)
        try:
            if route == "fts":
                rows = _fts_search(
                    conn,
                    "messages_fts",
                    sanitize(query),
                    filters=filters,
                    limit=limit,
                    offset=offset,
                )
                return _hits(rows, "fts", query)
            if route == "trigram":
                rows = _fts_search(
                    conn,
                    "messages_trigram",
                    sanitize(query) or query,
                    filters=filters,
                    limit=limit,
                    offset=offset,
                )
                return _hits(rows, "trigram", query)
        except sqlite3.Error:
            # Fall through rather than report an empty result: a grammar the
            # sanitizer did not anticipate must degrade to a slower search,
            # not to "nothing found".
            pass
        return _hits(
            _like_search(conn, query, filters=filters, limit=limit, offset=offset),
            "like",
            query,
        )


# ---- listing and reading --------------------------------------------------


def recent(limit: int = 20, filters: Filters | None = None) -> list[SessionRow]:
    filters = filters or Filters()
    where, params = filters.clauses()
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, turns, message_count, "
            f"provider, model, workspace FROM sessions s{clause} "
            "ORDER BY updated_at DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [_session_row(row) for row in rows]


def by_title(query: str, limit: int = 20) -> list[SessionRow]:
    """Sessions whose opening line contains this text.

    Separate from message search on purpose: "the session about the retry
    budget" is a different question from "sessions mentioning retry budget",
    and answering the first with the second buries it.
    """
    query = (query or "").strip()
    if not query:
        return []
    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, turns, message_count, "
            "provider, model, workspace FROM sessions "
            "WHERE title LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT ?",
            (f"%{escape_like(query)}%", max(1, int(limit))),
        ).fetchall()
        return [_session_row(row) for row in rows]


def get_session(session_id: str) -> SessionRow | None:
    with db_module.connect_quietly() as conn:
        if conn is None:
            return None
        row = conn.execute(
            "SELECT id, title, created_at, updated_at, turns, message_count, "
            "provider, model, workspace FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return _session_row(row) if row else None


def resolve_prefix(prefix: str) -> list[str]:
    """Session ids starting with this, the way git accepts a short sha."""
    prefix = (prefix or "").strip().lower()
    if not prefix:
        return []
    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT 25",
            (f"{escape_like(prefix)}%",),
        ).fetchall()
        return [row["id"] for row in rows]


def anchored(
    session_id: str,
    anchor: int,
    *,
    window: int = 4,
    bookend: int = 2,
    roles: tuple[str, ...] = ("user", "assistant"),
) -> dict[str, Any]:
    """A hit in context, plus the session's opening and closing exchanges.

    The bookends are the point. A match two hundred messages into a long
    session tells you what was said and nothing about what it was *for*; the
    opening says what was asked and the closing says how it ended, and all
    three arrive in one call instead of loading the whole transcript.

    `anchor` is a row id, not a transcript offset. Compaction restarts a
    session's offsets, so an offset would silently address a different message
    after one; ids are global and monotonic, which also makes them the right
    thing to order a mixed archived/live transcript by.

    The anchor is always kept, whatever its role — a filter that drops the
    message you asked to see is not a filter.
    """
    empty = {
        "window": [],
        "before": 0,
        "after": 0,
        "opening": [],
        "closing": [],
        "archived": 0,
    }
    with db_module.connect_quietly() as conn:
        if conn is None:
            return empty

        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, position, role, content, tool_name, archived "
                "FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        ]
        if not rows:
            return empty

        at = next((i for i, row in enumerate(rows) if row["id"] == anchor), -1)
        if at < 0:
            return empty

        low, high = max(0, at - window), min(len(rows), at + window + 1)
        slice_ = rows[low:high]
        if roles:
            keep = set(roles)
            slice_ = [
                row for row in slice_ if row["id"] == anchor or row["role"] in keep
            ]

        # Counted over the whole session, not the filtered slice: "12 earlier
        # messages" has to mean what is there, or a reader concludes they have
        # seen the start when they have not.
        before, after = low, len(rows) - high

        prose = [row for row in rows if row["content"].strip()]
        if roles:
            prose = [row for row in prose if row["role"] in set(roles)]
        head = [row for row in prose if row["id"] < slice_[0]["id"]] if slice_ else []
        tail = [row for row in prose if row["id"] > slice_[-1]["id"]] if slice_ else []

        return {
            "window": slice_,
            "before": before,
            "after": after,
            "opening": head[:bookend] if bookend else [],
            "closing": tail[-bookend:] if bookend else [],
            "archived": sum(1 for row in rows if row["archived"]),
        }


def transcript(
    session_id: str, limit: int = 400, include_archived: bool = True
) -> list[dict[str, Any]]:
    """Every indexed message of one session, oldest first.

    Ordered by `id` rather than `position`: compaction restarts positions, so
    ordering by them interleaves turns that were folded away with the ones that
    replaced them. Insertion order is chronological across every compaction.
    """
    clause = "" if include_archived else " AND archived = 0"
    with db_module.connect_quietly() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id, position, role, content, tool_name, archived FROM messages "
            f"WHERE session_id = ?{clause} ORDER BY id ASC LIMIT ?",
            (session_id, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]


def counts() -> dict[str, int]:
    with db_module.connect_quietly() as conn:
        if conn is None:
            return {"sessions": 0, "messages": 0}
        return {
            "sessions": conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()["n"],
            "messages": conn.execute(
                "SELECT COUNT(*) AS n FROM messages"
            ).fetchone()["n"],
            "archived": conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE archived = 1"
            ).fetchone()["n"],
        }
