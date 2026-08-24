"""Turning a session into something you can keep.

Four formats, because they answer different questions: `markdown` to paste
into a document, `html` to send to somebody who will open it in a browser,
`jsonl` to pipe into another program, and `text` to read in a pager.

**Everything is escaped, including in Markdown.** A transcript contains
whatever anybody pasted into it, and one of the things people paste is HTML. A
session export opened in a browser is a local file with the same privileges as
any other, so an unescaped `<script>` from a tool result is a real hole and not
a theoretical one.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any, Iterable

from .. import sessions as sessions_store

FORMATS = ("markdown", "html", "jsonl", "text")

# Roles that carry the conversation. `system` is excluded by default for the
# same reason it is not indexed: it is the skills manifest and every standing
# memory, which is neither what happened nor something to hand to anyone else.
DEFAULT_ROLES = ("user", "assistant", "tool")

TOOL_PREVIEW_LINES = 12


def normalize(value: str) -> str:
    fmt = (value or "markdown").strip().lower()
    if fmt == "md":
        fmt = "markdown"
    if fmt == "txt":
        fmt = "text"
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format {value!r}. One of: {', '.join(FORMATS)}")
    return fmt


def _stamp(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def _visible(
    session: "sessions_store.Session", roles: Iterable[str]
) -> list[dict[str, Any]]:
    keep = set(roles)
    out = []
    for message in session.messages:
        if not isinstance(message, dict) or message.get("role") not in keep:
            continue
        out.append(message)
    return out


def _clip(text: str, lines: int) -> str:
    rows = text.splitlines()
    if len(rows) <= lines:
        return text
    return "\n".join(rows[:lines] + [f"… {len(rows) - lines} more lines"])


# ---- markdown -------------------------------------------------------------


def render_markdown(
    session: "sessions_store.Session", roles: Iterable[str] = DEFAULT_ROLES
) -> str:
    lines = [
        f"# {session.title}",
        "",
        f"- session: `{session.id}`",
        f"- model: `{session.model or 'unknown'}`",
        f"- workspace: `{session.workspace or 'unknown'}`",
        f"- started: {_stamp(session.created_at)}",
        f"- last activity: {_stamp(session.updated_at)}",
        f"- turns: {session.turns}",
        "",
    ]
    for message in _visible(session, roles):
        role = str(message.get("role"))
        body = _text(message.get("content")).strip()
        if role == "user":
            lines += ["## You", "", body, ""]
        elif role == "assistant":
            if body:
                lines += ["## Andromeda", "", body, ""]
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = function.get("name") or call.get("name") or "tool"
                arguments = function.get("arguments") or call.get("arguments") or ""
                lines += [
                    f"### → {name}",
                    "",
                    "```json",
                    _clip(str(arguments), TOOL_PREVIEW_LINES),
                    "```",
                    "",
                ]
        elif role == "tool":
            name = message.get("name") or message.get("tool_name") or "tool"
            lines += [
                f"### ← {name}",
                "",
                "```",
                _clip(body, TOOL_PREVIEW_LINES),
                "```",
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


# ---- text -----------------------------------------------------------------


def render_text(
    session: "sessions_store.Session", roles: Iterable[str] = DEFAULT_ROLES
) -> str:
    lines = [f"{session.id}  {session.model}  {_stamp(session.updated_at)}", ""]
    for message in _visible(session, roles):
        role = str(message.get("role"))
        body = _text(message.get("content")).strip()
        if not body:
            continue
        marker = {"user": "›", "assistant": " ", "tool": "⚙"}.get(role, " ")
        lines.append(f"{marker} {body}" if marker.strip() else body)
        lines.append("")
    return "\n".join(lines)


# ---- html -----------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --bg:#fbfbfa; --fg:#1c1c1a; --dim:#6b6b66;
        --rule:#e2e2dd; --user:#0b5fa5; --tool:#f2f2ee; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141414; --fg:#e8e8e4; --dim:#8f8f88; --rule:#2c2c2a;
          --user:#7fb2e5; --tool:#1d1d1c; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2.5rem 1.25rem; background:var(--bg); color:var(--fg);
       font: 15px/1.65 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif; }
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .5rem; line-height:1.3; }
.meta { color:var(--dim); font-size:.82rem; margin-bottom:2rem;
        border-bottom:1px solid var(--rule); padding-bottom:1rem; }
.meta span { margin-right:1rem; white-space:nowrap; }
.turn { margin: 0 0 1.75rem; }
.who { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;
       color:var(--dim); margin-bottom:.35rem; }
.user .body { color:var(--user); font-weight:600; }
.body { white-space: pre-wrap; overflow-wrap:anywhere; }
details { background:var(--tool); border:1px solid var(--rule); border-radius:6px;
          padding:.5rem .75rem; margin:.5rem 0; font-size:.86rem; }
summary { cursor:pointer; color:var(--dim); }
pre { margin:.5rem 0 0; white-space:pre-wrap; overflow-wrap:anywhere;
      font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
footer { color:var(--dim); font-size:.75rem; margin-top:3rem;
         border-top:1px solid var(--rule); padding-top:1rem; }
"""


def render_html(
    session: "sessions_store.Session", roles: Iterable[str] = DEFAULT_ROLES
) -> str:
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(session.title)}</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        f"<h1>{escape(session.title)}</h1>",
        '<p class="meta">'
        f"<span>{escape(session.id)}</span>"
        f"<span>{escape(session.model or 'unknown model')}</span>"
        f"<span>{escape(_stamp(session.updated_at))}</span>"
        f"<span>{session.turns} turns</span>"
        f"<span>{escape(session.workspace)}</span>"
        "</p>",
    ]
    for message in _visible(session, roles):
        role = str(message.get("role"))
        body = _text(message.get("content")).strip()
        if role == "user":
            parts.append(
                f'<div class="turn user"><div class="who">You</div>'
                f'<div class="body">{escape(body)}</div></div>'
            )
        elif role == "assistant":
            if body:
                parts.append(
                    f'<div class="turn"><div class="who">Andromeda</div>'
                    f'<div class="body">{escape(body)}</div></div>'
                )
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or call.get("name") or "tool")
                arguments = str(function.get("arguments") or call.get("arguments") or "")
                parts.append(
                    f"<details><summary>→ {escape(name)}</summary>"
                    f"<pre>{escape(_clip(arguments, TOOL_PREVIEW_LINES))}</pre></details>"
                )
        elif role == "tool":
            name = str(message.get("name") or message.get("tool_name") or "tool")
            parts.append(
                f"<details><summary>← {escape(name)}</summary>"
                f"<pre>{escape(_clip(body, TOOL_PREVIEW_LINES))}</pre></details>"
            )
    parts.append(
        "<footer>Exported from Andromeda · "
        f"{escape(datetime.now().isoformat(timespec='seconds'))}</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"


# ---- jsonl ----------------------------------------------------------------


def render_jsonl(
    sessions: Iterable["sessions_store.Session"],
    roles: Iterable[str] = DEFAULT_ROLES,
    *,
    prompts_only: bool = False,
) -> str:
    """One record per line.

    `prompts_only` changes the unit from a session to a prompt, which is what
    makes the output useful to pipe into review or prompt-library tooling —
    the shape somebody actually wants is rarely "the whole session, again".
    """
    lines: list[str] = []
    for session in sessions:
        if prompts_only:
            for message in session.messages:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                lines.append(
                    json.dumps(
                        {
                            "session": session.id,
                            "at": session.updated_at,
                            "workspace": session.workspace,
                            "prompt": _text(message.get("content")),
                        },
                        ensure_ascii=False,
                    )
                )
            continue
        payload = session.to_json()
        payload["messages"] = _visible(session, roles)
        payload["title"] = session.title
        lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def render(
    session: "sessions_store.Session",
    fmt: str = "markdown",
    roles: Iterable[str] = DEFAULT_ROLES,
) -> str:
    chosen = normalize(fmt)
    if chosen == "markdown":
        return render_markdown(session, roles)
    if chosen == "html":
        return render_html(session, roles)
    if chosen == "text":
        return render_text(session, roles)
    return render_jsonl([session], roles)


def suffix(fmt: str) -> str:
    return {
        "markdown": ".md",
        "html": ".html",
        "jsonl": ".jsonl",
        "text": ".txt",
    }[normalize(fmt)]
