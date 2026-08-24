"""Session state: the index, the search over it, and the repairs for both.

The transcripts under `sessions/` remain the source of truth. Everything here
is derived from them and can be rebuilt — see `db.py` for why that is the
whole design rather than an implementation detail.
"""

from .db import capabilities, connect, connect_quietly, db_path, migrate
from .export import render as render_export
from .filters import FilterError, build as build_filters, parse_when
from .index import (
    archive_range,
    archived_count,
    forget_session,
    index_session,
    reindex,
    stale_count,
)
from .live import claim, held_by, release
from .recap import Recap, build as build_recap
from .recovery import Report, check, rebuild_index, repair, salvage
from .startup import Findings, check as startup_check
from .queries import (
    Filters,
    Hit,
    SessionRow,
    anchored,
    by_title,
    counts,
    get_session,
    recent,
    resolve_prefix,
    sanitize,
    search,
    transcript,
)

__all__ = [
    "FilterError",
    "Filters",
    "Findings",
    "Hit",
    "Recap",
    "Report",
    "SessionRow",
    "anchored",
    "archive_range",
    "archived_count",
    "build_filters",
    "build_recap",
    "by_title",
    "capabilities",
    "check",
    "claim",
    "connect",
    "connect_quietly",
    "counts",
    "db_path",
    "forget_session",
    "get_session",
    "held_by",
    "index_session",
    "migrate",
    "parse_when",
    "rebuild_index",
    "recent",
    "reindex",
    "release",
    "render_export",
    "repair",
    "resolve_prefix",
    "salvage",
    "sanitize",
    "search",
    "stale_count",
    "startup_check",
    "transcript",
]
