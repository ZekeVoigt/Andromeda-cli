from __future__ import annotations

import json

import httpx
import respx

from andromeda_cli import config as config_module
from andromeda_cli.commands import auth

BASE = "https://andromeda.test"
PAIR = f"{BASE}/api/gateway/pair"


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
