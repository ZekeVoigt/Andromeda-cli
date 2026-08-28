"""Signing this machine in to an Andromeda account.

The CLI runs on the user's own machine with no browser session, so it cannot
hold a Clerk session JWT — those are short-lived and minted by a client the
terminal does not have. It authenticates the same way the gateway daemon does:
a device token issued once at pairing time, stored 0600, and sent as
`Authorization: Bearer` alongside `X-Device-Id`.

Two ways to get that token, and the order matters.

**The browser.** `andromeda auth login` opens ai-andromeda.com, the person
signs in (or signs up) on a page that already knows how to do that, and the
website hands the code straight back to a loopback listener this process
opened first. Nothing is typed and nothing is read off a screen — which is the
whole point, because the alternative is a person copying six characters
between two windows and getting one of them wrong.

The listener is bound to 127.0.0.1 on an ephemeral port, so nothing off this
machine can reach it, and the `state` value is minted here and checked on the
way back: a request that does not carry it is ignored rather than answered.

**The code.** `andromeda auth login <code>` still works, unchanged, for a
machine with no browser — an ssh session, a container, a server. The website
prints the code on a page instead of redirecting when it has nowhere local to
redirect to.

The pairing code is the credential that bootstraps either path: six
characters, single-use, ten-minute expiry, and it only ever appears on a
screen the user is already signed in to.
"""

from __future__ import annotations

import http.server
import platform
import secrets
import socket
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config as config_module
from .. import output

PAIR_PATH = "/api/gateway/pair"
#: Where the website signs someone in and hands the pairing code back.
BROWSER_PATH = "/cli/auth"
#: Where a signed-in person manages their plan. The success page ends here,
#: because upgrading is the one thing the terminal cannot do for them.
SETTINGS_PATH = "/settings"
TIMEOUT = httpx.Timeout(20.0)
#: How long the loopback listener waits. Long enough to create an account,
#: verify an email and pick a plan; short enough that a forgotten terminal
#: does not hold a socket open all day.
WAIT_SECONDS = 300.0


def upgrade_url() -> str:
    """Where a person changes their plan.

    One function rather than the string in two places, because the REPL and the
    full-screen surface both offer `/upgrade` and a terminal that sends two
    people to two different pages is worse than one that offers neither.
    """
    base = str(config_module.load().get("base_url") or "").rstrip("/")
    return f"{base}{SETTINGS_PATH}"


def open_upgrade(announce=None) -> tuple[str, bool]:
    """Send them to the browser to upgrade. Returns the url and whether it opened.

    Never raises and never blocks. Unlike pairing there is nothing to wait for:
    the plan change lands in Convex and the next reply's headers carry the new
    balance, so the terminal has no reason to hold a socket open — and a
    headless box, an SSH session or a locked-down browser must still get the
    link printed rather than a stack trace.
    """
    url = upgrade_url()
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a headless box raises all sorts
        opened = False
    if announce is not None:
        announce(url, opened)
    return url, opened


def _device_name() -> str:
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "unknown"
    return f"{host} (andromeda-cli)"


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    ok: bool
    error: str = ""
    user_id: str = ""


def _pair(code: str, *, base_url: str) -> PairResult:
    """Trade a pairing code for a device token and write it to disk.

    Shared by both lanes so there is exactly one place that decides what a
    successful pairing is and exactly one place that writes credentials.
    """
    base = base_url.rstrip("/")
    existing = config_module.load_credentials()
    # Reuse the device id across re-pairings so the account keeps one row for
    # this machine instead of accumulating one per login.
    device_id = existing.device_id or f"cli-{uuid.uuid4()}"

    payload: dict[str, Any] = {
        "pairingCode": code.strip().upper(),
        "deviceId": device_id,
        "deviceName": _device_name(),
        "platform": f"cli-{platform.system().lower()}",
    }

    try:
        response = httpx.post(f"{base}{PAIR_PATH}", json=payload, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return PairResult(False, f"Could not reach {base}: {exc}")

    try:
        body = response.json()
    except ValueError:
        return PairResult(
            False, f"{base} returned a non-JSON response (HTTP {response.status_code})."
        )

    if not response.is_success or not body.get("success"):
        # The server's reasons (expired, already used, unknown) are safe to
        # pass through: the caller already holds a code.
        return PairResult(False, str(body.get("error") or "Pairing failed."))

    token = str(body.get("deviceToken") or "")
    user_id = str(body.get("userId") or "")
    if not token or not user_id:
        return PairResult(False, "Pairing succeeded but returned no device token.")

    config_module.save_credentials(
        config_module.Credentials(
            device_token=token,
            device_id=device_id,
            user_id=user_id,
            base_url=base,
        )
    )
    return PairResult(True, user_id=user_id)


# ---------------------------------------------------------------------------
# The page the browser lands on
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Andromeda</title>
{refresh}
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #09090b; color: #d4d4d8; padding: 32px;
    font: 15px/1.6 ui-sans-serif, -apple-system, "SF Pro Text", Inter, system-ui, sans-serif;
  }}
  main {{ max-width: 420px; width: 100%; text-align: left; }}
  .eyebrow {{
    font-size: 11px; letter-spacing: .34em; text-transform: uppercase;
    color: #71717a; margin: 0 0 28px;
  }}
  h1 {{ font-size: 26px; line-height: 1.25; font-weight: 600; color: #fafafa; margin: 0 0 12px; letter-spacing: -.01em; }}
  p {{ color: #a1a1aa; margin: 0 0 12px; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px;
    background: #18181b; border: 1px solid #27272a; border-radius: 6px; padding: 2px 6px; color: #e4e4e7;
  }}
  .actions {{ margin-top: 28px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  a.button {{
    display: inline-block; text-decoration: none; font-size: 14px; font-weight: 500;
    color: #09090b; background: #fafafa; border-radius: 8px; padding: 10px 18px;
  }}
  a.button:hover {{ background: #ffffff; }}
  .note {{ font-size: 13px; color: #52525b; margin-top: 24px; }}
  .bad h1 {{ color: #f2777a; }}
</style>
</head><body class="{tone}"><main>
  <p class="eyebrow">A N D R O M E D A</p>
  <h1>{title}</h1>
  {body}
  <div class="actions"><a class="button" href="{settings}">Go to your account</a></div>
  <p class="note">{note}</p>
</main></body></html>
"""


def _page(
    *, title: str, body: str, settings_url: str, note: str, redirect: bool, tone: str = ""
) -> bytes:
    # The redirect is a meta refresh rather than script: this page is served
    # by a listener that is about to shut down, and a person who lands here
    # with script disabled must still be able to reach their account.
    refresh = (
        f'<meta http-equiv="refresh" content="6;url={settings_url}">' if redirect else ""
    )
    return _PAGE.format(
        title=title, body=body, settings=settings_url, note=note, refresh=refresh, tone=tone
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# The loopback listener
# ---------------------------------------------------------------------------


class _CallbackServer(http.server.HTTPServer):
    """A single-purpose listener that lives for one sign-in.

    Bound to 127.0.0.1 explicitly, never 0.0.0.0: on a laptop sharing a café
    network, a listener that accepts a pairing code from anywhere is a way to
    hand somebody else's code to this machine.
    """

    allow_reuse_address = False

    expected_state: str = ""
    base_url: str = ""
    result: PairResult | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AndromedaCLI"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        server: _CallbackServer = self.server  # type: ignore[assignment]
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ("/", "/callback"):
            # Browsers ask for /favicon.ico the moment a tab opens. Answering
            # it 404 and carrying on is the difference between a clean wait and
            # a listener that stops on the first stray request.
            self._respond(404, b"not found", "text/plain")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if state != server.expected_state:
            # Not necessarily an attack — a stale tab from an earlier attempt
            # looks exactly like this. Either way it is not this sign-in, so it
            # is refused without ending the wait.
            self._respond(400, b"unexpected state", "text/plain")
            return

        error = (params.get("error") or [""])[0]
        code = (params.get("code") or [""])[0]
        settings = f"{server.base_url.rstrip('/')}{SETTINGS_PATH}"

        if error or not code:
            message = error or "The website did not return a pairing code."
            server.result = PairResult(False, message)
            self._respond(
                400,
                _page(
                    title="Sign-in did not finish",
                    body=f"<p>{_escape(message)}</p>"
                    "<p>Run <code>andromeda auth login</code> in your terminal to try again.</p>",
                    settings_url=settings,
                    note="You can close this tab.",
                    redirect=False,
                    tone="bad",
                ),
                "text/html; charset=utf-8",
            )
            return

        # Exchanged here, inline, rather than after the response: the page has
        # to say whether pairing actually worked, and it cannot say that before
        # it has happened.
        result = _pair(code, base_url=server.base_url)
        server.result = result

        if not result.ok:
            self._respond(
                400,
                _page(
                    title="Sign-in did not finish",
                    body=f"<p>{_escape(result.error)}</p>"
                    "<p>Run <code>andromeda auth login</code> in your terminal to try again.</p>",
                    settings_url=settings,
                    note="You can close this tab.",
                    redirect=False,
                    tone="bad",
                ),
                "text/html; charset=utf-8",
            )
            return

        self._respond(
            200,
            _page(
                title="This machine is signed in",
                body="<p>Andromeda is connected in your terminal. "
                "Go back to it and keep going — nothing else to do here.</p>"
                "<p>Your account page is where you change plan, add credit and "
                "see every machine you have signed in.</p>",
                settings_url=settings,
                note="Taking you to your account in a moment. You can close this tab instead.",
                redirect=True,
            ),
            "text/html; charset=utf-8",
        )

    def log_message(self, *args: Any) -> None:
        """Silence. The terminal below is mid-wizard; this must not draw on it."""


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
# Commands
# ---------------------------------------------------------------------------


def _browser_sign_in_available(base: str) -> bool:
    """Whether this server has the browser sign-in route at all.

    Asked with no parameters, so nothing is minted: the route refuses a request
    with no `state`, and a refusal is proof it exists. A deployment that
    predates it 404s, and without this check the CLI would open a browser onto
    that 404 and then wait five minutes for a redirect that cannot come.

    Anything other than a clear 404 is treated as available. A probe that fails
    for its own reasons — a proxy, a captive portal, a slow link — must not be
    what stops someone signing in.
    """
    try:
        response = httpx.get(
            f"{base}{BROWSER_PATH}", timeout=httpx.Timeout(8.0), follow_redirects=False
        )
    except httpx.HTTPError:
        return True
    return response.status_code != 404


def browser_login(
    *,
    base_url: str,
    open_browser: bool = True,
    wait_seconds: float = WAIT_SECONDS,
    announce=None,
) -> PairResult:
    """Sign in through the website and pair from the redirect it sends back.

    Returns the result rather than an exit code: the wizard shows it as one
    step of four, and the standalone command turns it into a process status.
    `announce` lets the caller draw the URL in its own voice.
    """
    base = base_url.rstrip("/")

    if not _browser_sign_in_available(base):
        return PairResult(
            False,
            f"{base} does not support browser sign-in yet. "
            "Get a code from your account page and run `andromeda auth login <code>`.",
        )

    state = secrets.token_urlsafe(24)

    try:
        server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    except OSError as exc:
        return PairResult(False, f"Could not open a local listener: {exc}")

    server.expected_state = state
    server.base_url = base
    server.timeout = 0.5

    port = server.server_address[1]
    query = urllib.parse.urlencode(
        {"state": state, "port": str(port), "device": _device_name()}
    )
    url = f"{base}{BROWSER_PATH}?{query}"

    opened = False
    if open_browser:
        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box raises all sorts
            opened = False

    if announce is not None:
        announce(url, opened)
    else:
        if opened:
            output.info("Opened your browser to finish signing in.")
        else:
            output.info("Open this in a browser to finish signing in:")
        output.info(f"  {url}")

    deadline = time.monotonic() + wait_seconds
    try:
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        server.server_close()
        return PairResult(False, "Sign-in cancelled.")
    finally:
        try:
            server.server_close()
        except OSError:
            pass

    if server.result is None:
        return PairResult(
            False,
            "Timed out waiting for the browser. "
            "Run `andromeda auth login` again, or `andromeda auth login <code>` with a code from your account page.",
        )
    return server.result


def login(code: str | None = None, *, base_url: str, open_browser: bool = True) -> int:
    """`andromeda auth login [code]`.

    With no code this is the browser flow. With one it is the manual flow, for
    a machine that has no browser to open.
    """
    if code is not None and not code.strip():
        # An empty argument is a typo, not a request to open a browser. Opening
        # one anyway would answer a mistake by taking over the screen for five
        # minutes.
        output.fail(
            "A pairing code is required.",
            "Run `andromeda auth login` with no code at all to sign in through your browser.",
        )
        return 2

    if code is None:
        result = browser_login(base_url=base_url, open_browser=open_browser)
        if not result.ok:
            output.fail(result.error)
            return 1
        output.ok(f"Signed in. This machine is paired with {base_url.rstrip('/')}")
        output.info(f"Credentials written to {config_module.credentials_path()} (0600)")
        return 0

    result = _pair(code, base_url=base_url)
    if not result.ok:
        output.fail(result.error)
        return 1
    output.ok(f"Paired with {base_url.rstrip('/')}")
    output.info(f"Credentials written to {config_module.credentials_path()} (0600)")
    return 0


def status() -> int:
    credentials = config_module.load_credentials()
    if not credentials.paired:
        output.info("Not signed in.")
        output.info("Run `andromeda auth login` to sign in through your browser.")
        return 1
    output.ok("Signed in")
    output.info(f"  account   {credentials.user_id}")
    output.info(f"  device    {credentials.device_id}")
    output.info(f"  endpoint  {credentials.base_url}")
    # The token itself is never printed, by any command.
    return 0


def logout() -> int:
    if config_module.clear_credentials():
        output.ok("Signed out. The device token was deleted from this machine.")
        output.info("The device row remains on the account until it is removed there.")
        return 0
    output.info("Nothing to do — this machine was not signed in.")
    return 0
