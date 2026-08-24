from __future__ import annotations

import json
import time
import urllib.error

import httpx
import respx

from andromeda_cli import config as config_module
from andromeda_cli.commands import auth

BASE = "https://andromeda.test"
PAIR = f"{BASE}/api/gateway/pair"
SIGN_IN = f"{BASE}/cli/auth"


def route_exists() -> None:
    """Answer the probe the browser lane makes before it opens anything.

    A 400 is the real answer from a deployed route asked with no `state`, and
    proof that it is there. Registered per test rather than in a fixture: the
    two tests at the bottom are *about* this probe and need it to answer
    differently.
    """
    respx.get(SIGN_IN).mock(return_value=httpx.Response(400))


@respx.mock
def test_login_stores_the_token_and_reports_success():
    respx.post(PAIR).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "deviceToken": "a" * 64,
                "userId": "user_42",
                "convexUrl": "https://convex.test",
            },
        )
    )

    assert auth.login("abc123", base_url=BASE) == 0

    credentials = config_module.load_credentials()
    assert credentials.paired
    assert credentials.user_id == "user_42"
    assert credentials.base_url == BASE


@respx.mock
def test_login_upcases_the_code_before_sending():
    route = respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    auth.login("ab12cd", base_url=BASE)
    body = json.loads(route.calls.last.request.read())
    assert body["pairingCode"] == "AB12CD"


@respx.mock
def test_relogin_reuses_the_device_id():
    """One machine should be one device row, not one per login."""
    route = respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    auth.login("code01", base_url=BASE)
    first = config_module.load_credentials().device_id

    auth.login("code02", base_url=BASE)
    second = config_module.load_credentials().device_id

    assert first == second
    assert route.call_count == 2


@respx.mock
def test_a_rejected_code_writes_nothing():
    respx.post(PAIR).mock(
        return_value=httpx.Response(400, json={"success": False, "error": "Code expired"})
    )
    assert auth.login("expired", base_url=BASE) == 1
    assert config_module.load_credentials().paired is False


@respx.mock
def test_success_without_a_token_is_treated_as_failure():
    respx.post(PAIR).mock(return_value=httpx.Response(200, json={"success": True}))
    assert auth.login("abc123", base_url=BASE) == 1
    assert config_module.load_credentials().paired is False


@respx.mock
def test_unreachable_host_is_an_error_not_a_crash():
    respx.post(PAIR).mock(side_effect=httpx.ConnectError("nope"))
    assert auth.login("abc123", base_url=BASE) == 1


def test_empty_code_is_a_usage_error():
    assert auth.login("   ", base_url=BASE) == 2


def test_status_and_logout_round_trip():
    assert auth.status() == 1  # not paired

    config_module.save_credentials(
        config_module.Credentials(
            device_token="t" * 64, device_id="cli-1", user_id="u", base_url=BASE
        )
    )
    assert auth.status() == 0
    assert auth.logout() == 0
    assert auth.status() == 1


# ---------------------------------------------------------------------------
# The browser lane
# ---------------------------------------------------------------------------


class _Browser:
    """Stands in for the person and their browser.

    Given the URL the CLI would have opened, it does what the website does:
    reads the state and port out of it and calls the loopback listener back
    with a pairing code. Run on a thread because the CLI blocks on that
    listener — which is the behaviour under test.
    """

    def __init__(self, *, code: str = "ABC123", state_override: str | None = None,
                 error: str = "") -> None:
        self.code = code
        self.state_override = state_override
        self.error = error
        self.url = ""
        self.status = 0
        self.body = ""

    def __call__(self, url: str) -> bool:
        import threading
        import urllib.parse
        import urllib.request

        self.url = url
        parts = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        port = parts["port"][0]
        state = self.state_override or parts["state"][0]
        query = {"state": state}
        if self.error:
            query["error"] = self.error
        else:
            query["code"] = self.code
        target = f"http://127.0.0.1:{port}/callback?{urllib.parse.urlencode(query)}"

        def visit():
            try:
                with urllib.request.urlopen(target, timeout=5) as response:
                    self.status = response.status
                    self.body = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:  # a refused sign-in still answers
                self.status = exc.code
                self.body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - the assertions below say what went wrong
                pass

        threading.Thread(target=visit, daemon=True).start()
        return True


@respx.mock
def test_the_browser_lane_pairs_without_anything_being_typed(monkeypatch):
    route_exists()
    respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "user_7"}
        )
    )
    browser = _Browser(code="ZZ9999")
    monkeypatch.setattr(auth.webbrowser, "open", browser)

    result = auth.browser_login(base_url=BASE, wait_seconds=10)

    assert result.ok, result.error
    assert config_module.load_credentials().user_id == "user_7"
    assert browser.url.startswith(f"{BASE}/cli/auth?")


@respx.mock
def test_the_code_reaches_the_pairing_endpoint_upcased(monkeypatch):
    route_exists()
    route = respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    monkeypatch.setattr(auth.webbrowser, "open", _Browser(code="ab12cd"))

    assert auth.browser_login(base_url=BASE, wait_seconds=10).ok
    assert json.loads(route.calls.last.request.read())["pairingCode"] == "AB12CD"


@respx.mock
def test_the_browser_is_told_it_worked_and_where_to_go_next(monkeypatch):
    route_exists()
    respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    browser = _Browser()
    monkeypatch.setattr(auth.webbrowser, "open", browser)

    auth.browser_login(base_url=BASE, wait_seconds=10)
    for _ in range(50):
        if browser.body:
            break
        time.sleep(0.05)

    assert browser.status == 200
    assert "signed in" in browser.body.lower()
    # The page's whole job after the confirmation: get them to the one place
    # the terminal cannot take them.
    assert f"{BASE}/settings" in browser.body


@respx.mock
def test_a_callback_with_the_wrong_state_is_ignored(monkeypatch):
    """A stale tab from an earlier attempt looks exactly like a forgery.

    Either way it is not this sign-in, so it must not end the wait and must
    never be exchanged.
    """
    route_exists()
    route = respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    monkeypatch.setattr(auth.webbrowser, "open", _Browser(state_override="not-the-state"))

    result = auth.browser_login(base_url=BASE, wait_seconds=2)

    assert result.ok is False
    assert route.call_count == 0
    assert config_module.load_credentials().paired is False


@respx.mock
def test_a_website_error_is_reported_rather_than_waited_out(monkeypatch):
    route_exists()
    monkeypatch.setattr(auth.webbrowser, "open", _Browser(error="Could not create a pairing code."))

    result = auth.browser_login(base_url=BASE, wait_seconds=10)

    assert result.ok is False
    assert "pairing code" in result.error


@respx.mock
def test_giving_up_says_how_to_finish_by_hand(monkeypatch):
    """The ssh case. A timeout must not be a dead end."""
    route_exists()
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: False)

    result = auth.browser_login(base_url=BASE, wait_seconds=0.2)

    assert result.ok is False
    assert "andromeda auth login" in result.error


@respx.mock
def test_the_listener_only_accepts_this_machine(monkeypatch):
    """Bound to loopback, never to every interface.

    A listener holding a live pairing exchange open on 0.0.0.0 is reachable
    from the café network the laptop is sitting on.
    """
    seen = {}

    class Recorder(auth._CallbackServer):
        def __init__(self, address, handler):
            seen["address"] = address
            super().__init__(address, handler)

    route_exists()
    monkeypatch.setattr(auth, "_CallbackServer", Recorder)
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: False)
    auth.browser_login(base_url=BASE, wait_seconds=0.2)

    assert seen["address"][0] == "127.0.0.1"


@respx.mock
def test_login_with_no_code_uses_the_browser(monkeypatch):
    route_exists()
    respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    monkeypatch.setattr(auth.webbrowser, "open", _Browser())

    assert auth.login(None, base_url=BASE) == 0
    assert config_module.load_credentials().paired


@respx.mock
def test_no_browser_still_prints_the_url(monkeypatch, capsys):
    route_exists()
    opened = []
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url) or True)

    auth.browser_login(base_url=BASE, open_browser=False, wait_seconds=0.2)

    assert opened == [], "--no-browser must not open anything"
    assert "/cli/auth?" in capsys.readouterr().out


@respx.mock
def test_a_server_without_the_route_says_so_instead_of_waiting(monkeypatch):
    """An older deployment must not cost five minutes of staring at a 404."""
    respx.get(f"{BASE}/cli/auth").mock(return_value=httpx.Response(404))
    opened = []
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url) or True)

    result = auth.browser_login(base_url=BASE, wait_seconds=60)

    assert result.ok is False
    assert "auth login <code>" in result.error
    assert opened == [], "no browser should be opened onto a 404"


@respx.mock
def test_a_probe_that_fails_on_its_own_does_not_block_sign_in(monkeypatch):
    """A proxy, a captive portal, a slow link — none of those are a refusal."""
    respx.get(f"{BASE}/cli/auth").mock(side_effect=httpx.ConnectError("flaky"))
    respx.post(PAIR).mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceToken": "a" * 64, "userId": "u"}
        )
    )
    monkeypatch.setattr(auth.webbrowser, "open", _Browser())

    assert auth.browser_login(base_url=BASE, wait_seconds=10).ok
