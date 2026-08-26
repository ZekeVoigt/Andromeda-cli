"""Shell hooks: parsing, the wire protocol, failure semantics, consent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from andromeda_agent import hooks, shell_hooks


@pytest.fixture(autouse=True)
def clean_bus(monkeypatch):
    hooks.reset()
    shell_hooks.reset_for_tests()
    monkeypatch.delenv("ANDROMEDA_ACCEPT_HOOKS", raising=False)
    monkeypatch.delenv("ANDROMEDA_SAFE_MODE", raising=False)
    yield
    hooks.reset()
    shell_hooks.reset_for_tests()


def script(tmp_path: Path, body: str, name: str = "hook.sh") -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def spec(command: str, event: str = "pre_tool_call", **kwargs) -> shell_hooks.ShellHookSpec:
    return shell_hooks.ShellHookSpec(event=event, command=command, **kwargs)


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_a_minimal_entry_parses():
    parsed = shell_hooks.iter_configured_hooks(
        {"hooks": {"pre_tool_call": [{"command": "  /bin/true  "}]}}
    )
    assert len(parsed) == 1
    assert parsed[0].command == "/bin/true"
    assert parsed[0].timeout == shell_hooks.DEFAULT_TIMEOUT_SECONDS
    assert parsed[0].fail_closed is False


def test_no_hooks_block_is_no_hooks():
    assert shell_hooks.iter_configured_hooks({}) == []
    assert shell_hooks.iter_configured_hooks({"hooks": None}) == []
    assert shell_hooks.iter_configured_hooks(None) == []


def test_an_unknown_event_is_skipped_with_a_suggestion(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_calls": [{"command": "/bin/true"}]}}
        )
    assert parsed == []
    assert "did you mean 'pre_tool_call'" in caplog.text


def test_a_typo_with_no_near_match_lists_the_valid_events(caplog):
    with caplog.at_level("WARNING"):
        shell_hooks.iter_configured_hooks({"hooks": {"zzz": [{"command": "/bin/true"}]}})
    assert "valid:" in caplog.text


def test_an_entry_with_no_command_is_skipped(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_call": [{"matcher": "terminal"}, {"command": "   "}]}}
        )
    assert parsed == []
    assert "no non-empty 'command'" in caplog.text


def test_entries_must_be_a_list(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_call": {"command": "/bin/true"}}}
        )
    assert parsed == []
    assert "must be a list" in caplog.text


def test_a_non_mapping_entry_is_skipped(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_call": ["/bin/true"]}}
        )
    assert parsed == []
    assert "must be a mapping" in caplog.text


def test_the_hooks_block_must_be_a_mapping(caplog):
    with caplog.at_level("WARNING"):
        assert shell_hooks.iter_configured_hooks({"hooks": ["nope"]}) == []
    assert "must be a mapping" in caplog.text


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, shell_hooks.DEFAULT_TIMEOUT_SECONDS),
        (-5, shell_hooks.DEFAULT_TIMEOUT_SECONDS),
        ("nonsense", shell_hooks.DEFAULT_TIMEOUT_SECONDS),
        (10_000, shell_hooks.MAX_TIMEOUT_SECONDS),
        (30, 30),
        ("30", 30),
    ],
)
def test_timeouts_are_clamped(raw, expected):
    parsed = shell_hooks.iter_configured_hooks(
        {"hooks": {"pre_tool_call": [{"command": "/bin/true", "timeout": raw}]}}
    )
    assert parsed[0].timeout == expected


def test_fail_closed_accepts_the_other_spelling():
    parsed = shell_hooks.iter_configured_hooks(
        {"hooks": {"pre_tool_call": [{"command": "/bin/true", "failClosed": True}]}}
    )
    assert parsed[0].fail_closed is True


def test_the_canonical_spelling_of_fail_closed_wins():
    parsed = shell_hooks.iter_configured_hooks(
        {
            "hooks": {
                "pre_tool_call": [
                    {"command": "/bin/true", "fail_closed": False, "failClosed": True}
                ]
            }
        }
    )
    assert parsed[0].fail_closed is False


def test_a_non_boolean_fail_closed_fails_open(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_call": [{"command": "/bin/true", "fail_closed": "yes"}]}}
        )
    assert parsed[0].fail_closed is False
    assert "must be true or false" in caplog.text


def test_fail_closed_is_dropped_on_an_event_that_cannot_block(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"on_session_end": [{"command": "/bin/true", "fail_closed": True}]}}
        )
    assert parsed[0].fail_closed is False
    assert "only pre_tool_call can block" in caplog.text


def test_a_matcher_is_dropped_where_it_would_be_ignored(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"on_session_start": [{"command": "/bin/true", "matcher": "x"}]}}
        )
    assert parsed[0].matcher is None
    assert "is ignored" in caplog.text


def test_a_non_string_matcher_is_dropped(caplog):
    with caplog.at_level("WARNING"):
        parsed = shell_hooks.iter_configured_hooks(
            {"hooks": {"pre_tool_call": [{"command": "/bin/true", "matcher": 5}]}}
        )
    assert parsed[0].matcher is None
    assert "must be a string regex" in caplog.text


def test_whitespace_around_a_matcher_is_stripped():
    """YAML folding introduces it, and " terminal" would silently match
    nothing at all."""
    assert spec("/bin/true", matcher="  terminal  ").matches_tool("terminal")


def test_a_matcher_matches_the_whole_name():
    entry = spec("/bin/true", matcher="term")
    assert entry.matches_tool("term") is True
    assert entry.matches_tool("terminal") is False


def test_a_regex_matcher_works():
    entry = spec("/bin/true", matcher="browser_.*")
    assert entry.matches_tool("browser_click") is True
    assert entry.matches_tool("terminal") is False


def test_an_invalid_regex_falls_back_to_literal_equality(caplog):
    with caplog.at_level("WARNING"):
        entry = spec("/bin/true", matcher="[unclosed")
    assert entry.matches_tool("[unclosed") is True
    assert entry.matches_tool("terminal") is False
    assert "not a valid regex" in caplog.text


def test_no_matcher_matches_everything():
    assert spec("/bin/true").matches_tool("anything") is True
    assert spec("/bin/true").matches_tool(None) is True


def test_a_matcher_does_not_match_a_missing_tool_name():
    assert spec("/bin/true", matcher="terminal").matches_tool(None) is False


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------


def test_the_payload_has_the_documented_shape():
    payload = json.loads(
        shell_hooks.serialize_payload(
            "pre_tool_call",
            {
                "tool_name": "terminal",
                "args": {"command": "ls"},
                "session_id": "s1",
                "step": 2,
            },
        )
    )
    assert payload["hook_event_name"] == "pre_tool_call"
    assert payload["tool_name"] == "terminal"
    assert payload["tool_input"] == {"command": "ls"}
    assert payload["session_id"] == "s1"
    assert payload["cwd"] == str(Path.cwd())
    assert payload["extra"] == {"step": 2}


def test_a_parent_session_id_stands_in_for_a_missing_one():
    payload = json.loads(
        shell_hooks.serialize_payload("subagent_stop", {"parent_session_id": "p1"})
    )
    assert payload["session_id"] == "p1"


def test_unserialisable_values_are_stringified_rather_than_dropped():
    payload = json.loads(
        shell_hooks.serialize_payload("post_tool_call", {"thing": object()})
    )
    assert payload["extra"]["thing"].startswith("<object")


def test_a_hook_receives_the_payload_on_stdin(tmp_path):
    out = tmp_path / "seen.json"
    command = script(tmp_path, f"cat > {out}")
    shell_hooks.run_once(spec(command), {"tool_name": "terminal", "args": {"a": 1}})
    assert json.loads(out.read_text())["tool_input"] == {"a": 1}


def test_the_event_name_is_in_the_environment(tmp_path):
    out = tmp_path / "env.txt"
    command = script(tmp_path, f'printf "$ANDROMEDA_HOOK_EVENT" > {out}')
    shell_hooks.run_once(spec(command, event="on_session_end"), {})
    assert out.read_text() == "on_session_end"


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def test_a_block_in_the_canonical_shape(tmp_path):
    command = script(tmp_path, 'echo \'{"action":"block","message":"no"}\'')
    result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"] == {"action": "block", "message": "no"}


def test_a_block_in_the_other_shape_is_translated(tmp_path):
    """The single most important translation in the file: a script written for
    another harness must not silently do nothing."""
    command = script(tmp_path, 'echo \'{"decision":"block","reason":"no"}\'')
    result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"] == {"action": "block", "message": "no"}


def test_a_block_with_no_message_gets_the_default():
    assert shell_hooks.parse_response("pre_tool_call", '{"action":"block"}') == {
        "action": "block",
        "message": shell_hooks.DEFAULT_BLOCK_MESSAGE,
    }


def test_modify_in_both_shapes():
    canonical = shell_hooks.parse_response(
        "pre_tool_call", '{"action":"modify","args":{"command":"ls"}}'
    )
    other = shell_hooks.parse_response(
        "pre_tool_call", '{"decision":"modify","tool_input":{"command":"ls"}}'
    )
    assert canonical == other == {"action": "modify", "args": {"command": "ls"}}


def test_a_modify_without_a_dict_is_ignored():
    assert shell_hooks.parse_response("pre_tool_call", '{"action":"modify","args":"x"}') is None


def test_an_approve_keeps_its_rule_key():
    assert shell_hooks.parse_response(
        "pre_tool_call",
        '{"action":"approve","message":"ask me","rule_key":"terminal:push"}',
    ) == {"action": "approve", "message": "ask me", "rule_key": "terminal:push"}


def test_context_is_read_for_a_non_tool_event():
    assert shell_hooks.parse_response("pre_llm_call", '{"context":"today is friday"}') == {
        "context": "today is friday"
    }


def test_a_transform_returns_its_output():
    assert shell_hooks.parse_response("transform_llm_output", '{"output":"hi"}') == {
        "output": "hi"
    }


def test_a_transform_ignores_a_block_directive():
    """There is nothing to block on a transform event, and reading it as one
    would be a veto that no fire site honours."""
    assert shell_hooks.parse_response("transform_llm_output", '{"action":"block"}') is None


def test_non_json_stdout_is_ignored(caplog, tmp_path):
    command = script(tmp_path, "echo not json at all")
    with caplog.at_level("WARNING"):
        result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"] is None
    assert "not valid JSON" in caplog.text


def test_a_json_array_is_ignored():
    assert shell_hooks.parse_response("pre_tool_call", "[1,2,3]") is None


def test_empty_stdout_contributes_nothing(tmp_path):
    command = script(tmp_path, "true")
    assert shell_hooks.run_once(spec(command), {})["parsed"] is None


# ---------------------------------------------------------------------------
# exit codes and failure semantics
# ---------------------------------------------------------------------------


def test_exit_two_blocks_with_the_stderr_message(tmp_path):
    command = script(tmp_path, "echo 'not on main' >&2\nexit 2")
    result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"] == {"action": "block", "message": "not on main"}


def test_exit_two_with_no_output_blocks_with_the_default(tmp_path):
    command = script(tmp_path, "exit 2")
    result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"] == {
        "action": "block",
        "message": shell_hooks.DEFAULT_BLOCK_MESSAGE,
    }


def test_stdout_beats_stderr_for_the_block_message(tmp_path):
    command = script(
        tmp_path, "echo ignored >&2\necho '{\"action\":\"block\",\"message\":\"kept\"}'\nexit 2"
    )
    assert shell_hooks.run_once(spec(command), {})["parsed"]["message"] == "kept"


def test_exit_two_does_not_block_an_event_that_cannot_block(tmp_path):
    command = script(tmp_path, "echo whatever >&2\nexit 2")
    result = shell_hooks.run_once(spec(command, event="on_session_end"), {})
    assert result["parsed"] is None


def test_another_non_zero_exit_still_has_its_stdout_read(tmp_path, caplog):
    command = script(tmp_path, "echo '{\"action\":\"block\",\"message\":\"still read\"}'\nexit 7")
    with caplog.at_level("WARNING"):
        result = shell_hooks.run_once(spec(command), {})
    assert result["parsed"]["message"] == "still read"
    assert "exited 7" in caplog.text


def test_a_missing_command_fails_open(caplog):
    with caplog.at_level("WARNING"):
        result = shell_hooks.run_once(spec("/nonexistent/hook.sh"), {})
    assert result["error"] == "command not found"
    assert result["parsed"] is None


def test_a_command_that_cannot_be_parsed_fails_open():
    result = shell_hooks.run_once(spec('/bin/echo "unbalanced'), {})
    assert "cannot be parsed" in result["error"]


def test_an_empty_command_fails_open():
    """Config parsing rejects a blank command, so this is the defensive path —
    a spec built in code, or one that shell-splits away to nothing."""
    assert shell_hooks.run_once(spec("   "), {})["error"] == "empty command"
    # An argv of one empty string is a different failure, and also open.
    assert shell_hooks.run_once(spec("''"), {})["error"] == "command not executable"


def test_a_non_executable_script_fails_open(tmp_path):
    path = tmp_path / "not-exec.sh"
    path.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    path.chmod(0o644)
    result = shell_hooks.run_once(spec(str(path)), {})
    assert result["error"] == "command not executable"


def test_a_timeout_fails_open(tmp_path, caplog):
    command = script(tmp_path, "sleep 5")
    with caplog.at_level("WARNING"):
        result = shell_hooks.run_once(spec(command, timeout=1), {})
    assert result["timed_out"] is True
    assert result["parsed"] is None
    assert "timed out" in caplog.text


def test_fail_closed_turns_a_timeout_into_a_block(tmp_path):
    command = script(tmp_path, "sleep 5")
    result = shell_hooks.run_once(spec(command, timeout=1, fail_closed=True), {})
    assert result["parsed"]["action"] == "block"
    assert "timed out after 1s" in result["parsed"]["message"]


def test_fail_closed_turns_a_missing_script_into_a_block():
    result = shell_hooks.run_once(spec("/nonexistent/hook.sh", fail_closed=True), {})
    assert result["parsed"] == {
        "action": "block",
        "message": "hook /nonexistent/hook.sh failed closed: command not found",
    }


def test_fail_closed_blocks_on_garbage_stdout(tmp_path):
    """A gate that printed a stack trace has not allowed anything."""
    command = script(tmp_path, "echo 'Traceback (most recent call last):'")
    result = shell_hooks.run_once(spec(command, fail_closed=True), {})
    assert result["parsed"]["action"] == "block"
    assert "unparseable stdout" in result["parsed"]["message"]


def test_fail_closed_allows_silence(tmp_path):
    """Empty stdout is a hook that looked and found nothing wrong."""
    command = script(tmp_path, "true")
    assert shell_hooks.run_once(spec(command, fail_closed=True), {})["parsed"] is None


def test_fail_closed_allows_valid_json_that_says_nothing(tmp_path):
    command = script(tmp_path, "echo '{\"note\":\"looked, fine\"}'")
    assert shell_hooks.run_once(spec(command, fail_closed=True), {})["parsed"] is None


def test_a_timeout_kills_the_whole_process_tree(tmp_path):
    """A hook that forked helpers must not leave them running — they hold the
    write end of the pipe, so the drain would hang behind them too."""
    marker = tmp_path / "child-alive"
    child = script(
        tmp_path,
        f"sleep 30\ntouch {marker}",
        name="child.sh",
    )
    parent = script(tmp_path, f"{child} &\nsleep 30", name="parent.sh")

    started = time.monotonic()
    result = shell_hooks.run_once(spec(parent, timeout=1), {})
    elapsed = time.monotonic() - started

    assert result["timed_out"] is True
    # The call returns promptly rather than waiting on the orphan's pipe.
    assert elapsed < 10
    # And the descendant is gone, so the marker never lands.
    time.sleep(0.5)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# matcher gating at fire time
# ---------------------------------------------------------------------------


def test_the_callback_skips_a_tool_the_matcher_does_not_name(tmp_path):
    ran = tmp_path / "ran"
    command = script(tmp_path, f"touch {ran}")
    callback = shell_hooks.make_callback(spec(command, matcher="terminal"))

    assert callback(tool_name="read_file", args={}) is None
    assert not ran.exists()

    callback(tool_name="terminal", args={})
    assert ran.exists()


def test_a_matcher_does_not_gate_an_event_that_is_not_tool_scoped(tmp_path):
    ran = tmp_path / "ran"
    command = script(tmp_path, f"touch {ran}")
    entry = shell_hooks.ShellHookSpec(
        event="on_session_end", command=command, matcher="terminal"
    )
    shell_hooks.make_callback(entry)(session_id="s")
    assert ran.exists()


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------


def config_with(command: str, event: str = "pre_tool_call", **extra) -> dict:
    return {"hooks": {event: [{"command": command, **extra}]}}


def test_nothing_registers_without_approval(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    command = script(tmp_path, "true")
    with caplog.at_level("WARNING"):
        registered = shell_hooks.register_from_config(config_with(command))
    assert registered == []
    assert hooks.has_hook("pre_tool_call") is False
    assert "not allowlisted" in caplog.text


def test_the_accept_flag_registers_and_records(tmp_path):
    command = script(tmp_path, "true")
    registered = shell_hooks.register_from_config(config_with(command), accept_hooks=True)
    assert len(registered) == 1
    assert hooks.has_hook("pre_tool_call") is True
    entry = shell_hooks.entry_for("pre_tool_call", command)
    assert entry["approved_at"]
    assert entry["script_mtime_at_approval"]


def test_the_environment_variable_is_an_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROMEDA_ACCEPT_HOOKS", "1")
    command = script(tmp_path, "true")
    assert shell_hooks.register_from_config(config_with(command))


def test_the_config_setting_is_an_opt_in(tmp_path):
    command = script(tmp_path, "true")
    config = config_with(command)
    config["hooks_auto_accept"] = True
    assert shell_hooks.register_from_config(config)


@pytest.mark.parametrize("value", ["true", "YES", "on", "1"])
def test_the_config_setting_accepts_the_written_forms(value):
    assert shell_hooks.resolve_effective_accept({"hooks_auto_accept": value}, False)


def test_safe_mode_registers_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANDROMEDA_SAFE_MODE", "1")
    command = script(tmp_path, "true")
    with caplog.at_level("INFO"):
        assert shell_hooks.register_from_config(config_with(command), accept_hooks=True) == []
    assert hooks.has_hook("pre_tool_call") is False
    assert "SAFE_MODE" in caplog.text


def test_an_approved_pair_registers_without_asking_again(tmp_path):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("pre_tool_call", command)
    # No TTY, no flag, no env — and it still registers, because a person
    # already said yes to this exact pair.
    assert shell_hooks.register_from_config(config_with(command))


def test_approval_is_per_event_not_per_command(tmp_path):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("pre_tool_call", command)
    assert shell_hooks.is_allowlisted("pre_tool_call", command) is True
    assert shell_hooks.is_allowlisted("on_session_end", command) is False


def test_registration_is_idempotent(tmp_path):
    command = script(tmp_path, "true")
    config = config_with(command)
    shell_hooks.register_from_config(config, accept_hooks=True)
    second = shell_hooks.register_from_config(config, accept_hooks=True)
    assert second == []
    assert len(hooks.manager().callbacks("pre_tool_call")) == 1


def test_the_same_script_may_register_once_per_matcher(tmp_path):
    command = script(tmp_path, "true")
    config = {
        "hooks": {
            "pre_tool_call": [
                {"command": command, "matcher": "terminal"},
                {"command": command, "matcher": "write_file"},
            ]
        }
    }
    assert len(shell_hooks.register_from_config(config, accept_hooks=True)) == 2


def test_revoking_removes_every_event_for_that_command(tmp_path):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("pre_tool_call", command)
    shell_hooks.record_approval("on_session_end", command)
    assert shell_hooks.revoke(command) == 2
    assert shell_hooks.load_allowlist()["approvals"] == []


def test_revoking_something_unknown_removes_nothing():
    assert shell_hooks.revoke("/never/approved.sh") == 0


def test_recording_the_same_pair_twice_keeps_one_entry(tmp_path):
    command = script(tmp_path, "true")
    shell_hooks.record_approval("pre_tool_call", command)
    shell_hooks.record_approval("pre_tool_call", command)
    assert len(shell_hooks.load_allowlist()["approvals"]) == 1


def test_a_corrupt_allowlist_reads_as_empty(tmp_path):
    shell_hooks.allowlist_path().parent.mkdir(parents=True, exist_ok=True)
    shell_hooks.allowlist_path().write_text("{not json", encoding="utf-8")
    assert shell_hooks.load_allowlist() == {"approvals": []}


def test_an_allowlist_that_is_not_an_object_reads_as_empty():
    shell_hooks.allowlist_path().parent.mkdir(parents=True, exist_ok=True)
    shell_hooks.allowlist_path().write_text("[]", encoding="utf-8")
    assert shell_hooks.load_allowlist() == {"approvals": []}


def test_concurrent_approvals_do_not_lose_each_other(tmp_path):
    """Two terminals starting at once is the ordinary case. Without the lock
    the second write drops the first one's approval."""
    commands = [script(tmp_path, "true", name=f"h{index}.sh") for index in range(12)]
    barrier = threading.Barrier(len(commands))

    def record(command: str) -> None:
        barrier.wait()
        shell_hooks.record_approval("pre_tool_call", command)

    threads = [threading.Thread(target=record, args=(command,)) for command in commands]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = {entry["command"] for entry in shell_hooks.load_allowlist()["approvals"]}
    assert stored == set(commands)


def test_an_unwritable_allowlist_does_not_raise(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(shell_hooks.tempfile, "mkstemp", boom)
    with caplog.at_level("WARNING"):
        shell_hooks.save_allowlist({"approvals": []})
    assert "could not write the hook allowlist" in caplog.text


# ---------------------------------------------------------------------------
# inspecting a command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/opt/hooks/guard.sh", "/opt/hooks/guard.sh"),
        ("python3 /opt/hooks/guard.py", "/opt/hooks/guard.py"),
        ("/usr/bin/env bash /opt/hooks/guard.sh", "/opt/hooks/guard.sh"),
        ("~/hooks/guard.sh", "~/hooks/guard.sh"),
        ("mycommand", "mycommand"),
        ("node /opt/hooks/guard.mjs --flag", "/opt/hooks/guard.mjs"),
    ],
)
def test_the_script_inside_a_command_line_is_found(command, expected):
    assert shell_hooks.command_script_path(command) == expected


def test_a_bare_script_must_be_executable(tmp_path):
    path = tmp_path / "guard.sh"
    path.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    path.chmod(0o644)
    assert shell_hooks.script_is_executable(str(path)) is False
    path.chmod(0o755)
    assert shell_hooks.script_is_executable(str(path)) is True


def test_an_interpreted_script_only_has_to_be_readable(tmp_path):
    """`+x` is not required when an interpreter opens the file, and reporting
    a healthy hook as broken is how people learn to ignore doctor."""
    path = tmp_path / "guard.py"
    path.write_text("print()\n", encoding="utf-8")
    path.chmod(0o644)
    assert shell_hooks.script_is_executable(f"python3 {path}") is True


def test_a_missing_script_is_not_executable():
    assert shell_hooks.script_is_executable("/nonexistent/guard.sh") is False


def test_the_mtime_of_a_missing_script_is_unknown():
    assert shell_hooks.script_mtime_iso("/nonexistent/guard.sh") is None


def test_the_mtime_moves_when_the_script_is_edited(tmp_path):
    path = tmp_path / "guard.sh"
    path.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    first = shell_hooks.script_mtime_iso(str(path))
    os.utime(path, (time.time() + 60, time.time() + 60))
    assert shell_hooks.script_mtime_iso(str(path)) > first


def test_a_hook_does_not_inherit_this_process_stdin(tmp_path):
    """It gets the payload, not the terminal — a hook that read the user's
    keystrokes would eat the next prompt."""
    command = script(tmp_path, "read -r line\nprintf '%s' \"$line\" | head -c 20")
    result = shell_hooks.run_once(spec(command), {"tool_name": "terminal"})
    assert result["stdout"].startswith('{"hook_event_name"')


def test_the_run_reports_how_long_it_took(tmp_path):
    command = script(tmp_path, "true")
    assert shell_hooks.run_once(spec(command), {})["elapsed_seconds"] >= 0


def test_subprocess_is_never_given_a_shell(tmp_path):
    """`shell=False` is the reason a command string is not an injection point.
    A shell metacharacter has to be inert."""
    out = tmp_path / "created-by-shell"
    result = shell_hooks.run_once(spec(f"/bin/echo hi; touch {out}"), {})
    assert not out.exists()
    assert result["stdout"].strip() == f"hi; touch {out}"


def test_the_helper_and_the_live_callback_agree(tmp_path):
    """`hooks test` has to be the same code path as a real firing, or it
    becomes a second implementation that drifts."""
    command = script(tmp_path, 'echo \'{"action":"block","message":"same"}\'')
    entry = spec(command)
    live = shell_hooks.make_callback(entry)(tool_name="terminal", args={})
    assert live == shell_hooks.run_once(entry, {"tool_name": "terminal", "args": {}})["parsed"]


def test_a_python_hook_works_end_to_end(tmp_path):
    """The documented `python3 script.py` form, not just shell."""
    path = tmp_path / "guard.py"
    path.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        'print(json.dumps({"action": "block", "message": payload["tool_name"]}))\n',
        encoding="utf-8",
    )
    result = shell_hooks.run_once(
        spec(f"{sys.executable} {path}"), {"tool_name": "terminal"}
    )
    assert result["parsed"] == {"action": "block", "message": "terminal"}


def test_a_hook_that_detaches_a_daemon_keeps_it(tmp_path):
    """Only a *timed-out* hook has its tree reaped. `some-daemon &` from a
    hook that finishes is a deliberate thing to do."""
    marker = tmp_path / "daemon-ran"
    child = script(tmp_path, f"sleep 0.5\ntouch {marker}", name="child.sh")
    parent = script(tmp_path, f"{child} &\nexit 0", name="parent.sh")

    shell_hooks.run_once(spec(parent, timeout=5), {})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()


def test_a_hook_writing_more_than_a_pipe_buffer_does_not_deadlock(tmp_path):
    """64KB is where a naive read-after-wait hangs forever."""
    command = script(tmp_path, "head -c 200000 /dev/zero | tr '\\0' 'x'")
    result = shell_hooks.run_once(spec(command, timeout=20), {})
    assert len(result["stdout"]) == 200_000
    assert result["timed_out"] is False


def test_the_subprocess_module_is_still_the_one_being_used():
    # Guards against a refactor to `shell=True`, which would make every
    # configured command a shell injection point.
    source = Path(shell_hooks.__file__).read_text(encoding="utf-8")
    assert "shell=False" in source
    assert "shell=True" not in source
    assert subprocess.Popen is not None
