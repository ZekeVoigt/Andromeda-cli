"""Finding a plugin by name instead of by URL.

`andromeda plugins install owner/repo` already works. This exists so that
`andromeda plugins install weather` can too, and so that `plugins search` has
something to search.

The index is one static JSON file at a canonical URL. There is no server, no
account and no upload path — an entry lands in it by someone opening a pull
request against the index repository, which is the whole of the curation
model and is stated here so nobody mistakes it for more.

    remote  ──▶  cache (24h)  ──▶  bundled seed
       │             │                  │
    fetched      still used         offline, and the
    when the     when the           format reference
    cache is     network is
    stale        gone

**Indexed is not audited.** Inclusion means an entry's *metadata* was reviewed
— that the name is not a typosquat of another entry, that the repository is
the one the description claims. It is not a review of the code, which changes
after the review anyway. Install still runs the scan and still asks for
capability consent, and the listing says so on every screen that shows it.

Every entry pins an immutable ref. A tag can be moved and a branch head moves
by definition, so an index that resolved to either would be an index that
resolves to different code tomorrow with the same words in it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INDEX_URL = "https://ai-andromeda.com/plugins/index.json"

CACHE_FILENAME = "plugin-index.json"
CACHE_TTL_SECONDS = 24 * 3600

#: Refuse an absurd payload rather than reading it into memory to find out.
MAX_INDEX_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 10.0

#: A 40-character commit SHA. Tags and branches are refused by name, so the
#: refusal can say which entry and why rather than failing at clone time.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

SECURITY_NOTE = (
    "Indexed is not audited: an entry's metadata was reviewed, not its code. "
    "The scan and the capability prompt still run on install."
)


@dataclass(frozen=True)
class IndexEntry:
    name: str
    repo: str
    ref: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    capabilities: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def matches(self, query: str) -> bool:
        """Substring, across the fields a person would search by."""
        needle = query.strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            [self.name, self.description, self.author, " ".join(self.tags)]
        ).lower()
        return needle in haystack


def seed_path() -> Path:
    """The bundled seed: the offline fallback and the format reference."""
    return Path(__file__).resolve().parent / "data" / "plugin_index.json"


def cache_path() -> Path:
    from andromeda_cli import config as config_module

    return config_module.home() / "cache" / CACHE_FILENAME


def index_url() -> str:
    from andromeda_cli import config as config_module

    try:
        configured = str(config_module.load().get("plugin_index_url") or "").strip()
    except Exception:  # noqa: BLE001 - a broken config is not a reason to fail here
        configured = ""
    return configured or DEFAULT_INDEX_URL


def parse(raw: Any) -> list[IndexEntry]:
    """Turn index JSON into entries, dropping the ones that are not usable.

    An entry with no name, no repo, or a ref that is not a 40-character SHA is
    dropped with a warning rather than raising. One malformed row in a
    community file must not take the whole index away from everybody.
    """
    if isinstance(raw, dict):
        rows = raw.get("plugins")
    else:
        rows = raw
    if not isinstance(rows, list):
        return []

    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        repo = str(row.get("repo") or "").strip()
        ref = str(row.get("ref") or "").strip().lower()
        if not NAME_RE.match(name):
            logger.warning("index entry %r has an unusable name; skipping", name)
            continue
        if not repo:
            logger.warning("index entry %r names no repository; skipping", name)
            continue
        if not SHA_RE.match(ref):
            logger.warning(
                "index entry %r pins %r, which is not a 40-character commit "
                "SHA; skipping. A tag can be moved and a branch head moves by "
                "definition.",
                name,
                ref,
            )
            continue
        if name in seen:
            # First wins. A duplicate name in a community index is either a
            # mistake or a typosquat, and picking the later one silently is the
            # worse answer to both.
            logger.warning("index lists %r more than once; keeping the first", name)
            continue
        seen.add(name)
        entries.append(
            IndexEntry(
                name=name,
                repo=repo,
                ref=ref,
                description=str(row.get("description") or "").strip(),
                author=str(row.get("author") or "").strip(),
                homepage=str(row.get("homepage") or "").strip(),
                capabilities=tuple(
                    str(item) for item in (row.get("capabilities") or []) if item
                ),
                tags=tuple(str(item) for item in (row.get("tags") or []) if item),
            )
        )
    return entries


def _read(path: Path) -> list[IndexEntry]:
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _cache_age() -> float | None:
    try:
        return time.time() - cache_path().stat().st_mtime
    except OSError:
        return None


def fetch(*, force: bool = False) -> tuple[list[IndexEntry], str]:
    """The index, and where it came from.

    The source is returned rather than logged because it changes what the
    listing means: entries from a stale cache may name versions that have since
    been yanked, and entries from the seed are whatever shipped with this
    install.
    """
    age = _cache_age()
    if not force and age is not None and age < CACHE_TTL_SECONDS:
        cached = _read(cache_path())
        if cached:
            return cached, "cache"

    fetched = _download()
    if fetched:
        _write_cache(fetched)
        return parse(fetched), "network"

    # A stale cache beats the seed: it is at least this install's own view of
    # the index at some point, while the seed is whatever was true at release.
    cached = _read(cache_path())
    if cached:
        return cached, "stale cache"

    return _read(seed_path()), "bundled"


def _download() -> Any:
    import httpx

    url = index_url()
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            if len(response.content) > MAX_INDEX_BYTES:
                logger.warning(
                    "the plugin index at %s is over %d bytes; ignoring it",
                    url,
                    MAX_INDEX_BYTES,
                )
                return None
            return response.json()
    except Exception as exc:  # noqa: BLE001 - offline is an ordinary state
        logger.debug("could not fetch the plugin index from %s: %s", url, exc)
        return None


def _write_cache(payload: Any) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("could not cache the plugin index: %s", exc)


def search(query: str, *, limit: int = 20) -> tuple[list[IndexEntry], str]:
    entries, source = fetch()
    matched = [entry for entry in entries if entry.matches(query)]
    matched.sort(key=lambda entry: (query.strip().lower() not in entry.name, entry.name))
    return matched[:limit], source


def resolve(name: str) -> IndexEntry | None:
    """One entry by exact name."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    entries, _source = fetch()
    for entry in entries:
        if entry.name == wanted:
            return entry
    return None


def looks_like_bare_name(identifier: str) -> bool:
    """Whether this is an index name rather than a repo, URL or path.

    Conservative on purpose. Anything with a slash, a scheme, a dot at the
    front or a `~` is a location the user gave us, and treating it as a name to
    look up would turn a typo in a path into a request to a remote index.
    """
    value = (identifier or "").strip()
    if not value or "/" in value or "\\" in value:
        return False
    if "://" in value or value.startswith((".", "~", "-")):
        return False
    return bool(NAME_RE.match(value.lower()))
