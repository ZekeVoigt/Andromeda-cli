"""Pairing this machine with an Andromeda account.

The CLI runs on the user's own machine with no browser session, so it cannot
hold a Clerk session JWT — those are short-lived and minted by a client the
terminal does not have. It authenticates the same way the gateway daemon does:
a device token issued once at pairing time, stored 0600, and sent as
`Authorization: Bearer` alongside `X-Device-Id`.

The pairing code is the credential that bootstraps it: six characters,
single-use, ten-minute expiry, and it only ever appears on a screen the user is
already signed in to.
"""

from __future__ import annotations

import platform
import socket
import uuid
from typing import Any

import httpx

from .. import config as config_module
from .. import output

PAIR_PATH = "/api/gateway/pair"
TIMEOUT = httpx.Timeout(20.0)


def _device_name() -> str:
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "unknown"
    return f"{host} (andromeda-cli)"


def login(code: str, *, base_url: str) -> int:
    code = code.strip().upper()
    if not code:
        output.fail(
            "A pairing code is required.",
            "Open Andromeda, generate a code, then run `andromeda auth login <code>`.",
        )
        return 2

    base = base_url.rstrip("/")
    existing = config_module.load_credentials()
    # Reuse the device id across re-pairings so the account keeps one row for
    # this machine instead of accumulating one per login.
    device_id = existing.device_id or f"cli-{uuid.uuid4()}"

    payload: dict[str, Any] = {
        "pairingCode": code,
        "deviceId": device_id,
        "deviceName": _device_name(),
        "platform": f"cli-{platform.system().lower()}",
    }

    try:
        response = httpx.post(f"{base}{PAIR_PATH}", json=payload, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        output.fail(f"Could not reach {base}: {exc}")
        return 1

    try:
        body = response.json()
    except ValueError:
        output.fail(f"{base} returned a non-JSON response (HTTP {response.status_code}).")
        return 1

    if not response.is_success or not body.get("success"):
        # The server's reasons (expired, already used, unknown) are safe to
        # pass through: the caller already holds a code.
        output.fail(str(body.get("error") or "Pairing failed."))
        return 1

    token = str(body.get("deviceToken") or "")
    user_id = str(body.get("userId") or "")
    if not token or not user_id:
        output.fail("Pairing succeeded but returned no device token.")
        return 1

    path = config_module.save_credentials(
        config_module.Credentials(
            device_token=token,
            device_id=device_id,
            user_id=user_id,
            base_url=base,
        )
    )
    output.ok(f"Paired with {base}")
    output.info(f"Credentials written to {path} (0600)")
    return 0


def status() -> int:
    credentials = config_module.load_credentials()
    if not credentials.paired:
        output.info("Not paired.")
        output.info("Run `andromeda auth login <code>` with a code from the app.")
        return 1
    output.ok("Paired")
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
    output.info("Nothing to do — this machine was not paired.")
    return 0
