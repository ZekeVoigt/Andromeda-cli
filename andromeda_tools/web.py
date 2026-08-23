"""Reaching the network.

`web_fetch` and `web_search` mirror the TypeScript registry's names, schemas and
`safe_local` tier. Both are read-only by construction — nothing here can change
anything anywhere.

HTML is reduced to text with the standard library rather than a parsing
dependency. The extraction is crude on purpose: the goal is readable prose for a
model, not a faithful DOM, and every added dependency is blast radius on the
next supply-chain incident.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from .spec import ToolResult, failure

TIMEOUT = httpx.Timeout(20.0)
MAX_BYTES = 2_000_000
MAX_TEXT = 60_000
DEFAULT_RESULTS = 5
USER_AGENT = "Andromeda-CLI/0.1 (+https://ai-andromeda.com)"

# Blocks that end a line of prose when they close.
BLOCK_TAGS = frozenset(
    "p div br li tr h1 h2 h3 h4 h5 h6 section article header footer blockquote pre".split()
)
DROP_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "head"})

BLANK_LINES = re.compile(r"\n{3,}")
SPACES = re.compile(r"[ \t]{2,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())
            self.parts.append(" ")

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = SPACES.sub(" ", joined)
        return BLANK_LINES.sub("\n\n", joined).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML is the normal case
        pass
    return parser.text() or unescape(re.sub(r"<[^>]+>", " ", html)).strip()


def _is_private(host: str) -> bool:
    """Whether a hostname resolves anywhere on this machine or its network.

    The URL comes from the model, and this process sits inside the user's own
    network with whatever their laptop can reach — a metadata endpoint, a
    router admin page, a service bound to localhost. A read tool that will
    fetch any address it is handed is a way to read those and put the response
    into the transcript. Resolved rather than pattern-matched, because
    `http://127.0.0.1.nip.io/` is not a literal loopback address.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        # Unresolvable: let the request fail on its own terms rather than
        # reporting a security refusal for a typo.
        return False

    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_reserved
            or parsed.is_multicast
            or parsed.is_unspecified
        ):
            return True
    return False


def fetch(url: str, allow_private: bool = False) -> ToolResult:
    """Fetch a URL as text.

    `allow_private` is a session setting, never a tool argument: the URL comes
    from the model, and a guard the model can turn off is not a guard. Someone
    working on a local server sets `allow_private_network` once, deliberately,
    and knows what they have opened.
    """
    url = (url or "").strip()
    if not url:
        return failure("A URL is required.")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return failure(f"Only http and https URLs can be fetched — got {parsed.scheme or 'no'} scheme.")
    if not parsed.hostname:
        return failure(f"{url!r} has no host.")
    if not allow_private and _is_private(parsed.hostname):
        return failure(
            f"{parsed.hostname} is on a private or local network. "
            "web_fetch only reaches the public internet — set "
            "`allow_private_network` to change that."
        )

    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return failure(f"Could not fetch {url}: {exc}")

    # Redirects are followed, so the host that actually answered is re-checked:
    # a public URL that 302s to 169.254.169.254 would otherwise walk straight
    # through the guard above.
    final_host = response.url.host
    if not allow_private and final_host and _is_private(final_host):
        return failure(f"{url} redirected to {final_host}, which is on a private network.")

    if response.status_code >= 400:
        return failure(f"{url} returned HTTP {response.status_code}.")

    content_type = response.headers.get("content-type", "")
    body = response.content[:MAX_BYTES]

    if "html" in content_type:
        text = html_to_text(body.decode(response.encoding or "utf-8", errors="replace"))
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        text = body.decode(response.encoding or "utf-8", errors="replace")
    else:
        return failure(f"{url} is {content_type or 'an unknown type'}, not text.")

    truncated = len(text) > MAX_TEXT
    if truncated:
        text = text[:MAX_TEXT] + "\n\n… truncated."

    return ToolResult(
        content=text or "(the page had no readable text)",
        display=f"{url} — {len(text):,} chars",
        metadata={"status": response.status_code, "truncated": truncated},
    )


# ---- search ---------------------------------------------------------------
#
# Search needs a provider, and every provider needs a key. Rather than pick one
# and hide the dependency, the backend is configured and the tool is simply not
# registered when none is — so the model is never told about a capability that
# can only ever answer "not configured".

PROVIDERS = {
    "brave": {
        "env": "BRAVE_SEARCH_API_KEY",
        "url": "https://api.search.brave.com/res/v1/web/search",
    },
    "tavily": {
        "env": "TAVILY_API_KEY",
        "url": "https://api.tavily.com/search",
    },
}


def configured_provider(env: dict[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    for name, spec in PROVIDERS.items():
        if source.get(spec["env"], "").strip():
            return name
    return None


def search(query: str, limit: int = DEFAULT_RESULTS) -> ToolResult:
    query = (query or "").strip()
    if not query:
        return failure("A search needs a query.")

    limit = max(1, min(int(limit or DEFAULT_RESULTS), 20))
    provider = configured_provider()
    if provider is None:
        keys = ", ".join(spec["env"] for spec in PROVIDERS.values())
        return failure(f"No search provider is configured. Set one of: {keys}")

    try:
        results = _brave(query, limit) if provider == "brave" else _tavily(query, limit)
    except httpx.HTTPError as exc:
        return failure(f"The search request failed: {exc}")

    if not results:
        return ToolResult(content=f"No results for {query!r}.", display="no results")

    lines = [f"{item['title']}\n  {item['url']}\n  {item['snippet']}" for item in results]
    return ToolResult(
        content="\n\n".join(lines),
        display=f"{len(results)} result{'s' if len(results) != 1 else ''} for {query!r}",
        metadata={"provider": provider, "count": len(results)},
    )


def _brave(query: str, limit: int) -> list[dict[str, str]]:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(
            PROVIDERS["brave"]["url"],
            params={"q": query, "count": limit},
            headers={
                "X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"].strip(),
                "Accept": "application/json",
            },
        )
    response.raise_for_status()
    payload = response.json()
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": html_to_text(str(item.get("description") or ""))[:300],
        }
        for item in (payload.get("web", {}).get("results") or [])[:limit]
    ]


def _tavily(query: str, limit: int) -> list[dict[str, str]]:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            PROVIDERS["tavily"]["url"],
            json={
                "api_key": os.environ["TAVILY_API_KEY"].strip(),
                "query": query,
                "max_results": limit,
            },
        )
    response.raise_for_status()
    payload = response.json()
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or "")[:300],
        }
        for item in (payload.get("results") or [])[:limit]
    ]
