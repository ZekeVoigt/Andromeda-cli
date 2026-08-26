"""Credentials resolved from a vault rather than read from a file.

The `cmd://` scheme is what most of this is tested through, because it is the
one that can be driven deterministically without installing a password manager
— and because it exercises the same `run` / `_from_output` path every other
scheme uses. The scheme-specific tests then only have to prove that each one
builds the right argv.
"""

from __future__ import annotations

import os
import sys

import pytest

from andromeda_agent import redact, secrets


@pytest.fixture(autouse=True)
def clean():
    secrets.clear_cache()
    redact.clear_known()
    yield
    secrets.clear_cache()
    redact.clear_known()


def _echo(value: str) -> str:
    """A `cmd://` reference that prints `value` and nothing else."""
    return f"cmd://printf %s {value!r}"


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference,scheme",
    [
        ("op://Personal/Item/field", "op"),
        ("bw://abc-123", "bw"),
        ("keychain://service/account", "keychain"),
        ("cmd://echo hi", "cmd"),
        ("env://HOME", "env"),
        ("OP://Personal/Item/field", "op"),
    ],
)
def test_a_reference_is_recognised_by_its_scheme(reference, scheme):
    assert secrets.scheme_of(reference) == scheme
    assert secrets.is_reference(reference)


@pytest.mark.parametrize(
    "value",
    ["sk-abcdef", "https://example.com/key", "", "no-scheme", None, 42],
)
def test_a_plain_value_is_not_a_reference(value):
    """A config that says `https://…` means a URL.

    Treating an unknown scheme as a reference would fail with a message about
    vaults, which is a worse answer than using the value as written.
    """
    assert not secrets.is_reference(value)


def test_a_malformed_reference_says_what_the_shape_is():
    result = secrets.resolve("KEY", "not-a-reference")
    assert result.problem is secrets.Problem.BAD_REFERENCE
    assert "scheme://" in result.detail


def test_an_unknown_scheme_names_the_ones_that_exist():
    result = secrets.resolve("KEY", "vault://x")
    assert result.problem is secrets.Problem.BAD_REFERENCE
    assert "op" in result.remedy and "keychain" in result.remedy


@pytest.mark.parametrize("name", ["", "not a name", "9LEADING", "has-dash"])
def test_an_invalid_variable_name_is_refused(name):
    result = secrets.resolve(name, "env://HOME")
    assert result.problem is secrets.Problem.BAD_REFERENCE


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_a_command_reference_resolves_to_its_output():
    result = secrets.resolve("KEY", _echo("the-secret-value"))
    assert result.ok
    assert result.value == "the-secret-value"


def test_trailing_whitespace_is_stripped():
    """A helper that prints a newline has not printed a different secret."""
    result = secrets.resolve("KEY", "cmd://printf 'value\\n\\n'")
    assert result.value == "value"


def test_an_empty_result_is_a_failure_and_not_an_empty_secret():
    """An empty string would flow into an Authorization header — a guaranteed
    401 that looks like a rejected key rather than a missing one."""
    result = secrets.resolve("KEY", "cmd://true")
    assert not result.ok
    assert result.problem is secrets.Problem.EMPTY


def test_a_failing_helper_is_reported_not_raised():
    result = secrets.resolve("KEY", "cmd://exit 3")
    assert not result.ok
    assert result.problem is secrets.Problem.FAILED


def test_a_helper_that_hangs_times_out_rather_than_blocking(monkeypatch):
    monkeypatch.setattr(secrets, "TIMEOUT", 0.4)
    result = secrets.resolve("KEY", "cmd://sleep 5")
    assert result.problem is secrets.Problem.TIMEOUT


def test_a_helper_that_prompts_fails_instead_of_hanging():
    """stdin is /dev/null, so a vault that wants a master password gives up.

    A session that may have nobody watching it must not sit on a prompt.
    """
    result = secrets.resolve("KEY", "cmd://read -r line && echo $line")
    assert not result.ok


def test_an_env_reference_reads_the_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_VALUE", "from-the-shell")
    assert secrets.resolve("KEY", "env://SOURCE_VALUE").value == "from-the-shell"


def test_an_env_reference_to_nothing_is_not_found(monkeypatch):
    monkeypatch.delenv("ABSENT_VALUE", raising=False)
    assert secrets.resolve("KEY", "env://ABSENT_VALUE").problem is (
        secrets.Problem.NOT_FOUND
    )


# ---------------------------------------------------------------------------
# What the helper is given
# ---------------------------------------------------------------------------


def test_a_helper_does_not_inherit_the_credentials_this_process_holds(monkeypatch):
    """The command is the user's own config, which is the same trust as their
    shell profile. That trust does not extend to handing it every key the
    session is already carrying."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-must-not-be-visible")
    result = secrets.resolve("KEY", "cmd://printf %s \"${OPENROUTER_API_KEY:-clean}\"")
    assert result.value == "clean"


def test_a_helper_keeps_the_variables_it_needs_to_run(monkeypatch):
    result = secrets.resolve("KEY", "cmd://printf %s \"${HOME:-missing}\"")
    assert result.value not in ("", "missing")


def test_ansi_escapes_from_a_helper_never_reach_our_output():
    """A helper's diagnostics must not be able to draw on this terminal."""
    result = secrets.resolve("KEY", "cmd://printf '\\033[31mboom\\033[0m' >&2; exit 1")
    assert "\x1b" not in result.detail
    assert "boom" in result.detail


def test_a_missing_helper_is_named_with_how_to_install_it(monkeypatch):
    monkeypatch.setattr(secrets.shutil, "which", lambda name: None)
    result = secrets.resolve("KEY", "op://Personal/X/field")
    assert result.problem is secrets.Problem.NO_HELPER
    assert "1Password" in result.remedy
    assert "developer.1password.com" in result.remedy


def test_nothing_is_ever_installed(monkeypatch):
    """The remedy names the command; it never runs it."""
    calls = []
    monkeypatch.setattr(secrets, "run", lambda *a, **k: calls.append(a) or secrets.Output(False))
    monkeypatch.setattr(secrets.shutil, "which", lambda name: None)
    secrets.resolve("KEY", "op://Personal/X/field")
    assert calls == []


# ---------------------------------------------------------------------------
# Scheme argv
# ---------------------------------------------------------------------------


def test_onepassword_passes_the_reference_after_a_separator(monkeypatch):
    """A reference comes from a config file, and a config file can say
    `--session`. After `--` it is data."""
    seen = {}
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        secrets,
        "run",
        lambda argv, **kw: seen.update(argv=argv, kw=kw)
        or secrets.Output(True, stdout="v"),
    )
    secrets.resolve("KEY", "op://Personal/Item/field")
    assert seen["argv"] == [
        "op", "read", "--no-newline", "--", "op://Personal/Item/field"
    ]


def test_bitwarden_uses_the_secrets_manager_cli(monkeypatch):
    seen = {}
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        secrets,
        "run",
        lambda argv, **kw: seen.update(argv=argv, kw=kw)
        or secrets.Output(True, stdout="v"),
    )
    secrets.resolve("KEY", "bw://abc-123")
    assert seen["argv"][0] == "bws"
    assert "abc-123" in seen["argv"]
    assert seen["kw"]["allow"] == ("BWS_ACCESS_TOKEN",)


@pytest.mark.skipif(sys.platform != "darwin", reason="the keychain is macOS only")
def test_the_keychain_builds_a_service_and_account_lookup(monkeypatch):
    seen = {}
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        secrets,
        "run",
        lambda argv, **kw: seen.update(argv=argv) or secrets.Output(True, stdout="v"),
    )
    secrets.resolve("KEY", "keychain://my-service/my-account")
    assert seen["argv"] == [
        "security", "find-generic-password", "-a", "my-account",
        "-s", "my-service", "-w",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="the keychain is macOS only")
def test_the_keychain_account_is_optional(monkeypatch):
    seen = {}
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        secrets,
        "run",
        lambda argv, **kw: seen.update(argv=argv) or secrets.Output(True, stdout="v"),
    )
    secrets.resolve("KEY", "keychain://my-service")
    assert "-a" not in seen["argv"]
    assert "my-service" in seen["argv"]


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def test_a_resolved_value_is_reused_within_a_session(tmp_path):
    """Otherwise every subagent re-shells into the vault."""
    marker = tmp_path / "count"
    reference = f"cmd://echo x >> {marker}; printf %s value"
    secrets.resolve("KEY", reference)
    secrets.resolve("KEY", reference)
    assert marker.read_text().count("x") == 1


def test_the_cache_can_be_bypassed(tmp_path):
    """`andromeda secrets` is asking whether the vault answers *now*."""
    marker = tmp_path / "count"
    reference = f"cmd://echo x >> {marker}; printf %s value"
    secrets.resolve("KEY", reference)
    secrets.resolve("KEY", reference, use_cache=False)
    assert marker.read_text().count("x") == 2


def test_a_failure_is_not_cached(tmp_path):
    """A locked vault that gets unlocked has to start working without a
    restart."""
    marker = tmp_path / "count"
    reference = f"cmd://echo x >> {marker}; exit 1"
    secrets.resolve("KEY", reference)
    secrets.resolve("KEY", reference)
    assert marker.read_text().count("x") == 2


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    """Somebody who moved their key into a vault did it so the value would stop
    living in a file."""
    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    secrets.resolve("KEY", _echo("the-secret-value"))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "the-secret-value" not in path.read_text(errors="ignore")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_apply_puts_the_value_in_the_environment():
    environ: dict[str, str] = {}
    report = secrets.apply({"KEY": _echo("value")}, environ=environ)
    assert environ["KEY"] == "value"
    assert report.applied == ["KEY"]
    assert report.ok


def test_the_shell_wins_over_the_vault():
    """`OPENROUTER_API_KEY=sk-test andromeda` has to work whatever the config
    says, or the config becomes something people comment out."""
    environ = {"KEY": "from-the-shell"}
    report = secrets.apply({"KEY": _echo("from-the-vault")}, environ=environ)
    assert environ["KEY"] == "from-the-shell"
    assert report.skipped == ["KEY"]


def test_override_reverses_that():
    environ = {"KEY": "from-the-shell"}
    secrets.apply({"KEY": _echo("from-the-vault")}, environ=environ, override=True)
    assert environ["KEY"] == "from-the-vault"


def test_one_failure_does_not_stop_the_others():
    """A locked vault must not stop the credential that is not in it."""
    environ: dict[str, str] = {}
    report = secrets.apply(
        {"BROKEN": "cmd://exit 1", "WORKS": _echo("value")}, environ=environ
    )
    assert environ == {"WORKS": "value"}
    assert [f.name for f in report.failures] == ["BROKEN"]
    assert not report.ok


def test_apply_with_no_block_does_nothing():
    assert secrets.apply(None).applied == []
    assert secrets.apply({}).applied == []


# ---------------------------------------------------------------------------
# The tie to redaction — the reason the three parts are one change
# ---------------------------------------------------------------------------


def test_a_resolved_secret_is_masked_everywhere_from_then_on():
    """This is what a vault buys beyond an `export`.

    The value is registered the moment it is resolved, so it is masked in every
    tool result, transcript and export for the rest of the session without
    anyone wiring it up.
    """
    secrets.resolve("MY_API_KEY", _echo("opaque-vault-value-1234"))
    scrubbed = redact.scrub("the key is opaque-vault-value-1234")
    assert "opaque-vault-value-1234" not in scrubbed.text
    assert "MY_API_KEY" in scrubbed.text


def test_a_failed_resolution_registers_nothing():
    secrets.resolve("KEY", "cmd://exit 1")
    assert redact.scrub("nothing to mask").text == "nothing to mask"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_the_block_is_read_defensively():
    assert secrets.from_config({}) == {}
    assert secrets.from_config({"secrets": None}) == {}
    assert secrets.from_config({"secrets": ["a", "b"]}) == {}
    assert secrets.from_config({"secrets": {"A": "op://x", "B": 5, "C": "  "}}) == (
        {"A": "op://x"}
    )


def test_the_status_command_never_prints_a_secret(tmp_path, monkeypatch, capsys):
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        f"secrets:\n  MY_KEY: \"{_echo('opaque-vault-value-1234')}\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MY_KEY", raising=False)

    assert secrets_cmd.status() == 0
    printed = capsys.readouterr().out
    assert "opaque-vault-value-1234" not in printed
    assert "MY_KEY" in printed


def test_status_says_when_the_environment_is_shadowing_a_reference(
    tmp_path, monkeypatch, capsys
):
    """A reference that resolves perfectly and is then ignored because of a
    stale `export` is a long afternoon."""
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"secrets:\n  MY_KEY: \"{_echo('value')}\"\n", encoding="utf-8"
    )
    monkeypatch.setenv("MY_KEY", "already-set")

    secrets_cmd.status()
    assert "environment already sets this" in capsys.readouterr().out


def test_get_masks_and_has_no_flag_to_stop_masking(tmp_path, monkeypatch, capsys):
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"secrets:\n  MY_KEY: \"{_echo('opaque-vault-value-1234')}\"\n",
        encoding="utf-8",
    )
    assert secrets_cmd.get("MY_KEY") == 0
    printed = capsys.readouterr().out
    assert "opaque-vault-value-1234" not in printed
    assert "23 characters" in printed


def test_get_on_an_unknown_name_says_what_to_run(tmp_path, monkeypatch, capsys):
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("secrets: {}\n", encoding="utf-8")
    assert secrets_cmd.get("NOPE") == 1


def test_a_reference_this_process_applied_does_not_count_as_shadowed(
    tmp_path, monkeypatch, capsys
):
    """Startup exports the block into the environment of whatever runs next.

    So a plain `os.environ` check reported every working reference as shadowed
    by the shell — including in the command whose whole job is to say whether
    it is. Found by running it.
    """
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"secrets:\n  MY_KEY: \"{_echo('value')}\"\n", encoding="utf-8"
    )
    monkeypatch.delenv("MY_KEY", raising=False)

    environ = dict(os.environ)
    secrets.apply({"MY_KEY": _echo("value")})
    try:
        secrets_cmd.status()
        assert "environment already sets this" not in capsys.readouterr().out
    finally:
        os.environ.clear()
        os.environ.update(environ)


def test_applied_names_records_only_what_went_to_the_real_environment():
    """A test that applies into a dict has not changed this process."""
    secrets.apply({"KEY": _echo("value")}, environ={})
    assert "KEY" not in secrets.applied_names()


# ---------------------------------------------------------------------------
# A pasted credential in the block that exists to prevent pasted credentials
# ---------------------------------------------------------------------------


def test_a_literal_value_is_named_and_never_used():
    """Someone reads the example, understands that keys go here, and puts the
    key here — in a file documented as safe to print and to commit."""
    config = {"secrets": {"GOOD": "op://v/i/f", "BAD": "sk-pasted-1234567890"}}
    assert secrets.from_config(config) == {"GOOD": "op://v/i/f"}
    assert secrets.literal_values(config) == ["BAD"]


def test_an_empty_entry_is_not_reported_as_a_pasted_credential():
    assert secrets.literal_values({"secrets": {"A": "", "B": "   ", "C": None}}) == []


def test_literal_values_is_defensive_about_the_block():
    assert secrets.literal_values({}) == []
    assert secrets.literal_values({"secrets": ["a"]}) == []


def test_the_status_command_reports_a_literal_without_printing_it(
    tmp_path, monkeypatch, capsys
):
    """The report must not become the leak."""
    from andromeda_cli.commands import secrets_cmd

    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        'secrets:\n  MY_KEY: "sk-pasted-value-1234"\n', encoding="utf-8"
    )
    assert secrets_cmd.status() == 1
    printed = capsys.readouterr()
    combined = printed.out + printed.err
    assert "sk-pasted-value-1234" not in combined
    assert "MY_KEY" in combined
    assert "not a reference" in combined
