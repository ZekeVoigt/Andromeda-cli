"""Driving a real browser.

**Refs, never pixels.** Every read of a page is a structured outline of its
interactive elements, each carrying a short ref (`e1`, `e2`, …); every action
names a ref. There is no screenshot tool and there will not be one: reasoning
about a page from an image is slower, costs far more, and is wrong in ways that
are invisible — a model that "sees" a button at the wrong coordinates clicks
whatever is actually there. The desktop runtime's browser tool says the same
thing in its own description, and this follows it.

Naming: the granular `browser_*` family rather than the desktop's single
`browser` tool. That tool's contract is managed profiles, a CDP relay, tab ids
and show/hide — none of which exist here, so taking its name would be a lie the
drift guard could not catch. The `browser_` prefix is what the specialist belts
key on, so the family is recognised as one surface either way.

Playwright is a lazy dependency. Until it is installed these tools are not
registered at all, so the model is never offered a browser it cannot open.
"""

from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass, field
from typing import Any

from .spec import ToolResult, failure
from .web import _is_private  # the same guard the fetch tool uses

DEFAULT_TIMEOUT_MS = 20_000
NAVIGATION_TIMEOUT_MS = 30_000
MAX_SNAPSHOT_CHARS = 24_000
MAX_TEXT_CHARS = 40_000
REF_ATTRIBUTE = "data-andromeda-ref"

INSTALL_HINT = (
    "The browser tools need Playwright. Install it with:\n"
    "  andromeda browser install"
)

# Walks the DOM once, stamps a ref on every element a person could interact
# with, and returns a compact outline. Done in one evaluate() rather than a
# tree of Playwright queries: a page with 400 nodes is 400 round trips
# otherwise, and each one can race the page mutating under it.
SNAPSHOT_SCRIPT = """
(refAttribute) => {
  const INTERACTIVE = 'a,button,input,select,textarea,summary,[role],[onclick],[contenteditable=""],[contenteditable="true"],[tabindex]:not([tabindex="-1"])';
  const SKIP_ROLES = new Set(['presentation', 'none']);

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit && !SKIP_ROLES.has(explicit)) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    return 'generic';
  };

  // What the element IS. Deliberately never its current value: a filled field
  // whose label became its own contents cannot be identified in the next
  // snapshot. The value is reported separately, as state.
  const nameOf = (el) => {
    const labels = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('title'),
      el.getAttribute('alt'),
      el.getAttribute('name'),
      el.innerText,
    ];
    for (const candidate of labels) {
      const text = (candidate || '').replace(/\\s+/g, ' ').trim();
      if (text) return text.slice(0, 120);
    }
    return '';
  };

  // What the element currently HOLDS. Without this a model cannot see what it
  // just typed, so it types it again.
  //
  // NOT named `valueOf`: that is Object.prototype.valueOf, and a local of that
  // name is invoked by the engine during ordinary coercion, with no element.
  const currentValue = (el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password') return '';
    const raw = el.value;
    if (typeof raw !== 'string') return '';
    return raw.replace(/\\s+/g, ' ').trim().slice(0, 120);
  };

  document.querySelectorAll('[' + refAttribute + ']').forEach((el) => el.removeAttribute(refAttribute));

  const lines = [];
  let index = 0;
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (!visible(el)) continue;
    const role = roleOf(el);
    if (role === 'generic') continue;
    const name = nameOf(el);
    if (!name && role !== 'textbox') continue;

    index += 1;
    const ref = 'e' + index;
    el.setAttribute(refAttribute, ref);

    const extras = [];
    if (el.disabled) extras.push('disabled');
    if (el.checked) extras.push('checked');
    const value = currentValue(el);
    if (value) extras.push('value: "' + value + '"');
    if (el.getAttribute('href')) extras.push(el.getAttribute('href').slice(0, 80));
    lines.push(
      '[' + ref + '] ' + role + (name ? ' "' + name + '"' : '') +
      (extras.length ? ' (' + extras.join(', ') + ')' : '')
    );
  }

  const heading = document.querySelector('h1');
  return {
    title: document.title || '',
    url: location.href,
    heading: heading ? heading.innerText.replace(/\\s+/g, ' ').trim().slice(0, 200) : '',
    elements: lines,
    text: (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim(),
  };
}
"""


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class BrowserSession:
    """One headless Chromium, started on first use and closed at exit.

    Lazily started because most sessions never open a page, and a browser
    launched at import costs a second and ~150MB for nothing.
    """

    headless: bool = True
    _playwright: Any = field(default=None, repr=False)
    _browser: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def page(self):
        with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page

            from playwright.sync_api import sync_playwright

            if self._playwright is None:
                self._playwright = sync_playwright().start()
                atexit.register(self.close)
            if self._browser is None or not self._browser.is_connected():
                self._browser = self._playwright.chromium.launch(headless=self.headless)

            context = self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                # A real UA: a headless default gets a materially different page
                # from many sites, and reasoning about that page is reasoning
                # about something the user will never see.
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            self._page = context.new_page()
            return self._page

    @property
    def started(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def close(self) -> None:
        with self._lock:
            for attribute, closer in (("_browser", "close"), ("_playwright", "stop")):
                target = getattr(self, attribute)
                if target is None:
                    continue
                try:
                    getattr(target, closer)()
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    pass
                setattr(self, attribute, None)
            self._page = None


def build_session(headless: bool = True) -> "BrowserSession":
    """The browser this install drives.

    A plugin holding `browser.provider` answers here instead of Playwright.
    Built through a factory rather than swapped after construction, because a
    session that has already opened a page is a session with cookies in it —
    handing that to a replacement mid-flight is handing over whatever it was
    signed into.

    Falls back to the built-in on any failure. A browser provider that cannot
    start should cost the page, not the session.
    """
    try:
        from andromeda_agent import plugins as plugins_module

        providers = plugins_module.browser_providers()
    except Exception:  # noqa: BLE001 - the browser must not depend on plugins
        providers = {}

    for name in sorted(providers):
        try:
            built = providers[name](headless=headless)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            import logging

            logging.getLogger(__name__).warning(
                "browser provider %s failed to start, using the built-in: %s",
                name,
                exc,
            )
            break
        if built is not None:
            return built
        break
    return BrowserSession(headless=headless)


def _snapshot_payload(page) -> dict[str, Any]:
    return page.evaluate(SNAPSHOT_SCRIPT, REF_ATTRIBUTE)


def _render(payload: dict[str, Any], include_text: bool = False) -> str:
    parts = [f"{payload['title'] or '(untitled)'} — {payload['url']}"]
    if payload.get("heading"):
        parts.append(f"# {payload['heading']}")

    elements = payload.get("elements") or []
    if elements:
        parts.append("\n".join(elements))
    else:
        parts.append("(no interactive elements found)")

    if include_text and payload.get("text"):
        body = payload["text"][:MAX_TEXT_CHARS]
        parts.append(f"\n--- page text ---\n{body}")

    rendered = "\n\n".join(parts)
    if len(rendered) > MAX_SNAPSHOT_CHARS:
        rendered = rendered[:MAX_SNAPSHOT_CHARS] + "\n\n… snapshot truncated."
    return rendered


def _guarded(session: BrowserSession, action, what: str) -> ToolResult:
    """Run one page action, turning every failure into a readable result.

    A tool that raises ends the turn; a tool that says "no element e7 on this
    page — take a fresh snapshot" lets the model recover, which is the normal
    case after a click changes the DOM.
    """
    if not playwright_available():
        return failure(INSTALL_HINT)
    try:
        return action(session.page())
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        message = str(exc).split("\n")[0][:300]
        return failure(f"{what} failed: {message}")


def navigate(
    session: BrowserSession,
    url: str,
    include_text: bool = False,
    allow_private: bool = False,
) -> ToolResult:
    url = (url or "").strip()
    if not url:
        return failure("A URL is required.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        return failure(f"{url!r} has no host.")
    # The same guard `web_fetch` uses, and for the same reason: a browser is a
    # more capable way to read a router admin page than an HTTP client, not a
    # less sensitive one. Opting in is a session setting rather than a tool
    # argument — a guard the model can switch off is not a guard.
    if not allow_private and _is_private(host):
        return failure(
            f"{host} is on a private or local network. The agent browser only "
            "reaches the public internet — set `allow_private_network` to "
            "browse a local development server."
        )

    def action(page):
        page.goto(url, timeout=NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
        payload = _snapshot_payload(page)
        return ToolResult(
            content=_render(payload, include_text),
            display=f"{payload['title'] or url}",
            metadata={"url": payload["url"], "elements": len(payload.get("elements") or [])},
        )

    return _guarded(session, action, f"navigating to {url}")


def snapshot(session: BrowserSession, include_text: bool = False) -> ToolResult:
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        payload = _snapshot_payload(page)
        return ToolResult(
            content=_render(payload, include_text),
            display=f"{payload['title'] or payload['url']}",
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, "reading the page")


def _selector(ref: str) -> str:
    return f'[{REF_ATTRIBUTE}="{ref}"]'


def _missing_ref(ref: str) -> ToolResult:
    return failure(
        f"No element {ref!r} on this page. The page may have changed — take a "
        "fresh browser_snapshot and use a ref from it."
    )


def click(session: BrowserSession, ref: str) -> ToolResult:
    ref = (ref or "").strip()
    if not ref:
        return failure("A ref is required. Take a browser_snapshot to get one.")
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        element = page.query_selector(_selector(ref))
        if element is None:
            return _missing_ref(ref)
        element.click(timeout=DEFAULT_TIMEOUT_MS)
        # Settle, then re-read: a click that navigates or opens a menu leaves
        # every previous ref stale, and handing back the old outline is how a
        # model ends up clicking a button that is no longer there.
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        payload = _snapshot_payload(page)
        return ToolResult(
            content=f"Clicked {ref}.\n\n{_render(payload)}",
            display=f"clicked {ref}",
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, f"clicking {ref}")


def type_text(
    session: BrowserSession, ref: str, text: str, submit: bool = False
) -> ToolResult:
    ref = (ref or "").strip()
    if not ref:
        return failure("A ref is required.")
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        element = page.query_selector(_selector(ref))
        if element is None:
            return _missing_ref(ref)
        element.fill(text, timeout=DEFAULT_TIMEOUT_MS)
        note = f"Typed into {ref}."
        if submit:
            element.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            note = f"Typed into {ref} and pressed Enter."
        payload = _snapshot_payload(page)
        return ToolResult(
            content=f"{note}\n\n{_render(payload)}",
            display=note,
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, f"typing into {ref}")


def press(session: BrowserSession, key: str) -> ToolResult:
    key = (key or "").strip()
    if not key:
        return failure("A key is required, e.g. Enter or Escape.")
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        page.keyboard.press(key)
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        payload = _snapshot_payload(page)
        return ToolResult(
            content=f"Pressed {key}.\n\n{_render(payload)}",
            display=f"pressed {key}",
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, f"pressing {key}")


def scroll(session: BrowserSession, direction: str = "down", amount: int = 1) -> ToolResult:
    direction = (direction or "down").strip().lower()
    if direction not in {"up", "down", "top", "bottom"}:
        return failure("direction must be up, down, top or bottom.")
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        if direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            sign = 1 if direction == "down" else -1
            page.evaluate(
                "([sign, pages]) => window.scrollBy(0, sign * pages * window.innerHeight * 0.9)",
                [sign, max(1, int(amount or 1))],
            )
        payload = _snapshot_payload(page)
        return ToolResult(
            content=f"Scrolled {direction}.\n\n{_render(payload)}",
            display=f"scrolled {direction}",
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, f"scrolling {direction}")


def back(session: BrowserSession) -> ToolResult:
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        response = page.go_back(timeout=NAVIGATION_TIMEOUT_MS)
        if response is None:
            return failure("There is nothing to go back to.")
        payload = _snapshot_payload(page)
        return ToolResult(
            content=f"Went back.\n\n{_render(payload)}",
            display="went back",
            metadata={"url": payload["url"]},
        )

    return _guarded(session, action, "going back")


def read_page(session: BrowserSession) -> ToolResult:
    """The page's text, for when the outline is not the point."""
    if not session.started:
        return failure("No page is open. Call browser_navigate first.")

    def action(page):
        payload = _snapshot_payload(page)
        text = (payload.get("text") or "").strip()
        if not text:
            return failure("The page has no readable text.")
        truncated = len(text) > MAX_TEXT_CHARS
        return ToolResult(
            content=text[:MAX_TEXT_CHARS] + ("\n\n… truncated." if truncated else ""),
            display=f"{payload['title'] or payload['url']} — {len(text):,} chars",
            metadata={"url": payload["url"], "truncated": truncated},
        )

    return _guarded(session, action, "reading the page text")
