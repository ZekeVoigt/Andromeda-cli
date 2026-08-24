from __future__ import annotations

import pytest

from andromeda_cli.__main__ import build_command_parser, build_parser, main


def test_bare_prompt_is_positional():
    args = build_parser().parse_args(["what is 2+2"])
    assert args.prompt == "what is 2+2"


def test_a_prompt_that_looks_like_a_command_is_still_a_prompt():
    """`andromeda "config the router"` must not be swallowed by the verb."""
    args = build_parser().parse_args(["config the router for me"])
    assert args.prompt == "config the router for me"


def test_flags_override_the_lane():
    args = build_parser().parse_args(["--provider", "direct", "--model", "x/y", "hi"])
    assert args.provider == "direct"
    assert args.model == "x/y"


def test_auth_login_parses():
    args = build_command_parser().parse_args(["auth", "login", "ABC123"])
    assert (args.command, args.auth_command, args.code) == ("auth", "login", "ABC123")


def test_config_set_parses():
    args = build_command_parser().parse_args(["config", "set", "model", "a/b"])
    assert (args.key, args.value) == ("model", "a/b")


def test_setting_an_unserved_model_is_a_usage_error():
    assert main(["config", "set", "model", "openai/gpt-4o"]) == 2


def test_a_verb_with_no_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as caught:
        build_command_parser().parse_args(["auth"])
    assert caught.value.code == 2


def test_config_get_all_succeeds(capsys):
    assert main(["config", "get"]) == 0
    assert "provider" in capsys.readouterr().out


def test_config_get_unknown_key_is_a_usage_error():
    assert main(["config", "get", "nope"]) == 2


def test_config_set_then_get_round_trips(capsys):
    assert main(["config", "set", "temperature", "0.25"]) == 0
    capsys.readouterr()
    assert main(["config", "get", "temperature"]) == 0
    assert capsys.readouterr().out.strip() == "0.25"


def test_unpaired_relay_run_exits_nonzero(capsys):
    assert main(["hello"]) == 1
    assert "not paired" in capsys.readouterr().err.lower()


def test_tools_listing_succeeds(capsys):
    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "terminal" in out and "read_file" in out


def test_tools_enable_disable_round_trips(capsys):
    assert main(["tools", "disable", "terminal"]) == 0
    capsys.readouterr()
    from andromeda_cli import config as config_module

    assert "terminal" not in config_module.load()["enabled_tools"]

    assert main(["tools", "enable", "terminal"]) == 0
    assert "terminal" in config_module.load()["enabled_tools"]


def test_disabling_an_unknown_tool_is_a_usage_error():
    assert main(["tools", "disable", "teleport"]) == 2


def test_model_command_shows_and_sets(capsys):
    from andromeda_agent.models import ALLOWED_MODEL_IDS

    served = ALLOWED_MODEL_IDS[0]
    assert main(["model", served]) == 0
    capsys.readouterr()
    assert main(["model"]) == 0
    assert capsys.readouterr().out.strip() == served


def test_the_model_command_refuses_an_unserved_model(capsys):
    assert main(["model", "anthropic/claude-3"]) == 2
    assert "serves" in capsys.readouterr().err


def test_approval_flag_reaches_the_policy():
    from andromeda_cli.__main__ import _config
    from andromeda_cli.session import build_policy

    args = build_parser().parse_args(["--approval", "auto", "hi"])
    policy = build_policy(_config(args), interactive=True)
    assert policy.mode == "auto"


def test_a_non_interactive_ask_session_is_narrowed_not_left_to_fail():
    """Nobody can answer a prompt in a pipe, so gated tools are not offered."""
    from andromeda_cli import config as config_module
    from andromeda_cli.session import build_policy

    config = config_module.load()
    config["approval_mode"] = "ask"
    assert build_policy(config, interactive=False).max_tier == "safe_local"
    assert build_policy(config, interactive=True).max_tier == "destructive"


def test_an_explicit_auto_is_not_narrowed():
    from andromeda_cli import config as config_module
    from andromeda_cli.session import build_policy

    config = config_module.load()
    config["approval_mode"] = "auto"
    assert build_policy(config, interactive=False).max_tier == "destructive"


def test_an_invalid_approval_mode_in_config_is_rejected():
    from andromeda_cli import config as config_module

    with pytest.raises(config_module.ConfigError):
        config_module.set_value("approval_mode", "sk")


def test_sessions_list_when_empty(capsys):
    assert main(["sessions"]) == 0
    assert "No saved sessions" in capsys.readouterr().out


def test_sessions_show_unknown_is_a_usage_error():
    assert main(["sessions", "show", "nope"]) == 2


def test_sessions_search_with_no_hits_exits_nonzero():
    assert main(["sessions", "search", "kubernetes"]) == 1


def test_sessions_commands_parse():
    args = build_command_parser().parse_args(["sessions", "show", "abc"])
    assert (args.command, args.sessions_command, args.id) == ("sessions", "show", "abc")


def test_resume_and_continue_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--resume", "abc", "--continue"])


def test_resume_with_no_such_session_is_a_usage_error(capsys):
    assert main(["--resume", "nope"]) == 2
    assert "No session matching" in capsys.readouterr().err


def test_continue_with_no_sessions_is_a_usage_error(capsys):
    assert main(["--continue"]) == 2
    assert "No saved sessions" in capsys.readouterr().err


def test_resume_with_a_prompt_is_refused_rather_than_ignored(capsys):
    """Silently dropping the flag would grow a transcript nobody is watching."""
    from andromeda_cli import sessions as store

    session = store.Session()
    session.messages = [{"role": "user", "content": "earlier"}]
    session.save()

    assert main(["--resume", session.id, "a new prompt"]) == 2
    assert "interactive" in capsys.readouterr().err


def test_sessions_list_shows_a_saved_session(capsys):
    from andromeda_cli import sessions as store

    session = store.Session()
    session.messages = [{"role": "user", "content": "build the harness"}]
    session.save()

    assert main(["sessions"]) == 0
    assert "build the harness" in capsys.readouterr().out


def test_the_tools_listing_matches_a_real_session(tmp_path):
    """A listing that omits tools the agent has is worse than none."""
    from andromeda_cli import config as config_module
    from andromeda_cli.commands import tools as tools_cmd
    from andromeda_cli.session import build_conversation
    from support import ScriptedProvider

    config = config_module.load()
    config["approval_mode"] = "auto"
    # A client stands in for a real provider's: the auxiliary model borrows it,
    # and without one the session honestly has no vision tool while the listing
    # — which describes what a real session gets — does.
    provider = ScriptedProvider(script=["ok"], client=object())
    conversation, _ = build_conversation(
        config, provider, interactive=True, workspace_root=str(tmp_path)
    )

    assert set(tools_cmd._registry()) == set(conversation.registry)


class TestTheReportedVersion:
    """What `--version` says must be the version that was released.

    It drifted the moment there were two places to write it: `pyproject.toml`
    reached 0.1.2 while the package literal still said 0.1.0, so a freshly
    installed CLI reported a release that was two behind. `doctor` prints it
    too, so every bug report would have named the wrong version — which is the
    one field in a report you cannot afford to be quietly wrong.
    """

    def test_it_matches_the_packaging_metadata(self):
        import re
        from pathlib import Path

        import andromeda_cli

        pyproject = Path(andromeda_cli.__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():
            pytest.skip("no pyproject beside the package")

        declared = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
        )
        assert declared, "pyproject.toml has no version"
        assert andromeda_cli.__version__ == declared.group(1), (
            f"the package reports {andromeda_cli.__version__} but pyproject.toml says "
            f"{declared.group(1)}.\n"
            "If you just bumped the version, the editable install's metadata is stale — "
            "it is only refreshed on install:\n"
            "    uv pip install --python .venv/bin/python -e ."
        )

    def test_it_is_not_a_hardcoded_literal(self):
        # The mechanism, not just the value: a literal would satisfy the check
        # above on the day it was written and drift again on the next release.
        from pathlib import Path

        import andromeda_cli

        source = Path(andromeda_cli.__file__).read_text(encoding="utf-8")
        assert "importlib.metadata" in source
