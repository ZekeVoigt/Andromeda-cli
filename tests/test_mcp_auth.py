"""OAuth against MCP servers.

The whole flow is exercised against a real HTTP server on a loopback port
rather than against mocks. Every defect worth catching here is a defect in
what actually goes over the wire — a `resource` parameter that is not sent, a
`redirect_uri` that differs between the two legs, a `state` that is compared
loosely — and a mock is a second opinion about the wire rather than the wire.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import socket
import stat
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from andromeda_tools import mcp_auth


# ---------------------------------------------------------------------------
# A pocket authorization server
# ---------------------------------------------------------------------------


class _Provider(http.server.BaseHTTPRequestHandler):
    """Enough of RFC 8414 / 7591 / 6749 to be worth testing against."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence during tests
        pass

    @property
    def state(self) -> dict:
        return self.server.state  # type: ignore[attr-defined]

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        base = self.state["base"]

        if path == "/.well-known/oauth-protected-resource":
            self._json(200, {"resource": base, "authorization_servers": [base]})
        elif path == "/.well-known/oauth-authorization-server":
            self._json(
                200,
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "registration_endpoint": f"{base}/register",
                    "scopes_supported": ["read", "write"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        elif path == "/authorize":
            self.state["authorize"] = dict(
                urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query)
            )
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if path == "/register":
            self.state["register"] = json.loads(raw or "{}")
            self._json(201, {"client_id": "generated-client-id"})
            return

        if path == "/token":
            form = dict(urllib.parse.parse_qsl(raw))
            self.state.setdefault("token_requests", []).append(form)

            if form.get("grant_type") == "refresh_token":
                if form["refresh_token"] != self.state["refresh_token"]:
                    self._json(400, {"error": "invalid_grant"})
                    return
                self._json(
                    200,
                    {
                        "access_token": "refreshed-access-token-value",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
                return

            expected = base64.urlsafe_b64encode(
                hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            ).decode().rstrip("=")
            if expected != self.state["authorize"].get("code_challenge"):
                self._json(400, {"error": "invalid_grant",
                                 "error_description": "PKCE mismatch"})
                return
            if form.get("redirect_uri") != self.state["authorize"].get("redirect_uri"):
                self._json(400, {"error": "invalid_grant",
                                 "error_description": "redirect_uri mismatch"})
                return

            self._json(
                200,
                {
                    "access_token": "issued-access-token-value",
                    "refresh_token": self.state["refresh_token"],
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "read write",
                },
            )
            return

        # The MCP endpoint itself: unauthorized unless a token comes with it.
        authorization = self.headers.get("Authorization") or ""
        if authorization != "Bearer issued-access-token-value":
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer resource_metadata='
                f'"{self.state["base"]}/.well-known/oauth-protected-resource"',
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})


@pytest.fixture
def provider():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    port = server.server_address[1]
    server.state = {  # type: ignore[attr-defined]
        "base": f"http://127.0.0.1:{port}",
        "refresh_token": "issued-refresh-token-value",
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _drive_browser(url: str, opened: bool) -> None:
    """Stand in for the person: fetch the authorize URL, then the redirect.

    The provider records the authorize query when it is fetched, so this both
    exercises the real URL we built and lets the redirect carry back the state
    we actually sent — a test that made up its own state would pass against a
    listener that ignored it.
    """
    httpx.get(url, timeout=5)
    parsed = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    httpx.get(
        parsed["redirect_uri"]
        + "?"
        + urllib.parse.urlencode({"code": "the-code", "state": parsed["state"]}),
        timeout=5,
    )


def _authorize(tmp_path: Path, provider, **kwargs):
    base = provider.state["base"]

    def announce(url: str, opened: bool) -> None:
        # On a thread, because `authorize` is blocking on the listener that
        # this request has to reach.
        threading.Thread(target=_drive_browser, args=(url, opened), daemon=True).start()

    return mcp_auth.authorize(
        home=tmp_path,
        server="pocket",
        server_url=f"{base}/mcp",
        open_browser=False,
        announce=announce,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def test_a_full_flow_registers_authorizes_and_stores(tmp_path, provider):
    stored = _authorize(tmp_path, provider)

    assert stored.tokens.access_token == "issued-access-token-value"
    assert stored.tokens.refresh_token == "issued-refresh-token-value"
    assert stored.tokens.valid
    assert stored.client.client_id == "generated-client-id"
    assert mcp_auth.token_path(tmp_path, "pocket").exists()


def test_the_client_registers_as_public_with_no_secret(tmp_path, provider):
    """A CLI on a laptop cannot keep a secret; PKCE is what replaces it."""
    _authorize(tmp_path, provider)
    registration = provider.state["register"]
    assert registration["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registration
    assert registration["grant_types"] == ["authorization_code", "refresh_token"]


def test_the_authorization_request_carries_a_resource_indicator(tmp_path, provider):
    """Without it the token is valid at every resource the issuer serves."""
    _authorize(tmp_path, provider)
    query = provider.state["authorize"]
    assert query["resource"] == f"{provider.state['base']}/mcp"


def test_the_authorization_request_uses_s256_and_never_plain(tmp_path, provider):
    _authorize(tmp_path, provider)
    assert provider.state["authorize"]["code_challenge_method"] == "S256"


def test_the_token_request_repeats_the_same_redirect_uri(tmp_path, provider):
    """The provider rejects a mismatch, so this passing is the proof."""
    _authorize(tmp_path, provider)
    exchange = provider.state["token_requests"][0]
    assert exchange["redirect_uri"] == provider.state["authorize"]["redirect_uri"]
    assert exchange["resource"] == f"{provider.state['base']}/mcp"


def test_scope_defaults_to_what_the_server_supports(tmp_path, provider):
    _authorize(tmp_path, provider)
    assert provider.state["authorize"]["scope"] == "read write"


def test_a_configured_scope_wins(tmp_path, provider):
    _authorize(tmp_path, provider, config={"scope": "read"})
    assert provider.state["authorize"]["scope"] == "read"


def test_a_configured_client_id_skips_registration(tmp_path, provider):
    _authorize(tmp_path, provider, config={"client_id": "preregistered"})
    assert "register" not in provider.state
    assert provider.state["authorize"]["client_id"] == "preregistered"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_the_token_file_is_not_readable_by_anyone_else(tmp_path, provider):
    _authorize(tmp_path, provider)
    path = mcp_auth.token_path(tmp_path, "pocket")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"tokens are {oct(mode)}"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_file_is_never_world_readable_even_briefly(tmp_path):
    """The mode goes on before the content, not after.

    Between a default-mode create and a chmod there is a window, and a
    credential file only has to be readable once.
    """
    stored = mcp_auth.Stored(tokens=mcp_auth.Tokens(access_token="a-token-value"))
    mcp_auth.save(tmp_path, "srv", stored)
    path = mcp_auth.token_path(tmp_path, "srv")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp")), "the temporary file was left behind"


def test_a_round_trip_preserves_everything(tmp_path):
    stored = mcp_auth.Stored(
        tokens=mcp_auth.Tokens(
            access_token="a", refresh_token="b", expires_at=123.0, scope="read"
        ),
        client=mcp_auth.ClientRegistration(client_id="c", client_secret="d"),
        redirect_uri="http://127.0.0.1:1/callback",
        issuer="https://issuer",
    )
    mcp_auth.save(tmp_path, "srv", stored)
    loaded = mcp_auth.load(tmp_path, "srv")
    assert loaded == stored


def test_a_corrupt_file_reads_as_signed_out(tmp_path):
    path = mcp_auth.token_path(tmp_path, "srv")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert mcp_auth.load(tmp_path, "srv").tokens.access_token == ""


def test_forget_removes_the_file(tmp_path):
    mcp_auth.save(tmp_path, "srv", mcp_auth.Stored())
    assert mcp_auth.forget(tmp_path, "srv") is True
    assert mcp_auth.forget(tmp_path, "srv") is False


@pytest.mark.parametrize("name", ["../../etc/passwd", "..", ".", "a/b", "~/x", ""])
def test_a_server_name_cannot_escape_the_token_directory(tmp_path, name):
    """A name comes from a config file, and a config file can say anything.

    The invariant is where the file lands, not what it is called — dots in a
    filename are harmless once the separators are gone.
    """
    directory = mcp_auth.token_dir(tmp_path).resolve()
    path = mcp_auth.token_path(tmp_path, name)
    assert path.parent == directory
    assert path.resolve().parent == directory


def test_stored_tokens_are_registered_for_redaction(tmp_path):
    """An MCP token has no vendor shape, so exact match is the only pass that
    will ever catch it — and a server echoing back its own Authorization header
    is exactly the accident this guards against."""
    from andromeda_agent import redact

    redact.clear_known()
    try:
        mcp_auth.save(
            tmp_path,
            "srv",
            mcp_auth.Stored(tokens=mcp_auth.Tokens(access_token="opaque-token-abcdefgh")),
        )
        assert "opaque-token-abcdefgh" not in redact.scrub(
            "echo: opaque-token-abcdefgh"
        ).text
    finally:
        redact.clear_known()


# ---------------------------------------------------------------------------
# Expiry and refresh
# ---------------------------------------------------------------------------


def test_a_token_close_to_expiry_counts_as_invalid():
    """Refreshing after expiry is a 401 the user sees for no reason."""
    assert not mcp_auth.Tokens(access_token="a", expires_at=time.time() + 5).valid
    assert mcp_auth.Tokens(access_token="a", expires_at=time.time() + 600).valid


def test_a_token_with_no_expiry_is_taken_at_its_word():
    """Guessing a lifetime expires a token that was never going to."""
    assert mcp_auth.Tokens(access_token="a", expires_at=0).valid


def test_refresh_exchanges_the_refresh_token(tmp_path, provider):
    stored = _authorize(tmp_path, provider)
    refreshed = mcp_auth.refresh(
        tmp_path, "pocket", f"{provider.state['base']}/mcp", stored
    )
    assert refreshed is not None
    assert refreshed.tokens.access_token == "refreshed-access-token-value"


def test_refresh_keeps_the_old_token_when_the_server_returns_none(tmp_path, provider):
    """A server that does not rotate returns no new refresh token, and dropping
    the old one ends the session at the next expiry."""
    stored = _authorize(tmp_path, provider)
    refreshed = mcp_auth.refresh(
        tmp_path, "pocket", f"{provider.state['base']}/mcp", stored
    )
    assert refreshed.tokens.refresh_token == "issued-refresh-token-value"


def test_a_revoked_refresh_token_returns_none_rather_than_raising(tmp_path, provider):
    """An expired grant is an ordinary end of life, not a session failure."""
    stored = _authorize(tmp_path, provider)
    stored.tokens.refresh_token = "revoked"
    assert mcp_auth.refresh(
        tmp_path, "pocket", f"{provider.state['base']}/mcp", stored
    ) is None


def test_refresh_without_a_refresh_token_is_none(tmp_path):
    stored = mcp_auth.Stored(
        tokens=mcp_auth.Tokens(access_token="a"), issuer="https://x"
    )
    assert mcp_auth.refresh(tmp_path, "srv", "https://x/mcp", stored) is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_the_resource_metadata_url_comes_from_the_challenge(provider):
    response = httpx.post(f"{provider.state['base']}/mcp", json={}, timeout=5)
    assert response.status_code == 401
    assert mcp_auth.resource_metadata_url(response, f"{provider.state['base']}/mcp") == (
        f"{provider.state['base']}/.well-known/oauth-protected-resource"
    )


def test_a_bare_401_falls_back_to_the_well_known_path():
    """Common enough that refusing it would be refusing real servers."""
    response = httpx.Response(401)
    assert mcp_auth.resource_metadata_url(response, "https://x.test/deep/mcp") == (
        "https://x.test/.well-known/oauth-protected-resource"
    )


def test_a_server_with_no_metadata_says_so_and_says_what_to_do():
    # A closed local port rather than an unroutable address: a refused
    # connection returns at once, and four candidate URLs against a blackholed
    # IP is four discovery timeouts — two minutes for one assertion.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]

    with pytest.raises(mcp_auth.OAuthError) as caught:
        with httpx.Client() as client:
            mcp_auth.discover_endpoints(client, f"http://127.0.0.1:{closed}")
    assert "does not publish OAuth metadata" in str(caught.value)
    assert "headers" in caught.value.hint


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://Example.COM/mcp", "https://example.com/mcp"),
        ("https://example.com:443/mcp", "https://example.com/mcp"),
        ("https://example.com/mcp/", "https://example.com/mcp"),
        ("https://example.com/mcp#frag", "https://example.com/mcp"),
        ("http://example.com:8080/mcp", "http://example.com:8080/mcp"),
    ],
)
def test_the_canonical_resource_is_normalised(url, expected):
    assert mcp_auth.canonical_resource(url) == expected


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_a_verifier_is_within_the_permitted_length():
    verifier = mcp_auth.make_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier


def test_two_verifiers_are_never_the_same():
    assert mcp_auth.make_verifier() != mcp_auth.make_verifier()


def test_the_challenge_is_the_unpadded_sha256_of_the_verifier():
    verifier = "abc123"
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"abc123").digest())
    assert mcp_auth.challenge_for(verifier) == expected.decode().rstrip("=")


# ---------------------------------------------------------------------------
# The transport's view
# ---------------------------------------------------------------------------


def test_a_signed_in_authorization_supplies_a_bearer_header(tmp_path, provider):
    _authorize(tmp_path, provider)
    auth = mcp_auth.Authorization(
        tmp_path, "pocket", f"{provider.state['base']}/mcp"
    )
    assert auth.headers() == {"Authorization": "Bearer issued-access-token-value"}
    assert auth.signed_in()


def test_an_unauthorized_session_asks_for_a_browser_rather_than_opening_one(tmp_path):
    """A scheduled run at three in the morning must say what to run, not sit on
    a listener until it times out."""
    auth = mcp_auth.Authorization(tmp_path, "pocket", "https://x.test/mcp")
    with pytest.raises(mcp_auth.NeedsBrowser) as caught:
        auth.handle_unauthorized()
    assert "andromeda mcp login pocket" in caught.value.hint


def test_an_expiring_token_is_refreshed_before_it_is_used(tmp_path, provider):
    """One request, not a failed call plus a refresh plus a retry."""
    stored = _authorize(tmp_path, provider)
    stored.tokens.expires_at = time.time() + 5
    mcp_auth.save(tmp_path, "pocket", stored)

    auth = mcp_auth.Authorization(tmp_path, "pocket", f"{provider.state['base']}/mcp")
    assert auth.headers()["Authorization"] == "Bearer refreshed-access-token-value"


def test_no_tokens_means_no_header(tmp_path):
    auth = mcp_auth.Authorization(tmp_path, "none", "https://x.test/mcp")
    assert auth.headers() == {}
    assert not auth.signed_in()


# ---------------------------------------------------------------------------
# End to end, through the MCP client
# ---------------------------------------------------------------------------


def test_an_oauth_server_connects_once_it_has_been_authorized(tmp_path, provider):
    from andromeda_tools import mcp as mcp_module

    _authorize(tmp_path, provider)
    server = mcp_module.MCPServer(
        name="pocket",
        config={"url": f"{provider.state['base']}/mcp", "auth": "oauth"},
        home=tmp_path,
    )
    assert server.uses_oauth
    assert server.connect(), server.error
    server.close()


def test_an_unauthorized_oauth_server_reports_what_to_run(tmp_path, provider):
    """Reported as needing sign-in, not as broken — otherwise people go and
    check their config file instead of running the one command that fixes it."""
    from andromeda_tools import mcp as mcp_module

    server = mcp_module.MCPServer(
        name="pocket",
        config={"url": f"{provider.state['base']}/mcp", "auth": "oauth"},
        home=tmp_path,
    )
    assert not server.connect()
    assert "andromeda mcp login pocket" in server.error


def test_a_server_without_a_home_is_simply_not_offered_oauth(provider):
    """No guessed credential path when the caller did not name one."""
    from andromeda_tools import mcp as mcp_module

    server = mcp_module.MCPServer(
        name="pocket", config={"url": "https://x.test/mcp", "auth": "oauth"}
    )
    assert server._authorization() is None


def test_an_oauth_config_block_implies_oauth(tmp_path):
    from andromeda_tools import mcp as mcp_module

    server = mcp_module.MCPServer(
        name="s", config={"url": "https://x.test/mcp", "oauth": {"scope": "read"}}
    )
    assert server.uses_oauth


def test_a_stdio_server_is_never_treated_as_oauth(tmp_path):
    from andromeda_tools import mcp as mcp_module

    server = mcp_module.MCPServer(
        name="s", config={"command": "true", "auth": "oauth"}, home=tmp_path
    )
    assert server._authorization() is None


def test_an_expired_authorization_mid_call_is_a_tool_error_not_a_dead_turn(tmp_path):
    """`mcp_auth` raises its own exception type, not `MCPError`.

    A tool call that propagates ends the turn. The model has to be able to read
    what happened and move on — and the message has to carry the command, or
    "sign in with this" becomes "something went wrong".
    """
    from andromeda_tools import mcp as mcp_module

    server = mcp_module.MCPServer(
        name="pocket",
        config={"url": "https://x.test/mcp", "auth": "oauth"},
        home=tmp_path,
    )
    server.connected = True

    class _Refusing:
        def send(self, payload, timeout):
            raise mcp_auth.NeedsBrowser("pocket")

        def close(self):
            pass

    server.transport = _Refusing()
    result = server.call("search", {})
    assert not result.ok
    assert "andromeda mcp login pocket" in result.content
    assert not server.connected, "a refused server must not stay marked live"
