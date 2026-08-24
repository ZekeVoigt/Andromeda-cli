"""Turning what someone typed into a filter.

`--since 7d`, `--since yesterday`, `--since 2026-08-01` all mean a moment in
time, and a person switching between them should not have to think about which
one this program accepts.

**An unparseable value is an error, never "no filter".** Silently ignoring
`--since lastweek` returns every session ever recorded and looks like a
successful search over a wider range than was asked for, which is the failure
you do not notice.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .queries import Filters

_RELATIVE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


class FilterError(ValueError):
    pass


def parse_when(value: str, *, now: float | None = None) -> float:
    """A point in time, as a unix timestamp."""
    text = (value or "").strip().lower()
    if not text:
        raise FilterError("Give a time: 7d, yesterday, or 2026-08-01.")
    moment = time.time() if now is None else now

    if text in {"now", "today"}:
        # "today" means the start of it, not this instant — otherwise
        # `--since today` returns nothing, which is never what was meant.
        if text == "now":
            return moment
        start = datetime.fromtimestamp(moment).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start.timestamp()
    if text == "yesterday":
        start = datetime.fromtimestamp(moment).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
        return start.timestamp()

    relative = _RELATIVE.match(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        return moment - amount * _UNITS[unit]

    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).timestamp()
        except ValueError:
            continue

    raise FilterError(
        f"Could not read {value!r} as a time. Try 7d, yesterday, or 2026-08-01."
    )


def build(
    *,
    workspace: str = "",
    model: str = "",
    provider: str = "",
    since: str = "",
    until: str = "",
    role: str = "",
    now: float | None = None,
) -> Filters:
    roles = tuple(
        part.strip() for part in (role or "").split(",") if part.strip()
    )
    return Filters(
        workspace=(workspace or "").strip(),
        model=(model or "").strip(),
        provider=(provider or "").strip(),
        since=parse_when(since, now=now) if since else 0.0,
        until=parse_when(until, now=now) if until else 0.0,
        roles=roles,
    )


def describe(filters: Filters) -> str:
    """What is actually being narrowed, so a thin result set explains itself."""
    parts: list[str] = []
    if filters.workspace:
        parts.append(f"workspace~{filters.workspace}")
    if filters.model:
        parts.append(f"model~{filters.model}")
    if filters.provider:
        parts.append(f"provider={filters.provider}")
    if filters.since:
        parts.append(f"since {datetime.fromtimestamp(filters.since):%Y-%m-%d %H:%M}")
    if filters.until:
        parts.append(f"until {datetime.fromtimestamp(filters.until):%Y-%m-%d %H:%M}")
    if filters.roles:
        parts.append("roles " + ",".join(filters.roles))
    return " · ".join(parts)
