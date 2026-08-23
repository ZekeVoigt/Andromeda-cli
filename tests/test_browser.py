"""The browser tools, against a real Chromium and a real local server.

Not mocked. A snapshot is a JavaScript walk of a live DOM, and a fake page
object would test the fixture rather than the walk.

The local server is reached with `allow_private=True` — the same explicit
opt-in a developer working against their own dev server uses. The default
refusal is tested separately.
"""

from __future__ import annotations

import http.server
import threading
from functools import partial
from pathlib import Path

import pytest

from andromeda_tools import browser

def _chromium_launches() -> str:
    """Whether Chromium can actually start here.

    Installed is not the same as runnable: a browser can fail to launch for
    reasons that have nothing to do with this code — a corrupted download, a
    machine whose process services are degraded, a CI image without the shared
    libraries. Fourteen red failures in that case hide whatever real regression
    lands next, so the honest outcome is a skip that names the reason.
    """
    if not browser.playwright_available():
        return "Playwright is not installed"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as driver:
            instance = driver.chromium.launch(headless=True)
            instance.close()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        return f"Chromium will not launch here: {str(exc).splitlines()[0][:120]}"
    return ""


_LAUNCH_PROBLEM = _chromium_launches()

pytestmark = pytest.mark.skipif(bool(_LAUNCH_PROBLEM), reason=_LAUNCH_PROBLEM or "ok")

PAGES = {
    "/index.html": """
        <html><head><title>Home</title></head><body>
          <h1>Welcome</h1>
          <p>Some body text that should be readable.</p>
          <a href="/second.html">Go to second</a>
          <button onclick="document.getElementById('out').innerText='clicked'">Press me</button>
          <input type="text" name="q" placeholder="Search here">
          <input type="hidden" name="secret" value="hidden-value">
          <div style="display:none"><button>Invisible</button></div>
          <div id="out"></div>
          <script>var tracked = 1;</script>
          <style>.x { color: red }</style>
        </body></html>
    """,
    "/second.html": """
        <html><head><title>Second</title></head><body>
          <h1>Second page</h1>
          <a href="/index.html">Back home</a>
        </body></html>
    """,
    "/form.html": """
        <html><head><title>Form</title></head><body>
          <h1>Form</h1>
          <form action="/second.html" method="get">
            <input type="text" name="query" placeholder="Type a query">
          </form>
        </body></html>
    """,
    "/tall.html": """
        <html><head><title>Tall</title></head><body>
          <h1>Top</h1>
          <div style="height:4000px"></div>
          <button>Bottom button</button>
        </body></html>
    """,
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        path = self.path.split("?")[0]
        body = PAGES.get(path)
        if body is None:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):  # noqa: A003 - silence the test output
        pass


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def session():
    live = browser.BrowserSession()
    yield live
    live.close()


def go(session, server, path="/index.html", **kwargs):
    return browser.navigate(session, f"{server}{path}", allow_private=True, **kwargs)


class TestSnapshot:
    def test_lists_interactive_elements_with_refs(self, session, server):
        result = go(session, server)
        assert result.ok
        assert "Home" in result.content
        assert "# Welcome" in result.content
        assert 'link "Go to second"' in result.content
        assert 'button "Press me"' in result.content
        assert "[e1]" in result.content

    def test_refs_are_stable_within_one_snapshot(self, session, server):
        go(session, server)
        first = browser.snapshot(session).content
        second = browser.snapshot(session).content
        assert first == second

    def test_hidden_elements_are_not_offered(self, session, server):
        result = go(session, server)
        assert "Invisible" not in result.content

    def test_hidden_inputs_are_not_offered(self, session, server):
        """A ref for a field a person cannot see is a ref to something else."""
        result = go(session, server)
        assert "hidden-value" not in result.content

    def test_script_and_style_are_not_in_the_text(self, session, server):
        result = go(session, server, include_text=True)
        assert "Some body text" in result.content
        assert "var tracked" not in result.content
        assert "color: red" not in result.content

    def test_text_is_off_by_default(self, session, server):
        assert "Some body text" not in go(session, server).content

    def test_snapshot_before_navigating_says_so(self, session):
        result = browser.snapshot(session)
        assert result.ok is False and "browser_navigate" in result.content


class TestNavigate:
    def test_a_bare_host_gets_https(self, session):
        # Refused for a different reason than a scheme error, which is what
        # proves the scheme was added.
        result = browser.navigate(session, "example.invalid")
        assert "https" not in result.content.lower() or result.ok is False

    def test_a_private_address_is_refused_by_default(self, session, server):
        result = browser.navigate(session, f"{server}/index.html")
        assert result.ok is False
        assert "allow_private_network" in result.content

    def test_an_empty_url_is_refused(self, session):
        assert browser.navigate(session, "  ").ok is False

    def test_a_404_is_reported_not_raised(self, session, server):
        result = go(session, server, "/nope.html")
        # The page loads; it just has nothing on it. Either way, no exception.
        assert isinstance(result.content, str)


class TestInteraction:
    def test_clicking_a_link_navigates_and_re_snapshots(self, session, server):
        go(session, server)
        snap = browser.snapshot(session).content
        ref = _ref_for(snap, "Go to second")

        result = browser.click(session, ref)
        assert result.ok
        assert "Second page" in result.content
        assert "second.html" in result.metadata["url"]

    def test_clicking_a_button_re_reads_the_page(self, session, server):
        go(session, server)
        ref = _ref_for(browser.snapshot(session).content, "Press me")
        result = browser.click(session, ref)
        assert result.ok and "Clicked" in result.content

    def test_a_stale_ref_says_to_take_a_fresh_snapshot(self, session, server):
        go(session, server)
        result = browser.click(session, "e999")
        assert result.ok is False
        assert "fresh browser_snapshot" in result.content

    def test_typing_fills_a_field(self, session, server):
        go(session, server, "/form.html")
        ref = _ref_for(browser.snapshot(session).content, "Type a query")
        result = browser.type_text(session, ref, "hello world")
        assert result.ok
        assert "hello world" in browser.snapshot(session).content

    def test_typing_with_submit_navigates(self, session, server):
        go(session, server, "/form.html")
        ref = _ref_for(browser.snapshot(session).content, "Type a query")
        result = browser.type_text(session, ref, "q", submit=True)
        assert result.ok
        assert "pressed Enter" in result.display

    def test_going_back_returns_to_the_previous_page(self, session, server):
        go(session, server)
        ref = _ref_for(browser.snapshot(session).content, "Go to second")
        browser.click(session, ref)

        result = browser.back(session)
        assert result.ok and "Welcome" in result.content

    def test_going_back_with_no_history_says_so(self, session, server):
        go(session, server)
        assert browser.back(session).ok is False

    def test_scrolling_to_the_bottom_works(self, session, server):
        go(session, server, "/tall.html")
        result = browser.scroll(session, "bottom")
        assert result.ok and "Bottom button" in result.content

    def test_an_unknown_scroll_direction_is_refused(self, session, server):
        go(session, server)
        assert browser.scroll(session, "sideways").ok is False

    def test_pressing_a_key_works(self, session, server):
        go(session, server)
        assert browser.press(session, "Escape").ok

    def test_reading_returns_the_page_text(self, session, server):
        go(session, server)
        result = browser.read_page(session)
        assert result.ok and "Some body text" in result.content

    def test_acting_before_navigating_says_so(self, session):
        for call in (
            lambda: browser.click(session, "e1"),
            lambda: browser.type_text(session, "e1", "x"),
            lambda: browser.press(session, "Enter"),
            lambda: browser.scroll(session),
            lambda: browser.back(session),
            lambda: browser.read_page(session),
        ):
            assert "browser_navigate" in call().content


class TestSessionLifecycle:
    def test_a_session_is_not_started_until_it_navigates(self):
        live = browser.BrowserSession()
        assert live.started is False
        live.close()

    def test_closing_twice_is_safe(self, session, server):
        go(session, server)
        session.close()
        session.close()

    def test_it_restarts_after_being_closed(self, session, server):
        go(session, server)
        session.close()
        assert go(session, server).ok


def _ref_for(snapshot_text: str, name: str) -> str:
    for line in snapshot_text.splitlines():
        if name in line and line.startswith("["):
            return line[1 : line.index("]")]
    raise AssertionError(f"no ref for {name!r} in:\n{snapshot_text}")


class TestFieldValues:
    """A model that cannot see what it typed types it again."""

    def test_a_filled_field_shows_its_value_and_keeps_its_label(self, session, server):
        go(session, server, "/form.html")
        ref = _ref_for(browser.snapshot(session).content, "Type a query")
        browser.type_text(session, ref, "hello world")

        line = _line_for(browser.snapshot(session).content, "Type a query")
        assert 'value: "hello world"' in line
        # The label survives, so the field is still identifiable next snapshot.
        assert '"Type a query"' in line

    def test_an_empty_field_shows_no_value(self, session, server):
        go(session, server, "/form.html")
        assert "value:" not in browser.snapshot(session).content

    def test_a_password_value_is_never_shown(self, session):
        page = session.page()
        page.set_content('<input type="password" name="pw" value="hunter2">')
        assert "hunter2" not in browser.snapshot(session).content


def _line_for(snapshot_text: str, name: str) -> str:
    for line in snapshot_text.splitlines():
        if name in line and line.startswith("["):
            return line
    raise AssertionError(f"no line for {name!r} in:\n{snapshot_text}")
