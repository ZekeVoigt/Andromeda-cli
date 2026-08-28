"""Refusing to go looking for somebody's secrets.

From a real session: asked to build a job that emails a summary, the agent
grepped the home directory for email-provider keys, found a live one in an old
recovery dump, used it, and copied it into a third party's environment. Every
individual step was permitted; the sequence was not something anybody agreed to.
"""

from __future__ import annotations

import pytest

from andromeda_tools.credential_guard import sweeps_for_credentials as sweeps


class TestTheSweepIsRefused:
    def test_the_exact_command_from_the_incident(self):
        assert sweeps(
            'grep -iE "resend|sendgrid|ses|smtp|mailgun|postmark|brevo" '
            "~/andromeda-env-local-RECOVERED-from-vscode.txt"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "grep -r API_KEY ~",
            "rg --hidden AWS_SECRET_ACCESS_KEY /Users/zeke",
            "grep -r GITHUB_TOKEN $HOME",
            "grep -r OPENAI_API_KEY ~/Downloads",
            "grep -r RESEND_API_KEY ~/.env",
            "grep -rn stripe ~/.aws/credentials",
            "find ~ -name '*.pem' -exec grep -l private_key {} +",
        ],
    )
    def test_hunting_outside_the_workspace(self, command):
        assert sweeps(command)

    def test_the_keychain_needs_no_search_tool(self):
        """Not a search, and no innocent reading in an agent's hands."""
        assert sweeps("security find-generic-password -s github -w")


class TestOrdinaryWorkIsUntouched:
    """A guard that fires on real work is one people turn off."""

    @pytest.mark.parametrize(
        "command",
        [
            "grep API_KEY .env",
            "grep -rn TODO src/",
            "cat .env",
            "npm test",
            'find . -name "*.py"',
            "grep -rn resend ./src",
            "rg STRIPE_SECRET_KEY .",
        ],
    )
    def test_inside_the_workspace(self, command):
        assert not sweeps(command)

    def test_a_project_that_happens_to_live_under_a_home_directory(self):
        """Depth is what separates a home directory from a project in one."""
        assert not sweeps("grep -rn API_KEY /Users/zeke/code/api/src")
        assert not sweeps("grep -rn password /Users/zeke/Desktop/proj/tests")

    def test_reading_a_file_you_were_pointed_at(self):
        """`cat`ting a file somebody named is not a sweep."""
        assert not sweeps("cat ~/andromeda-env-local-RECOVERED-from-vscode.txt")

    def test_an_empty_command(self):
        assert not sweeps("")
        assert not sweeps("   ")


class TestTheRefusalReachesTheTool:
    def test_the_terminal_refuses_before_running_anything(self, tmp_path):
        from andromeda_tools import Workspace
        from andromeda_tools.terminal import run_command

        result = run_command(Workspace(str(tmp_path)), "grep -r API_KEY ~")

        assert not result.ok
        assert "search for credentials" in result.content

    def test_the_refusal_says_what_to_do_instead(self, tmp_path):
        """A refusal with no route forward is one the model routes around."""
        from andromeda_tools import Workspace
        from andromeda_tools.terminal import run_command

        content = run_command(Workspace(str(tmp_path)), "grep -r API_KEY ~").content

        assert "connect_app" in content
        assert "ask the user" in content

    def test_background_commands_are_guarded_too(self, tmp_path):
        from andromeda_tools import Workspace
        from andromeda_tools.terminal import run_command

        result = run_command(
            Workspace(str(tmp_path)), "grep -r API_KEY ~", background=True
        )
        assert not result.ok
        assert "search for credentials" in result.content

    def test_real_commands_still_run(self, tmp_path):
        from andromeda_tools import Workspace
        from andromeda_tools.terminal import run_command

        result = run_command(Workspace(str(tmp_path)), "echo hello")
        assert result.ok
        assert "hello" in result.content
