"""What the redaction layer must and must not remove.

Two halves, and the second matters as much as the first. A redactor that masks
every secret and also mangles a page of documentation gets turned off, and then
it masks nothing at all. So every "this is removed" test here has a "this is
left alone" sibling.
"""

from __future__ import annotations

import pytest

from andromeda_agent import redact


@pytest.fixture(autouse=True)
def no_registered_secrets():
    """The known-value registry is process-global, as it has to be.

    Without this a test that registers a token leaks it into every test that
    runs after it, and the failure surfaces somewhere unrelated.
    """
    redact.clear_known()
    yield
    redact.clear_known()


# ---------------------------------------------------------------------------
# Known values
# ---------------------------------------------------------------------------


def test_a_registered_value_is_masked_anywhere_it_appears():
    redact.register_known("device_tok_abcdefghijk", "device-token")
    result = redact.scrub("curl -H 'X: device_tok_abcdefghijk' https://x")
    assert "device_tok_abcdefghijk" not in result.text
    assert "«redacted:device-token»" in result.text


def test_a_registered_value_is_masked_even_with_redaction_disabled(monkeypatch):
    """The toggle is for debugging patterns, not for echoing our own tokens."""
    monkeypatch.setenv("ANDROMEDA_REDACT_SECRETS", "0")
    redact.register_known("device_tok_abcdefghijk", "device-token")
    assert not redact.enabled()
    assert "device_tok_abcdefghijk" not in redact.scrub("it is device_tok_abcdefghijk").text


def test_a_short_value_is_refused_rather_than_registered():
    """A three-character 'secret' would match inside ordinary words."""
    assert redact.register_known("abc") is False
    assert redact.scrub("abc def").text == "abc def"


def test_the_longer_of_two_overlapping_values_is_masked_first():
    """A token that is a prefix of another must not leave the other's tail."""
    redact.register_known("tok_abcdefghijkl")
    redact.register_known("tok_abcdefghijklmnop_extra")
    result = redact.scrub("value tok_abcdefghijklmnop_extra here")
    assert "abcdefghijkl" not in result.text
    assert "_extra" not in result.text


# ---------------------------------------------------------------------------
# Known shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrst",
        "sk-ant-api03-abcdefghijklmnop",
        "ghp_ABCDEFGHIJKLMNOP1234",
        "github_pat_11ABCDEFG_abcdefghijklmnop",
        "glpat-abcdefghijklmnop",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA_abcdefghijklmnopqrstuvwxyz1234567",
        "hf_abcdefghijklmnopqrst",
        "npm_abcdefghijklmnopqrst",
    ],
)
def test_vendor_prefixed_credentials_are_masked_in_any_text(secret):
    result = redact.scrub(f"the key is {secret} ok")
    assert secret not in result.text
    assert result.count == 1


def test_a_jwt_is_masked():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert jwt not in redact.scrub(f"Bearer {jwt}").text


def test_an_authorization_header_is_masked():
    result = redact.scrub("Authorization: Bearer opaquetokenvalue1234")
    assert "opaquetokenvalue1234" not in result.text
    assert "Bearer" in result.text, "the scheme is not the secret"


def test_a_database_password_is_masked_and_the_host_is_kept():
    result = redact.scrub("postgres://app:hunter2@db.internal:5432/prod")
    assert "hunter2" not in result.text
    assert "db.internal:5432/prod" in result.text


def test_a_private_key_block_is_removed_whole():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef\n-----END RSA PRIVATE KEY-----"
    result = redact.scrub(f"here:\n{block}\ndone")
    assert "MIIEabc" not in result.text
    assert "here:" in result.text and "done" in result.text


def test_a_token_split_by_a_control_character_is_still_masked():
    """The smuggling case: contiguous to a reader, not to a regex."""
    result = redact.scrub("key sk-abc\x1bdefghijklmnop1234 end")
    assert "defghijklmnop1234" not in result.text


def test_a_token_at_end_of_line_does_not_eat_the_next_line():
    """Line structure is legitimate; joining across it masks real content."""
    result = redact.scrub("ghp_ABCDEFGHIJKLMNOP1234\nbutton [ref=e3]")
    assert "button [ref=e3]" in result.text


# ---------------------------------------------------------------------------
# Named assignments, and what they must not touch
# ---------------------------------------------------------------------------


def test_an_opaque_value_under_a_secret_name_is_masked():
    result = redact.scrub("MY_SERVICE_TOKEN=abc123randomstring", code_file=False)
    assert "abc123randomstring" not in result.text


def test_a_json_credential_field_is_masked():
    result = redact.scrub('{"apiKey": "supersecretvalue"}', code_file=False)
    assert "supersecretvalue" not in result.text


def test_a_yaml_credential_field_is_masked():
    result = redact.scrub("api_key: hunter2xyz", code_file=False)
    assert "hunter2xyz" not in result.text


def test_a_dotted_config_key_is_masked():
    result = redact.scrub("spring.datasource.password=hunter2xyz", code_file=False)
    assert "hunter2xyz" not in result.text


@pytest.mark.parametrize(
    "innocent",
    [
        "Secretary: J. Smith",
        "tokenizer: cl100k_base",
        "author=Smith",
        "The passage: a long one",
        "credentialing: in progress",
    ],
)
def test_prose_that_merely_contains_a_keyword_is_left_alone(innocent):
    """The failure this guards against is not cosmetic.

    A browser snapshot or a fetched page scrubbed into nonsense sends the model
    back to re-fetch the same page, and the loop only ends when the step
    ceiling does.
    """
    result = redact.scrub(innocent, code_file=False)
    assert result.text == innocent
    assert result.count == 0


def test_a_reference_to_a_variable_is_not_its_value():
    for snippet in ("KEY=os.getenv('X')", 'api_key: os.environ["X"]', "TOKEN=$MY_VAR"):
        assert redact.scrub(snippet, code_file=False).text == snippet


def test_source_code_constants_survive_the_default_pass():
    source = 'MAX_TOKENS = 100\nDEFAULT = {"apiKey": "test"}'
    assert redact.scrub(source).text == source


def test_a_url_query_token_is_passed_through():
    """An OAuth callback or a magic link is a URL the agent was told to follow.

    Masking the parameter breaks the flow mid-step, and a credential-shaped
    value inside the URL is still caught by the prefix pass.
    """
    url = "https://example.com/cb?code=ABC123&state=xyz"
    assert redact.scrub(url).text == url


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------


def test_a_file_read_mask_cannot_be_written_back_as_a_key():
    """The corruption case: a head-and-tail mask looks like a truncated key."""
    result = redact.scrub("TOKEN=ghp_ABCDEFGHIJKLMNOP1234", file_read=True)
    assert "«redacted:ghp_…»" in result.text
    assert "..." not in result.text


def test_a_log_mask_keeps_a_recognisable_stub():
    result = redact.scrub("key sk-abcdefghijklmnopqrstuvwx end")
    assert "sk-abc...uvwx" in result.text


def test_mask_hides_a_short_value_whole():
    assert redact.mask("short") == "***"
    assert redact.mask("sk-proj-abcdef1234567890") == "sk-p...7890"
    assert redact.mask("", empty="(not set)") == "(not set)"


def test_mask_never_emits_a_control_character():
    assert "\n" not in redact.mask("abc\ndefghijklmnop\n123")


# ---------------------------------------------------------------------------
# The count, which drives what the user is told
# ---------------------------------------------------------------------------


def test_a_declined_match_is_not_counted():
    """`re.subn` counts matches, not changes.

    Counting matches puts "1 value was masked" on a file nothing was removed
    from, which teaches people to disbelieve the notice.
    """
    assert redact.scrub("Secretary: J. Smith", code_file=False).count == 0
    assert redact.scrub("tokenizer: cl100k_base", code_file=False).count == 0


def test_the_count_is_the_number_of_secrets_removed():
    text = "A=sk-abcdefghijklmnopqrst\nB=ghp_ABCDEFGHIJKLMNOP1234"
    assert redact.scrub(text).count == 2


def test_the_notice_appears_only_when_something_was_removed():
    assert redact.notice(redact.Scrubbed("x", 0)) == ""
    assert "2 value(s)" in redact.notice(redact.Scrubbed("x", 2))


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


def test_a_dotenv_read_masks_every_value_whatever_the_key_is_called():
    """`.env` holds secrets by convention, so the key name says nothing."""
    body = "GITHUB=ghp_ABCDEFGHIJKLMNOP1234\nDB_HOST=prod-ro-7f2a\nEMPTY="
    result = redact.scrub_tool_result("read_file", {"path": "app/.env"}, body)
    assert "prod-ro-7f2a" not in result.text
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in result.text
    assert "EMPTY=" in result.text, "unset is information, not a secret"


def test_a_dotenv_read_keeps_the_vendor_label_from_the_earlier_pass():
    body = "GITHUB=ghp_ABCDEFGHIJKLMNOP1234"
    result = redact.scrub_tool_result("read_file", {"path": ".env"}, body)
    assert "«redacted:ghp_…»" in result.text


def test_a_dotenv_example_is_not_treated_as_a_secret_store():
    """It documents the shape and holds no values; scrubbing it hides the one
    file that exists to be read."""
    body = "GITHUB=your-token-here"
    result = redact.scrub_tool_result("read_file", {"path": ".env.example"}, body)
    assert result.text == body


def test_an_ordinary_source_read_does_not_run_the_assignment_pass():
    body = "MAX_TOKENS = 100"
    assert redact.scrub_tool_result("read_file", {"path": "config.py"}, body).text == body


def test_printenv_output_runs_the_assignment_pass():
    result = redact.scrub_tool_result(
        "terminal", {"command": "printenv"}, "MY_SERVICE_TOKEN=abc123randomstring"
    )
    assert "abc123randomstring" not in result.text


def test_cat_dotenv_is_treated_the_same_as_printenv():
    """Blocking one and not the other teaches the agent which to reach for."""
    result = redact.scrub_tool_result(
        "terminal", {"command": "cat .env"}, "DB_HOST=prod-ro-7f2a"
    )
    assert "prod-ro-7f2a" not in result.text


def test_an_ordinary_command_does_not_run_the_assignment_pass():
    result = redact.scrub_tool_result(
        "terminal", {"command": "npm run build"}, "MAX_TOKENS=100"
    )
    assert result.text == "MAX_TOKENS=100"


def test_an_unknown_tool_gets_the_conservative_default():
    """A tool added later must be covered without anyone wiring it up."""
    result = redact.scrub_tool_result("some_new_tool", {}, "sk-abcdefghijklmnopqrst")
    assert "sk-abcdefghijklmnopqrst" not in result.text
    assert redact.scrub_tool_result("some_new_tool", {}, "MAX_TOKENS=100").text == (
        "MAX_TOKENS=100"
    )


@pytest.mark.parametrize(
    "command,expected",
    [
        ("env", True),
        ("printenv | grep KEY", True),
        ("ls && export", True),
        ("npm run build", False),
        ("", False),
        (None, False),
    ],
)
def test_env_dump_detection(command, expected):
    assert redact.is_env_dump(command) is expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("cat .env", True),
        ("head -n 5 app/.env.local", True),
        ("cat '.env'", True),
        ("cat .env.example", False),
        ("cat README.md", False),
        ("cat", False),
    ],
)
def test_env_file_read_detection(command, expected):
    assert redact.reads_env_file(command) is expected


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def test_the_prefix_screen_is_derived_from_the_patterns():
    """A hand-maintained second list goes stale the first time somebody adds a
    vendor and does not know the screen exists."""
    for pattern in redact._PREFIX_PATTERNS:
        literal = redact._literal_prefix(pattern)
        assert literal, f"{pattern} has no literal prefix to screen on"
        assert literal in redact._PREFIX_SUBSTRINGS


def test_the_screen_never_produces_a_false_negative():
    """Every pattern's own literal must pass the screen it is screened by."""
    for secret in ("sk-abcdefghijklmnop", "ghp_ABCDEFGHIJ1234", "AKIAIOSFODNN7EXAMPLE"):
        assert redact._has_prefix_substring(secret)


def test_redaction_can_be_turned_off_by_environment(monkeypatch):
    monkeypatch.setenv("ANDROMEDA_REDACT_SECRETS", "off")
    assert redact.scrub("sk-abcdefghijklmnopqrst").text == "sk-abcdefghijklmnopqrst"
    monkeypatch.setenv("ANDROMEDA_REDACT_SECRETS", "1")
    assert redact.scrub("sk-abcdefghijklmnopqrst").text != "sk-abcdefghijklmnopqrst"


def test_force_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("ANDROMEDA_REDACT_SECRETS", "off")
    assert "sk-abcdefghijklmnopqrst" not in redact.scrub(
        "sk-abcdefghijklmnopqrst", force=True
    ).text


def test_empty_and_non_string_input_do_not_raise():
    assert redact.scrub("").text == ""
    assert redact.scrub(None).text == ""
    assert redact.scrub(1234).text == "1234"


# ---------------------------------------------------------------------------
# The chokepoint, end to end
# ---------------------------------------------------------------------------


def _conversation(tmp_path, script, **kwargs):
    from andromeda_agent import Conversation, Policy
    from andromeda_tools import Workspace, build_registry
    from andromeda_tools.todo import TodoList
    from support import ScriptedProvider

    workspace = Workspace(tmp_path)
    todos = TodoList()
    return Conversation(
        provider=ScriptedProvider(script=list(script)),  # type: ignore[arg-type]
        policy=Policy(
            mode="auto",
            enabled=frozenset({"read_file", "terminal", "list_dir"}),
            max_tier="destructive",
        ),
        workspace=workspace,
        todos=todos,
        registry=build_registry(workspace, todos),
        **kwargs,
    )


def test_a_secret_never_reaches_the_transcript(tmp_path):
    """The invariant the whole layer exists for.

    The transcript is the durable copy — it is exported, indexed for search,
    and replayed on resume. A secret that reaches it is a secret that outlives
    the session that leaked it.
    """
    from andromeda_agent import Callbacks
    from support import call, turn_with

    (tmp_path / "conf.yaml").write_text(
        "github_token: ghp_ABCDEFGHIJKLMNOP1234\n", encoding="utf-8"
    )
    conversation = _conversation(
        tmp_path,
        [turn_with(call("read_file", {"path": "conf.yaml"})), "There is a token."],
    )

    conversation.send("read conf.yaml", Callbacks())

    transcript = "".join(str(message.get("content") or "") for message in conversation.messages)
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in transcript


def test_the_surface_sees_the_scrubbed_result_too(tmp_path):
    """A secret in the scrollback has already been read by whoever is there."""
    from andromeda_agent import Callbacks
    from support import call, turn_with

    (tmp_path / "conf.yaml").write_text(
        "token: ghp_ABCDEFGHIJKLMNOP1234\n", encoding="utf-8"
    )
    seen: list[str] = []
    conversation = _conversation(
        tmp_path,
        [turn_with(call("read_file", {"path": "conf.yaml"})), "done"],
    )

    conversation.send(
        "read it",
        Callbacks(on_tool_result=lambda spec, result: seen.append(result.display + result.content)),
    )

    assert seen and "ghp_ABCDEFGHIJKLMNOP1234" not in "".join(seen)


def test_a_redacted_file_read_tells_the_model_what_happened(tmp_path):
    """Without the note the sentinel has been observed being written onward."""
    from andromeda_agent import Callbacks
    from support import call, turn_with

    (tmp_path / ".env").write_text("KEY=ghp_ABCDEFGHIJKLMNOP1234\n", encoding="utf-8")
    conversation = _conversation(
        tmp_path, [turn_with(call("read_file", {"path": ".env"})), "ok"]
    )

    conversation.send("read .env", Callbacks())

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "masked before you saw them" in tool_message["content"]


def test_an_unredacted_result_is_returned_unchanged(tmp_path):
    """No copy, no rebuild, no notice when there was nothing to remove."""
    from andromeda_agent import Callbacks
    from support import call, turn_with

    (tmp_path / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    conversation = _conversation(
        tmp_path, [turn_with(call("read_file", {"path": "note.txt"})), "ok"]
    )

    conversation.send("read it", Callbacks())

    tool_message = [m for m in conversation.messages if m["role"] == "tool"][0]
    assert "masked" not in tool_message["content"]
    assert "alpha" in tool_message["content"]


# ---------------------------------------------------------------------------
# The line-number gutter — found by running it, not by testing it
# ---------------------------------------------------------------------------


def test_the_anchored_patterns_survive_read_files_line_numbers():
    """`read_file` returns `  12\\tCONTENT`.

    So on the one surface the line-anchored patterns exist for — a config file
    the agent has just read — the line does not start where the pattern expects
    it to, and every anchored pass was silently dead. A `.env` read came back
    with its second line untouched.
    """
    numbered = "1\tGITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOP1234\n2\tDB_HOST=prod-ro-7f2a"
    result = redact.scrub_tool_result("read_file", {"path": ".env"}, numbered)
    assert "prod-ro-7f2a" not in result.text
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in result.text


def test_a_numbered_empty_value_is_still_left_alone():
    numbered = "3\tEMPTY="
    result = redact.scrub_tool_result("read_file", {"path": ".env"}, numbered)
    assert result.text == numbered


@pytest.mark.parametrize("gutter", ["", "  ", "12\t", "  7\t"])
def test_the_anchored_config_pass_matches_with_or_without_a_gutter(gutter):
    result = redact.scrub(f"{gutter}password=hunter2xyz", code_file=False)
    assert "hunter2xyz" not in result.text


def test_a_later_pass_never_flattens_an_earlier_masks_label():
    """`«redacted:ghp_…»` becoming `***` throws away the one useful thing the
    first mask kept: which vendor's credential is in the file."""
    numbered = "1\tGITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOP1234"
    result = redact.scrub_tool_result("read_file", {"path": ".env"}, numbered)
    assert "«redacted:ghp_…»" in result.text
    assert "***" not in result.text
