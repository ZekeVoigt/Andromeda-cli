"""OAuth for MCP servers that will not talk to an anonymous client.

`mcp.py` could reach a server over stdio or over HTTP, and over HTTP it could
only ever send a static header. That is the whole of what a self-hosted server
needs and none of what a hosted one does — every managed MCP server answers an
unauthenticated request with a 401 and a pointer to an authorization server. So
the tool that "buys a category" was, in practice, unable to reach most of the
category.

Implemented against the specifications rather than through an SDK, for the same
reason `mcp.py` is: this is the OAuth 2.1 authorization-code flow, which has not
changed in a decade, and a dependency here is a dependency whose next release
can break every session.

    RFC 9728  protected-resource metadata — who guards this server
    RFC 8414  authorization-server metadata — where its endpoints are
    RFC 7591  dynamic client registration — how a client with no prior
              relationship gets a client_id
    RFC 7636  PKCE — the code exchange, S256 only
    RFC 8707  resource indicators — what binds the token to *this* server

**The whole flow, once:**

1. A request comes back 401 with `WWW-Authenticate: Bearer resource_metadata=…`.
2. Fetch that document; it names the authorization servers.
3. Fetch the authorization server's metadata; it names the endpoints.
4. Register as a client, if this machine has not already.
5. Open a browser at the authorization endpoint with a PKCE challenge and a
   `resource` parameter, and wait on a loopback listener for the redirect.
6. Exchange the code for an access token and a refresh token.
7. Store both, 0600, and refresh them before they expire rather than after.

**What is deliberately not here.** Client ID Metadata Documents — the newer
alternative to dynamic registration — require publishing a document at a stable
public URL and having every user's client identify as it. A locally installed
CLI has no such URL, and pointing every install at one address would make every
user's authorization look like the same client. Dynamic registration is the
right shape for software that runs on someone's own machine.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CLIENT_NAME = "Andromeda CLI"
CLIENT_URI = "https://ai-andromeda.com"

# The browser half of the flow. Long enough to sign in, create an account and
# read a consent screen; short enough that an abandoned attempt does not hold a
# listener open all afternoon.
AUTHORIZE_TIMEOUT = 300.0

# Everything else is a machine talking to a machine.
HTTP_TIMEOUT = 30.0

# Refresh this far before expiry. A token that expires between the check and
# the request is a 401 the user sees for no reason, and clock skew between here
# and the authorization server is real.
REFRESH_SKEW = 60.0

SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class OAuthError(RuntimeError):
    """Anything that stops a server being reached. Carries what to do next."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class NeedsBrowser(OAuthError):
    """Authorization is required and there is nobody here to give it.

    Raised rather than blocking. A scheduled run at three in the morning must
    not sit on a loopback listener until it times out; it must say which
    command a person should run.
    """

    def __init__(self, server: str) -> None:
        super().__init__(
            f"The {server} MCP server needs you to sign in.",
            hint=f"Run `andromeda mcp login {server}` — it opens your browser.",
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def token_dir(home: Path) -> Path:
    return home / "mcp-auth"


def token_path(home: Path, server: str) -> Path:
    return token_dir(home) / f"{SAFE_NAME.sub('_', server) or 'server'}.json"


@dataclass
class Tokens:
    """What a completed flow leaves behind.

    `expires_at` is absolute rather than the `expires_in` the server returns:
    a duration is only meaningful next to the moment it was issued, and that
    moment is not in the file.
    """

    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""

    @property
    def valid(self) -> bool:
        if not self.access_token:
            return False
        if not self.expires_at:
            # No expiry given. Treated as valid — a 401 will say otherwise, and
            # guessing a lifetime would expire a token that was never going to.
            return True
        return time.time() < self.expires_at - REFRESH_SKEW

    @property
    def refreshable(self) -> bool:
        return bool(self.refresh_token)


@dataclass
class ClientRegistration:
    client_id: str = ""
    client_secret: str = ""
    registration_issued_at: float = 0.0


@dataclass
class Stored:
    tokens: Tokens = field(default_factory=Tokens)
    client: ClientRegistration = field(default_factory=ClientRegistration)
    # The redirect URI that was registered. It has to be sent identically on
    # the token request, and an authorization server is entitled to reject a
    # port that has moved since registration.
    redirect_uri: str = ""
    issuer: str = ""


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write 0600, atomically, with the directory 0700.

    The mode is set on the temporary file before the rename rather than on the
    final path after it: between a world-readable create and a chmod there is a
    window, and a credential file only has to be readable once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_suffix(".tmp")
    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)


def load(home: Path, server: str) -> Stored:
    raw = _read(token_path(home, server))
    tokens = raw.get("tokens") or {}
    client = raw.get("client") or {}
    stored = Stored(
        tokens=Tokens(
            access_token=str(tokens.get("access_token") or ""),
            refresh_token=str(tokens.get("refresh_token") or ""),
            token_type=str(tokens.get("token_type") or "Bearer"),
            expires_at=float(tokens.get("expires_at") or 0.0),
            scope=str(tokens.get("scope") or ""),
        ),
        client=ClientRegistration(
            client_id=str(client.get("client_id") or ""),
            client_secret=str(client.get("client_secret") or ""),
            registration_issued_at=float(client.get("registration_issued_at") or 0.0),
        ),
        redirect_uri=str(raw.get("redirect_uri") or ""),
        issuer=str(raw.get("issuer") or ""),
    )
    _register_for_redaction(stored)
    return stored


def save(home: Path, server: str, stored: Stored) -> None:
    _write(
        token_path(home, server),
        {
            "tokens": {
                "access_token": stored.tokens.access_token,
                "refresh_token": stored.tokens.refresh_token,
                "token_type": stored.tokens.token_type,
                "expires_at": stored.tokens.expires_at,
                "scope": stored.tokens.scope,
            },
            "client": {
                "client_id": stored.client.client_id,
                "client_secret": stored.client.client_secret,
                "registration_issued_at": stored.client.registration_issued_at,
            },
            "redirect_uri": stored.redirect_uri,
            "issuer": stored.issuer,
        },
    )
    _register_for_redaction(stored)


def forget(home: Path, server: str) -> bool:
    path = token_path(home, server)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _register_for_redaction(stored: Stored) -> None:
    """Mask these values in every tool result from now on.

    An MCP access token has no vendor prefix, so no pattern will ever catch it
    — and an MCP server echoing back the `Authorization` header it received is
    exactly the shape of accident this guards against. Imported here rather
    than at module scope: `andromeda_tools` must not depend on
    `andromeda_agent`, and this is the one place that would.
    """
    try:
        from andromeda_agent import redact
    except ImportError:  # pragma: no cover - only if the layering is broken
        return
    redact.register_known(stored.tokens.access_token, "mcp-token")
    redact.register_known(stored.tokens.refresh_token, "mcp-refresh")
    redact.register_known(stored.client.client_secret, "mcp-client-secret")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def canonical_resource(url: str) -> str:
    """The resource identifier this token should be bound to (RFC 8707).

    Scheme and host lowercased, the default port and any fragment dropped, the
    path kept. Sending a resource the authorization server does not recognise
    gets the request rejected; sending none at all gets a token that is valid
    at *every* server that trusts the same issuer, which is the confused-deputy
    problem the parameter exists to close.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (parts.scheme == "https" and parts.port == 443)
        or (parts.scheme == "http" and parts.port == 80)
    ):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def resource_metadata_url(response: httpx.Response, server_url: str) -> str:
    """Where the protected-resource document lives.

    The 401's `WWW-Authenticate` header is the authoritative answer. The
    well-known path is the fallback for servers that return a bare 401, which
    is common enough that refusing them would be refusing real servers.
    """
    header = response.headers.get("www-authenticate", "")
    found = re.search(r'resource_metadata="([^"]+)"', header)
    if found:
        return found.group(1)
    parts = urllib.parse.urlsplit(server_url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/.well-known/oauth-protected-resource", "", "")
    )


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def discover_issuer(client: httpx.Client, metadata_url: str, server_url: str) -> str:
    """The authorization server for this resource.

    A resource may list several; the first is taken. Choosing between them
    would need a preference nobody has expressed, and every one of them is by
    definition able to issue a token for this resource.
    """
    document = _get_json(client, metadata_url)
    servers = (document or {}).get("authorization_servers")
    if isinstance(servers, list) and servers:
        return str(servers[0]).rstrip("/")
    # No document, or one that names nobody. Some servers are their own
    # authorization server and publish only the RFC 8414 document.
    parts = urllib.parse.urlsplit(server_url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def discover_endpoints(client: httpx.Client, issuer: str) -> dict[str, Any]:
    """The authorization server's metadata (RFC 8414), or a raise.

    Four candidate locations, in the order the specifications prefer them. The
    path-suffixed forms matter for an issuer with a path component — a tenant
    on a shared host — where the well-known document sits under the tenant, not
    at the root.
    """
    parts = urllib.parse.urlsplit(issuer)
    path = parts.path.rstrip("/")
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    candidates = [
        f"{root}/.well-known/oauth-authorization-server{path}",
        f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
        f"{root}/.well-known/openid-configuration{path}",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]

    for candidate in candidates:
        document = _get_json(client, candidate)
        if document and document.get("authorization_endpoint"):
            return document

    raise OAuthError(
        f"{issuer} does not publish OAuth metadata.",
        hint=(
            "The server may not support OAuth. If it takes a static token, put "
            "it in `headers` in mcp.json instead."
        ),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_client(
    client: httpx.Client, metadata: dict[str, Any], redirect_uri: str
) -> ClientRegistration:
    """Dynamic client registration (RFC 7591).

    A public client: no secret is requested, because a CLI on someone's laptop
    cannot keep one — anything shipped in the binary or written beside the
    tokens is readable by whoever can read the tokens. PKCE is what makes the
    exchange safe without one, which is why `none` is the only token endpoint
    auth method offered here.
    """
    endpoint = str(metadata.get("registration_endpoint") or "")
    if not endpoint:
        raise OAuthError(
            "This server requires a client id and does not support registering one.",
            hint=(
                "Register manually with the provider, then put the id in "
                "mcp.json as `oauth.client_id`."
            ),
        )

    payload = {
        "client_name": CLIENT_NAME,
        "client_uri": CLIENT_URI,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    try:
        response = client.post(endpoint, json=payload, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not register with {endpoint}: {exc}") from exc

    if response.status_code not in (200, 201):
        raise OAuthError(
            f"Registration was refused ({response.status_code}): "
            f"{_error_detail(response)}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError("The registration endpoint returned invalid JSON.") from exc

    client_id = str(body.get("client_id") or "")
    if not client_id:
        raise OAuthError("The registration endpoint returned no client id.")

    return ClientRegistration(
        client_id=client_id,
        client_secret=str(body.get("client_secret") or ""),
        registration_issued_at=float(body.get("client_id_issued_at") or time.time()),
    )


def _error_detail(response: httpx.Response) -> str:
    """The `error_description` if there is one, else the body, bounded.

    OAuth errors are a JSON envelope with a genuinely useful message in it, and
    a raw body dump buries it under a page of HTML on the servers that answer
    with a page.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        detail = body.get("error_description") or body.get("error")
        if detail:
            return str(detail)[:200]
    return response.text[:200]


# ---------------------------------------------------------------------------
# PKCE and the browser leg
# ---------------------------------------------------------------------------


def make_verifier() -> str:
    """A PKCE code verifier: 43-128 characters of unreserved alphabet."""
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


_PAGE = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 margin:0;display:grid;place-items:center;min-height:100vh;background:#0b0b0c;color:#e4e4e7}}
 main{{max-width:34rem;padding:2.5rem;text-align:center}}
 h1{{font-size:1.25rem;font-weight:600;margin:0 0 .75rem}}
 p{{color:#a1a1aa;margin:.5rem 0}}
 code{{background:#18181b;padding:.15rem .4rem;border-radius:.25rem;color:#e4e4e7}}
</style>
<main><h1>{title}</h1>{body}</main>
"""


class _CallbackServer(http.server.HTTPServer):
    """One listener, one sign-in.

    Bound to 127.0.0.1 explicitly and never 0.0.0.0: on a shared network, a
    listener that will accept an authorization code from anywhere is a way to
    have somebody else's code redeemed against this machine's PKCE verifier.
    """

    allow_reuse_address = False

    expected_state: str = ""
    code: str = ""
    error: str = ""
    done: bool = False


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AndromedaCLI"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        server: _CallbackServer = self.server  # type: ignore[assignment]
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ("/", "/callback"):
            # A browser asks for /favicon.ico the moment a tab opens. Answering
            # it and carrying on is the difference between a clean wait and a
            # listener that stops on the first stray request.
            self._respond(404, b"not found", "text/plain")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if not secrets.compare_digest(state, server.expected_state):
            # Not necessarily an attack — a stale tab from an earlier attempt
            # looks the same. Either way it is not this authorization, so it is
            # refused without ending the wait.
            self._respond(400, b"unexpected state", "text/plain")
            return

        error = (params.get("error") or [""])[0]
        description = (params.get("error_description") or [""])[0]
        code = (params.get("code") or [""])[0]

        if error or not code:
            server.error = description or error or "the server returned no code"
            server.done = True
            self._respond(
                400,
                _PAGE.format(
                    title="Authorization did not finish",
                    body=f"<p>{_escape(server.error)}</p>"
                    "<p>Go back to your terminal and try again.</p>",
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        server.code = code
        server.done = True
        self._respond(
            200,
            _PAGE.format(
                title="Connected",
                body="<p>Andromeda can now use this server.</p>"
                "<p>Go back to your terminal — you can close this tab.</p>",
            ).encode("utf-8"),
            "text/html; charset=utf-8",
        )

    def log_message(self, *args: Any) -> None:
        """Silence. The terminal below is mid-flow; this must not draw on it."""

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def authorize(
    *,
    home: Path,
    server: str,
    server_url: str,
    config: dict[str, Any] | None = None,
    open_browser: bool = True,
    announce=None,
) -> Stored:
    """Run the whole flow and return what to store. Interactive by definition.

    Everything before the browser leg is done first, so a misconfigured server
    fails before a tab opens rather than after.
    """
    config = config or {}
    stored = load(home, server)

    with httpx.Client(follow_redirects=True) as client:
        issuer = str(config.get("issuer") or "")
        if not issuer:
            probe = _probe(client, server_url)
            issuer = discover_issuer(
                client, resource_metadata_url(probe, server_url), server_url
            )
        metadata = discover_endpoints(client, issuer)

        try:
            listener = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        except OSError as exc:
            raise OAuthError(f"Could not open a local listener: {exc}") from exc

        port = listener.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        registration = _client_for(client, config, stored, metadata, redirect_uri)

        verifier = make_verifier()
        state = secrets.token_urlsafe(24)
        listener.expected_state = state
        listener.timeout = 0.5

        query = {
            "response_type": "code",
            "client_id": registration.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge_for(verifier),
            "code_challenge_method": "S256",
            # Binds the token to this server. Without it the token is good at
            # every resource the issuer serves.
            "resource": canonical_resource(server_url),
        }
        scope = str(config.get("scope") or "").strip()
        if not scope:
            supported = metadata.get("scopes_supported")
            if isinstance(supported, list) and supported:
                scope = " ".join(str(item) for item in supported)
        if scope:
            query["scope"] = scope

        url = (
            str(metadata["authorization_endpoint"])
            + ("&" if "?" in str(metadata["authorization_endpoint"]) else "?")
            + urllib.parse.urlencode(query)
        )

        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a headless box raises all sorts
                opened = False
        if announce is not None:
            announce(url, opened)

        code = _wait_for_code(listener)

        tokens = _exchange(
            client,
            metadata,
            registration,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            resource=canonical_resource(server_url),
        )

    result = Stored(
        tokens=tokens, client=registration, redirect_uri=redirect_uri, issuer=issuer
    )
    save(home, server, result)
    return result


def _probe(client: httpx.Client, server_url: str) -> httpx.Response:
    """Ask the server, unauthenticated, so its 401 can name its guard."""
    try:
        return client.post(
            server_url,
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not reach {server_url}: {exc}") from exc


def _client_for(
    client: httpx.Client,
    config: dict[str, Any],
    stored: Stored,
    metadata: dict[str, Any],
    redirect_uri: str,
) -> ClientRegistration:
    """A configured client id, a stored one, or a newly registered one.

    A stored registration is reused across sign-ins: registering again on every
    authorization leaves a trail of dead clients on the provider's side, and
    some of them rate-limit it.
    """
    configured = str(config.get("client_id") or "")
    if configured:
        return ClientRegistration(
            client_id=configured,
            client_secret=str(config.get("client_secret") or ""),
        )
    if stored.client.client_id and stored.redirect_uri == redirect_uri:
        return stored.client
    return register_client(client, metadata, redirect_uri)


def _wait_for_code(listener: _CallbackServer) -> str:
    deadline = time.monotonic() + AUTHORIZE_TIMEOUT
    try:
        while not listener.done and time.monotonic() < deadline:
            listener.handle_request()
    except KeyboardInterrupt:
        raise OAuthError("Sign-in cancelled.") from None
    finally:
        try:
            listener.server_close()
        except OSError:
            pass

    if listener.error:
        raise OAuthError(f"Authorization failed: {listener.error}")
    if not listener.code:
        raise OAuthError(
            "Timed out waiting for the browser.",
            hint="Run the command again, and finish in the tab it opens.",
        )
    return listener.code


def _exchange(
    client: httpx.Client,
    metadata: dict[str, Any],
    registration: ClientRegistration,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    resource: str,
) -> Tokens:
    endpoint = str(metadata.get("token_endpoint") or "")
    if not endpoint:
        raise OAuthError("The authorization server publishes no token endpoint.")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        # Sent again, identically. It is not used to redirect anything at this
        # point — it is checked against the value the code was issued for.
        "redirect_uri": redirect_uri,
        "client_id": registration.client_id,
        "code_verifier": verifier,
        "resource": resource,
    }
    if registration.client_secret:
        data["client_secret"] = registration.client_secret

    return _token_request(client, endpoint, data)


def refresh(
    home: Path, server: str, server_url: str, stored: Stored
) -> Stored | None:
    """Trade the refresh token for a new access token, or return None.

    None rather than a raise: a refresh token that has been revoked is an
    ordinary end of life, and the caller's answer to it is to authorize again,
    not to fail the session.
    """
    if not stored.tokens.refreshable or not stored.issuer:
        return None

    with httpx.Client(follow_redirects=True) as client:
        try:
            metadata = discover_endpoints(client, stored.issuer)
        except OAuthError:
            return None
        endpoint = str(metadata.get("token_endpoint") or "")
        if not endpoint:
            return None

        data = {
            "grant_type": "refresh_token",
            "refresh_token": stored.tokens.refresh_token,
            "client_id": stored.client.client_id,
            "resource": canonical_resource(server_url),
        }
        if stored.client.client_secret:
            data["client_secret"] = stored.client.client_secret

        try:
            tokens = _token_request(client, endpoint, data)
        except OAuthError:
            return None

    # A server that rotates refresh tokens returns a new one; a server that
    # does not returns none, and dropping the old one would end the session at
    # the next expiry.
    if not tokens.refresh_token:
        tokens.refresh_token = stored.tokens.refresh_token

    result = Stored(
        tokens=tokens,
        client=stored.client,
        redirect_uri=stored.redirect_uri,
        issuer=stored.issuer,
    )
    save(home, server, result)
    return result


def _token_request(
    client: httpx.Client, endpoint: str, data: dict[str, str]
) -> Tokens:
    try:
        response = client.post(
            endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not reach the token endpoint: {exc}") from exc

    if response.status_code != 200:
        raise OAuthError(f"The token request failed: {_error_detail(response)}")

    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthError("The token endpoint returned invalid JSON.") from exc

    access = str(body.get("access_token") or "")
    if not access:
        raise OAuthError("The token endpoint returned no access token.")

    expires_in = body.get("expires_in")
    try:
        lifetime = float(expires_in) if expires_in is not None else 0.0
    except (TypeError, ValueError):
        lifetime = 0.0

    return Tokens(
        access_token=access,
        refresh_token=str(body.get("refresh_token") or ""),
        token_type=str(body.get("token_type") or "Bearer"),
        expires_at=time.time() + lifetime if lifetime > 0 else 0.0,
        scope=str(body.get("scope") or ""),
    )


# ---------------------------------------------------------------------------
# What the transport talks to
# ---------------------------------------------------------------------------


class Authorization:
    """The transport's view of all of the above: a header, and a retry.

    Deliberately narrow. The transport does not know what OAuth is — it asks
    for headers, and when it is refused it asks once whether that is worth
    retrying. Everything else stays here.
    """

    def __init__(
        self,
        home: Path,
        server: str,
        server_url: str,
        config: dict[str, Any] | None = None,
        *,
        interactive: bool = False,
    ) -> None:
        self.home = home
        self.server = server
        self.server_url = server_url
        self.config = config or {}
        self.interactive = interactive
        self.stored = load(home, server)
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        with self._lock:
            if not self.stored.tokens.valid and self.stored.tokens.refreshable:
                # Before expiry rather than after. A proactive refresh is one
                # request; a reactive one is a failed call, a refresh and a
                # retry, and the failed call is visible to the model.
                refreshed = refresh(
                    self.home, self.server, self.server_url, self.stored
                )
                if refreshed is not None:
                    self.stored = refreshed
            token = self.stored.tokens.access_token
        if not token:
            return {}
        return {"Authorization": f"{self.stored.tokens.token_type} {token}"}

    def signed_in(self) -> bool:
        return bool(self.stored.tokens.access_token)

    def handle_unauthorized(self) -> bool:
        """A 401 came back. Return whether the caller should try once more.

        One attempt, never a loop: a server that answers 401 to a freshly
        issued token is not going to accept the next one either, and retrying
        would spend the user's authorization on a spin.
        """
        with self._lock:
            if self.stored.tokens.refreshable:
                refreshed = refresh(
                    self.home, self.server, self.server_url, self.stored
                )
                if refreshed is not None:
                    self.stored = refreshed
                    return True

            if not self.interactive:
                raise NeedsBrowser(self.server)

            self.stored = authorize(
                home=self.home,
                server=self.server,
                server_url=self.server_url,
                config=self.config,
            )
            return True
