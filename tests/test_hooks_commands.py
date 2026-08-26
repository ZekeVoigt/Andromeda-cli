"""`andromeda hooks` — list, test, revoke, doctor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from andromeda_agent import hooks, shell_hooks
from andromeda_cli import config as config_module
from andromeda_cli.commands import hooks_cmd


@pytest.fixture(autouse=True)
def clean_bus():
    hooks.reset()
    shell_hooks.reset_for_tests()
    yield
    hooks.reset()
    shell_hooks.reset_for_tests()


def script(tmp_path: Path, body: str, name: str = "hook.sh") -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def configure(block: dict) -> None:
    path = config_module.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"hooks": block}), encoding="utf-8")


def run(argv: list[str]) -> int:
    from andromeda_cli.__main__ import main

    return main(argv)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_says_so_when_nothing_is_configured(capsys):
    assert hooks_cmd.show_list() == 0
    assert "No hooks configured" in capsys.readouterr().out


def test_list_shows_an_unapproved_hook_as_inert(tmp_path, capsys):
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command, "matcher": "terminal"}]})

    hooks_cmd.show_list()

    out = capsys.readouterr().out
    assert "pre_tool_call" in out
    assert "not approved — will not fire" in out
    assert "matcher='terminal'" in out


def test_list_shows_an_approved_hook_with_its_date(tmp_path, capsys):
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    hooks_cmd.show_list()

    out = capsys.readouterr().out
    assert "allowed" in out
    assert "approved 2" in out


def test_list_warns_when_the_script_changed_after_approval(tmp_path, capsys):
    """Approval is for the script that was read, not for the path."""
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    import os
    import time

    later = time.time() + 120
    os.utime(command, (later, later))

    hooks_cmd.show_list()
    assert "changed after it was approved" in capsys.readouterr().out


def test_list_shows_fail_closed(tmp_path, capsys):
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command, "fail_closed": True}]})
    hooks_cmd.show_list()
    assert "fail_closed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_testing_an_unknown_event_fails(capsys):
    assert hooks_cmd.test("on_full_moon") == 2
    assert "No hook event named" in capsys.readouterr().err


def test_testing_an_event_with_no_hooks_says_so(capsys):
    assert hooks_cmd.test("pre_tool_call") == 0
    assert "No hooks configured for pre_tool_call" in capsys.readouterr().out


def test_testing_fires_the_hook_and_shows_the_directive(tmp_path, capsys):
    command = script(tmp_path, "echo '{\"action\":\"block\",\"message\":\"nope\"}'")
    configure({"pre_tool_call": [{"command": command}]})

    assert hooks_cmd.test("pre_tool_call") == 0

    out = capsys.readouterr().out
    assert '"action": "block"' in out
    assert "nope" in out


def test_testing_says_when_a_hook_contributed_nothing(tmp_path, capsys):
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command}]})
    hooks_cmd.test("pre_tool_call")
    assert "nothing passed to the agent" in capsys.readouterr().out


def test_testing_reports_a_failing_hook(tmp_path, capsys):
    configure({"pre_tool_call": [{"command": "/nonexistent/hook.sh"}]})
    assert hooks_cmd.test("pre_tool_call") == 1
    assert "command not found" in capsys.readouterr().out


def test_the_for_tool_flag_applies_the_matcher(tmp_path, capsys):
    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command, "matcher": "terminal"}]})

    hooks_cmd.test("pre_tool_call", for_tool="read_file")
    assert "No hooks configured" in capsys.readouterr().out

    hooks_cmd.test("pre_tool_call", for_tool="terminal")
    assert "firing 1 hook" in capsys.readouterr().out


def test_a_payload_file_is_merged_into_the_payload(tmp_path, capsys):
    command = script(
        tmp_path,
        "python3 -c \"import json,sys; print(json.load(sys.stdin)['tool_input']['command'])\"",
    )
    configure({"pre_tool_call": [{"command": command}]})
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"args": {"command": "custom-command"}}), encoding="utf-8")

    hooks_cmd.test("pre_tool_call", payload_file=str(payload))
    assert "custom-command" in capsys.readouterr().out


def test_a_payload_file_that_is_not_an_object_is_refused(tmp_path, capsys):
    configure({"pre_tool_call": [{"command": "/bin/true"}]})
    payload = tmp_path / "payload.json"
    payload.write_text("[1,2,3]", encoding="utf-8")
    assert hooks_cmd.test("pre_tool_call", payload_file=str(payload)) == 2
    assert "must hold a JSON object" in capsys.readouterr().err


def test_a_missing_payload_file_is_refused(capsys):
    assert hooks_cmd.test("pre_tool_call", payload_file="/nonexistent.json") == 2
    assert "Could not read" in capsys.readouterr().err


def test_every_event_has_a_synthetic_payload():
    """`hooks test <event>` has to work for every event that exists, or the
    command teaches people that some events are second-class."""
    assert set(hooks_cmd.PAYLOADS) == set(hooks.VALID_HOOKS)


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoking_an_unknown_command_says_so(capsys):
    assert hooks_cmd.revoke("/never/approved.sh") == 0
    assert "No approval on record" in capsys.readouterr().out


def test_revoking_removes_the_approval(tmp_path, capsys):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.revoke(command) == 0
    out = capsys.readouterr().out
    assert "Withdrew 1 approval" in out
    assert "until they restart" in out
    assert shell_hooks.is_allowlisted("pre_tool_call", command) is False


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_with_nothing_configured(capsys):
    assert hooks_cmd.doctor() == 0
    assert "nothing to check" in capsys.readouterr().out


def test_doctor_reports_a_missing_script(capsys):
    configure({"pre_tool_call": [{"command": "/nonexistent/hook.sh"}]})
    assert hooks_cmd.doctor() == 1
    assert "missing, or not executable" in capsys.readouterr().out


def test_doctor_does_not_run_an_unapproved_script(tmp_path, capsys):
    """The whole reason to run doctor on a config you just pulled is to see
    what is about to register. Running it would defeat that."""
    marker = tmp_path / "it-ran"
    command = script(tmp_path, f"touch {marker}")
    configure({"pre_tool_call": [{"command": command}]})

    hooks_cmd.doctor()

    assert not marker.exists()
    assert "not run: a hook is only exercised here once approved" in capsys.readouterr().out


def test_doctor_runs_an_approved_script(tmp_path, capsys):
    marker = tmp_path / "it-ran"
    command = script(tmp_path, f"touch {marker}")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.doctor() == 0

    assert marker.exists()
    out = capsys.readouterr().out
    assert "ran clean, said nothing" in out
    assert "an observer" in out


def test_doctor_flags_stdout_that_is_not_json(tmp_path, capsys):
    command = script(tmp_path, "echo hello there")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.doctor() == 1
    assert "stdout was not JSON" in capsys.readouterr().out


def test_doctor_flags_json_that_means_nothing_here(tmp_path, capsys):
    """The failure people spend an afternoon on: the script looks healthy from
    every angle except the one that matters."""
    command = script(tmp_path, "echo '{\"status\":\"fine\"}'")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.doctor() == 1
    out = capsys.readouterr().out
    assert "returned valid JSON" in out
    assert "nothing in it is a directive this event understands" in out


def test_doctor_accepts_a_working_directive(tmp_path, capsys):
    command = script(tmp_path, "echo '{\"action\":\"block\",\"message\":\"no\"}'")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.doctor() == 0
    assert "returned valid JSON" in capsys.readouterr().out


def test_doctor_reports_a_timeout(tmp_path, capsys):
    command = script(tmp_path, "sleep 5")
    configure({"pre_tool_call": [{"command": command, "timeout": 1}]})
    shell_hooks.record_approval("pre_tool_call", command)

    assert hooks_cmd.doctor() == 1
    assert "timed out" in capsys.readouterr().out


def test_doctor_reports_drift_and_still_runs_the_script(tmp_path, capsys):
    import os
    import time

    command = script(tmp_path, "true")
    configure({"pre_tool_call": [{"command": command}]})
    shell_hooks.record_approval("pre_tool_call", command)
    later = time.time() + 120
    os.utime(command, (later, later))

    assert hooks_cmd.doctor() == 1
    out = capsys.readouterr().out
    assert "changed after approval" in out
    assert "revoke and re-approve" in out


# ---------------------------------------------------------------------------
# through the real entry point
# ---------------------------------------------------------------------------


def test_the_verb_is_reachable_from_argv(capsys):
    assert run(["hooks", "list"]) == 0
    assert "No hooks configured" in capsys.readouterr().out


def test_a_bare_hooks_verb_lists(capsys):
    assert run(["hooks"]) == 0
    assert "No hooks configured" in capsys.readouterr().out


def test_doctor_is_reachable_from_argv(capsys):
    assert run(["hooks", "doctor"]) == 0
    assert "nothing to check" in capsys.readouterr().out


def test_revoke_is_reachable_from_argv(tmp_path, capsys):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("on_session_end", command)
    assert run(["hooks", "revoke", command]) == 0
    assert shell_hooks.load_allowlist()["approvals"] == []


def test_the_help_text_documents_the_config_shape():
    from andromeda_cli.__main__ import HOOKS_HELP

    for fragment in ("hooks:", "pre_tool_call", "fail_closed", "hook_event_name", "Exiting 2"):
        assert fragment in HOOKS_HELP


def test_hooks_auto_accept_is_a_real_setting():
    assert config_module.load()["hooks_auto_accept"] is False
