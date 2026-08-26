"""Secrets, removed before anything can read them back.

A tool result is the one path by which the outside world enters this process.
It is shown in the terminal, appended to a JSON transcript, indexed for
cross-session search, exported by `sessions export`, and sent to the model —
five destinations, one source. So the redaction happens once, at that source,
in `loop._dispatch`, and every destination inherits it. Redacting at each
destination instead is how two of them end up disagreeing about what a secret
is, and the transcript is the one that keeps the answer forever.

Three passes, in order of how much they can be trusted:

1. **Known values.** Secrets this process is actually holding — the device
   token, a BYOK key, an MCP access token — masked by exact string match.
   No pattern can be wrong about these, and no pattern would catch them
   either: a relay-issued device token has no vendor prefix to recognise.
2. **Known shapes.** `sk-…`, `ghp_…`, JWTs, private-key blocks, database
   connection strings, `Authorization:` headers. High confidence, low false
   positives, and they run on every kind of text.
3. **Named assignments.** `OPENAI_API_KEY=…`, `"apiKey": "…"`, `password: …`.
   These catch the opaque token that has no recognisable shape, and they are
   also the ones that mangle innocent text — `Secretary: J. Smith`,
   `tokenizer: cl100k_base`, `MAX_TOKENS=100`. They run only where the text
   is credential-shaped rather than prose or source, which the caller decides
   by passing `code_file`.

**What redaction is not.** The terminal tool runs as the user, with the user's
shell. Anything reachable by `cat` is reachable, and a model that means harm
can base64 a file before printing it. This layer exists so that a secret does
not end up in a transcript, an export, a search index or a scrollback *by
accident*, which is how it actually happens. Treating it as a boundary is how
you get a boundary nobody checks.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Known values — pass one
# ---------------------------------------------------------------------------

# Secrets this process is holding, registered by whoever loaded them. Masked by
# exact match, so there is no pattern to be wrong about and no false positive
# to weigh. This is the only pass that can catch a credential with no
# recognisable shape, which is most of them: a relay device token, an OAuth
# access token, a webhook signing secret.
#
# The floor is not cosmetic. A short registered value would match inside
# ordinary words and scrub the transcript into noise, so anything under it is
# refused rather than half-applied.
_MIN_KNOWN_LENGTH = 12

_known: dict[str, str] = {}
_known_lock = threading.Lock()


def register_known(value: str, label: str = "") -> bool:
    """Mask `value` wherever it appears from now on. Returns whether it took.

    Call this the moment a credential is loaded, not the moment it is used —
    the tool call that leaks it is usually not the one that needed it.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) < _MIN_KNOWN_LENGTH:
        return False
    with _known_lock:
        _known[value] = f"«redacted:{label}»" if label else "«redacted-secret»"
    return True


def clear_known() -> None:
    """Only for tests, and for a profile switch that changes every credential."""
    with _known_lock:
        _known.clear()


def _mask_known(text: str) -> tuple[str, int]:
    with _known_lock:
        # Longest first: one credential can be a prefix of another (a token and
        # the same token with a suffix), and masking the short one first would
        # leave the tail of the long one in the text.
        items = sorted(_known.items(), key=lambda pair: len(pair[0]), reverse=True)
    hits = 0
    for value, replacement in items:
        if value in text:
            hits += text.count(value)
            text = text.replace(value, replacement)
    return text, hits


# ---------------------------------------------------------------------------
# Known shapes — pass two
# ---------------------------------------------------------------------------

# Vendor prefixes. Each entry keeps its full literal prefix at the front so the
# cheap substring pre-screen below can be derived from it automatically; a
# pattern that started with a character class would break that derivation and
# silently disable the screen for itself.
_PREFIX_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{10,}",            # OpenAI, OpenRouter, Anthropic (sk-ant-)
    r"sk_live_[A-Za-z0-9]{10,}",         # Stripe secret, live
    r"sk_test_[A-Za-z0-9]{10,}",         # Stripe secret, test
    r"rk_live_[A-Za-z0-9]{10,}",         # Stripe restricted
    r"sk_[A-Za-z0-9_]{10,}",             # underscore form (ElevenLabs and others)
    r"ghp_[A-Za-z0-9]{10,}",             # GitHub PAT, classic
    r"github_pat_[A-Za-z0-9_]{10,}",     # GitHub PAT, fine-grained
    r"gho_[A-Za-z0-9]{10,}",             # GitHub OAuth
    r"ghu_[A-Za-z0-9]{10,}",             # GitHub user-to-server
    r"ghs_[A-Za-z0-9]{10,}",             # GitHub server-to-server
    r"ghr_[A-Za-z0-9]{10,}",             # GitHub refresh
    r"glpat-[A-Za-z0-9_\-]{10,}",        # GitLab PAT
    r"gloas-[A-Za-z0-9_\-]{10,}",        # GitLab OAuth application secret
    r"gldt-[A-Za-z0-9_\-]{10,}",         # GitLab deploy token
    r"glrt-[A-Za-z0-9_.\-]{10,}",        # GitLab runner auth (routable, dotted)
    r"glcbt-[A-Za-z0-9_\-]{10,}",        # GitLab CI job token
    r"glptt-[A-Za-z0-9_\-]{10,}",        # GitLab pipeline trigger
    r"xox[baprs]-[A-Za-z0-9-]{10,}",     # Slack bot/app/user
    r"xapp-\d+-[A-Za-z0-9-]{10,}",       # Slack app-level
    r"AIza[A-Za-z0-9_-]{30,}",           # Google API key
    r"AKIA[A-Z0-9]{16}",                 # AWS access key id
    r"ASIA[A-Z0-9]{16}",                 # AWS temporary access key id
    r"SG\.[A-Za-z0-9_-]{10,}",           # SendGrid
    r"hf_[A-Za-z0-9]{10,}",              # Hugging Face
    r"r8_[A-Za-z0-9]{10,}",              # Replicate
    r"npm_[A-Za-z0-9]{10,}",             # npm
    r"pypi-[A-Za-z0-9_-]{10,}",          # PyPI
    r"dop_v1_[A-Za-z0-9]{10,}",          # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",          # DigitalOcean OAuth
    r"pplx-[A-Za-z0-9]{10,}",            # Perplexity
    r"gsk_[A-Za-z0-9]{10,}",             # Groq
    r"xai-[A-Za-z0-9]{30,}",             # xAI
    r"tvly-[A-Za-z0-9]{10,}",            # Tavily
    r"exa_[A-Za-z0-9]{10,}",             # Exa
    r"fc-[A-Za-z0-9]{10,}",              # Firecrawl
    r"fal_[A-Za-z0-9_-]{10,}",           # fal.ai
    r"ntn_[A-Za-z0-9]{10,}",             # Notion integration token
    r"syt_[A-Za-z0-9]{10,}",             # Matrix access token
    r"bb_live_[A-Za-z0-9_-]{10,}",       # Browserbase
    r"gAAAA[A-Za-z0-9_=-]{20,}",         # Fernet-encrypted blob
)

_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def _literal_prefix(pattern: str) -> str:
    """The leading literal characters of `pattern`, stopping at the first meta.

    Any match of the pattern must contain this as a substring, so screening on
    it cannot produce a false negative. Derived rather than written down: a
    hand-maintained second list is a list that goes stale the first time
    somebody adds a vendor and does not know the screen exists.
    """
    for index, character in enumerate(pattern):
        if character in "[(\\.?*+|{^$":
            return pattern[:index]
    return pattern


_PREFIX_SUBSTRINGS = tuple(
    sorted({literal for literal in map(_literal_prefix, _PREFIX_PATTERNS) if literal})
)

# Control and zero-width characters that can split a token body. A secret
# smuggled as `sk-abc\x1bdef…` is contiguous to a reader and not to a regex,
# so the ordinary pass misses it entirely.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202f\u2060\ufeff]")

# Characters a token body may contain. `=` is deliberately absent: it separates
# a name from a value, so allowing it would let one match span two unrelated
# assignments on adjacent lines.
_TOKEN_BODY = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)

# Everything unprintable, for the display mask. A masked secret must never emit
# a newline or an invisible character into a status table.
_DISPLAY_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f\x80-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064]"
)

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization\s*[:=]\s*)((?:Bearer|Basic|Token|Digest|ApiKey)\s+)?"
    r"([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)

_SECRET_HEADER_RE = re.compile(
    r"((?:x-)?(?:api[-_]?key|auth[-_]?token|access[-_]?token|session[-_]?token|"
    r"secret[-_]?key)\s*:\s*)([^\s,;]{8,})",
    re.IGNORECASE,
)

# scheme://user:password@host. The password group forbids whitespace so a
# sentence containing an `@` cannot be swallowed.
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis|rediss|amqp|amqps|"
    r"clickhouse|mssql|cockroachdb)://[^\s:/@]+:)([^\s@/]+)(@)",
    re.IGNORECASE,
)

# scheme://TOKEN@host — a bare credential in userinfo, the shape a git remote
# with an embedded token takes. The colon-bearing `user:pass@` form is left to
# _DB_CONNSTR_RE for database schemes and passed through for web ones, because
# a web URL carrying a password is usually one the agent was told to follow.
_URL_BARE_TOKEN_RE = re.compile(
    r"((?:https?|ftp|ssh|git)://)([A-Za-z0-9._~+/=-]{16,})(@)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Named assignments — pass three
# ---------------------------------------------------------------------------

_SECRET_ENV_NAMES = r"(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PW|CREDENTIAL|AUTH)"

# All-caps names keep embedded matching — `MYTOKEN=…` is a credential, not
# prose, because prose is not shouted.
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2"
)

# Lower-case names must have the keyword at an underscore boundary. A bare
# `password=` or `token=` in mixed case appears in prose, in query strings and
# in form bodies far more often than it appears as a leaked credential.
_ENV_ASSIGN_LOWER_RE = re.compile(
    r"([a-z0-9_]+(?:_|^)(?:key|pass|pw|token|secret|password|passwd|credential|auth)"
    r"(?=[^a-z0-9_]|$))\s*=\s*(['\"]?)(\S+)\2",
    re.IGNORECASE,
)

# `read_file` returns `  12\tCONTENT`, so on the one surface these patterns
# exist for — a config file the agent just read — a line does not start where a
# line-anchored pattern expects it to. Found by running it: a `.env` read came
# back with its second line untouched. Every `^`-anchored pattern below allows
# the gutter, and a test pins each one against numbered and unnumbered input.
_GUTTER = r"[ \t]*(?:\d+\t)?"

_SECRET_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential|auth)"
_CFG_VALUE = r"(['\"]?)([^\s&]+?)\2(?=[\s&]|$)"

# A linear pre-gate for the two config patterns below. Both require a secret
# keyword in the key, so text without one cannot match either — skipping is
# exact, not a heuristic. It matters because the dotted pattern backtracks
# badly on a long unbroken run of token characters, which is exactly what a
# base64 blob in a tool result looks like.
_CFG_KEYWORD_RE = re.compile(_SECRET_CFG_NAMES, re.IGNORECASE)

# A namespaced key is unambiguously configuration: `spring.datasource.password`
# is never a sentence.
_CFG_DOTTED_RE = re.compile(
    rf"([A-Za-z0-9_\-]+\.[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*"
    rf"|[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*\.[A-Za-z0-9_.\-]+)"
    rf"={_CFG_VALUE}",
    re.IGNORECASE,
)

# A bare key only counts at the start of a line, optionally after `export`.
# Mid-sentence, `I set password=hunter2` is the user telling you something.
_CFG_ANCHORED_RE = re.compile(
    rf"(^{_GUTTER}(?:export[ \t]+)?[A-Za-z0-9_\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_\-]*)={_CFG_VALUE}",
    re.IGNORECASE | re.MULTILINE,
)

_JSON_FIELD_RE = re.compile(
    r"(\"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|"
    r"bearer|client_secret|private_key)\")\s*:\s*\"([^\"]{4,})\"",
)

# Unquoted YAML. `auth` is excluded from the key set so `Authorization:` and
# `author:` do not match; `auth_token` still matches through `token`.
_YAML_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential)"
_YAML_ASSIGN_RE = re.compile(
    rf"(^{_GUTTER}[A-Za-z0-9_.\-]*{_YAML_CFG_NAMES}[A-Za-z0-9_.\-]*)(:[ \t]*)(?!['\"])([^\s&]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Every assignment, whatever it is called. Used only where the whole file is a
# secret store, so the key name carries no information about whether the value
# is one. Anchored per line and quote-aware, because a `.env` value legitimately
# contains spaces once it is quoted.
_ANY_ASSIGN_RE = re.compile(
    rf"^({_GUTTER}(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*)=(['\"]?)([^\n]*?)\2[ \t]*$",
    re.MULTILINE,
)

# A reference to a variable is not the variable's value. `KEY=os.getenv("X")`
# is a line of code, and masking it turns a correct snippet into a wrong one.
_ENV_LOOKUP_RE = re.compile(r"^(?:os\.(?:getenv|environ)|process\.env|\$ENV\{|\$\{?[A-Z_]+\}?$)")

# Which keyword occurrences count. The key patterns above allow affixes so that
# `client_secret`, `clientSecret` and `s3.secret-key` all match — the cost is
# that `Secretary`, `tokenizer` and `authored` matched too, and scrubbing those
# out of a fetched page turns legitimate content into noise the model then
# re-fetches. A keyword only counts at a word boundary within the key: at an
# edge, beside a non-letter, or at a camelCase transition.
_KEY_KEYWORD_RE = re.compile(
    r"(?:api|auth|access|refresh|session|secret)[ _.\-]?(?:key|token)"
    r"|token|secret|passwd|password|pass|pw|credential|auth|key",
    re.IGNORECASE,
)


def _is_word_start(text: str, index: int) -> bool:
    if index == 0:
        return True
    previous, current = text[index - 1], text[index]
    if not previous.isalpha():
        return True
    return current.isupper() and previous.islower()


def _is_word_end(text: str, index: int) -> bool:
    """`index` is one past the last character of the keyword."""
    if index >= len(text):
        return True
    # A trailing plural belongs to the keyword: `secrets:` and `tokens:` are
    # the same key as their singulars.
    if text[index] in "sS" and (index + 1 >= len(text) or not text[index + 1].isalpha()):
        return True
    following = text[index]
    if not following.isalpha():
        return True
    return following.isupper()


def _key_has_secret_keyword(key: str) -> bool:
    if key.isupper():
        # Shouted keys keep embedded matching, as above.
        return True
    for match in _KEY_KEYWORD_RE.finditer(key):
        if _is_word_start(key, match.start()) and _is_word_end(key, match.end()):
            return True
    return False


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask(
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """Mask a secret for display, keeping enough of it to be recognised.

    This is the one for a status table or a config listing, where the point is
    to let a person confirm *which* key is configured without printing it.
    Anything short enough that head and tail would reveal most of it is masked
    whole.
    """
    if not value:
        return empty
    value = _DISPLAY_CONTROL_RE.sub("", value)
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_log(token: str) -> str:
    """The mask for text nobody will paste back: keeps a recognisable stub."""
    if not token:
        return "***"
    return mask(token, head=6, tail=4, floor=18)


def _mask_unusable(token: str) -> str:
    """The mask for file content, which somebody will paste back.

    A head-and-tail mask looks like a real key that has been truncated, and an
    agent that reads one out of a config file and writes it back has quietly
    replaced a working credential with thirteen dead characters. This shape is
    syntactically impossible as a token, so the mistake cannot be made silently
    — and it keeps the vendor prefix, so the agent can still answer "there is a
    GitHub token in this file" without seeing a byte of it.
    """
    if not token:
        return "«redacted-secret»"
    for prefix in _PREFIX_SUBSTRINGS:
        if token.startswith(prefix):
            return f"«redacted:{prefix}…»"
    return "«redacted-secret»"


def _mask_split_tokens(text: str, mask_fn: Callable[[str], str]) -> tuple[str, int]:
    """Mask tokens whose body is interrupted by control characters.

    Match on a copy with the control characters removed — where the token is
    contiguous again — then mask the corresponding span of the original. The
    two are aligned one-to-one on every non-control character, so the span is
    exact.
    """
    stripped = _CONTROL_RE.sub("", text)
    if stripped == text:
        return text, 0

    positions = [index for index, ch in enumerate(text) if not _CONTROL_RE.match(ch)]
    replacements: list[tuple[int, int, str]] = []

    for found in _PREFIX_RE.finditer(stripped):
        body = found.group(1)
        start = positions[found.start(1)]
        end = positions[found.end(1) - 1] + 1
        span = text[start:end]

        # A newline between two things that each look like a token is line
        # structure, not smuggling. Joining across it would mask whatever
        # followed the newline, which on a browser snapshot is a real element.
        if ("\n" in span or "\r" in span) and _PREFIX_RE.search(span):
            continue
        # The span must be token material throughout, and must not run into a
        # `KEY=` name: a real value is followed by whitespace or end of line.
        if not all(ch in _TOKEN_BODY or _CONTROL_RE.match(ch) for ch in span):
            continue
        if end < len(text) and text[end] == "=":
            continue
        replacements.append((start, end, mask_fn(body)))

    if not replacements:
        return text, 0

    out = list(text)
    for start, end, replacement in reversed(replacements):
        out[start:end] = list(replacement)
    return "".join(out), len(replacements)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

_ENV_TOGGLE = "ANDROMEDA_REDACT_SECRETS"


def enabled() -> bool:
    """Whether pattern redaction runs. Known values are masked regardless.

    Off is a real choice — somebody debugging a credential handshake needs to
    see the credential — but it is deliberately not a choice the model can make
    and not one a config file makes quietly. It is an environment variable on
    the command that starts the session.
    """
    raw = os.environ.get(_ENV_TOGGLE, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


@dataclass(frozen=True)
class Scrubbed:
    """Text with its secrets removed, and how many there were.

    The count is not statistics. A redacted file read has to *say* it was
    redacted, or the agent treats the sentinel as the value and writes it back
    somewhere.
    """

    text: str
    count: int = 0

    @property
    def changed(self) -> bool:
        return self.count > 0

    def __str__(self) -> str:  # so a caller can drop it straight into a format
        return self.text


def scrub(
    text: str,
    *,
    force: bool = False,
    code_file: bool | None = None,
    file_read: bool = False,
    secrets_file: bool = False,
) -> Scrubbed:
    """Remove secrets from `text`.

    `code_file` skips the named-assignment pass. Unset means True — on unknown
    text the cost of a false positive (mangled content the model believes, or
    re-fetches in a loop) is higher than the cost of a missed opaque token,
    which the known-value and known-shape passes usually catch anyway. A caller
    holding a credential dump passes False explicitly, and that survives
    `file_read`: reading `.env` is both a file read and a credential dump, and
    an implied default that overrode the explicit argument is what made the
    first version of this miss every unprefixed value in a `.env`.

    `file_read` swaps the mask for a shape that cannot be written back.

    `secrets_file` masks the value of *every* assignment, whatever the key is
    called. Only for a file that holds nothing but secrets — `.env`, `.netrc`,
    `.pgpass` — where `DATABASE=postgres_prod_ro` is as much a credential as
    the line above it and no keyword pass would ever catch it.
    """
    if text is None:
        return Scrubbed("", 0)
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return Scrubbed(text, 0)
    if code_file is None:
        code_file = True

    # Pass one runs even with redaction off: these are values this process was
    # handed in confidence, and no debugging session needs them echoed back.
    text, count = _mask_known(text)

    if not (force or enabled()):
        return Scrubbed(text, count)

    token_mask = _mask_unusable if file_read else _mask_log

    if _has_prefix_substring(text):
        text, hits = _mask_split_tokens(text, token_mask)
        count += hits
        text, hits = _sub_counting(_PREFIX_RE, lambda m: token_mask(m.group(1)), text)
        count += hits

    if secrets_file and "=" in text:
        text, hits = _sub_counting(_ANY_ASSIGN_RE, _replace_any_assignment, text)
        count += hits

    if not code_file:
        if "=" in text:
            text, hits = _sub_counting(_ENV_ASSIGN_RE, _replace_assignment, text)
            count += hits
            # Skip URLs: a query string legitimately carries `token=` and
            # `key=` parameters that the agent was told to follow.
            if "://" not in text:
                text, hits = _sub_counting(
                    _ENV_ASSIGN_LOWER_RE, _replace_assignment, text
                )
                count += hits
                if _CFG_KEYWORD_RE.search(text):
                    text, hits = _sub_counting(
                        _CFG_DOTTED_RE, _replace_assignment, text
                    )
                    count += hits
                    text, hits = _sub_counting(
                        _CFG_ANCHORED_RE, _replace_assignment, text
                    )
                    count += hits

        if ":" in text and '"' in text:
            text, hits = _sub_counting(_JSON_FIELD_RE, _replace_json, text)
            count += hits

        if ":" in text and "://" not in text:
            text, hits = _sub_counting(_YAML_ASSIGN_RE, _replace_yaml, text)
            count += hits

    # `uthorization` is the cheapest gate that covers every casing without
    # folding the whole string.
    if "uthorization" in text or "UTHORIZATION" in text:
        text, hits = _sub_counting(
            _AUTH_HEADER_RE,
            lambda m: m.group(1) + (m.group(2) or "") + _mask_log(m.group(3)),
            text,
        )
        count += hits

    if ":" in text:
        # The env-lookup carve-out applies here too, and only a test caught
        # that it did not: `api_key: os.environ["X"]` is header-shaped as well
        # as YAML-shaped, and this pass ran first and masked the code snippet
        # that the YAML pass would have correctly left alone.
        text, hits = _sub_counting(
            _SECRET_HEADER_RE,
            lambda m: (
                m.group(0)
                if _ENV_LOOKUP_RE.match(m.group(2))
                else m.group(1) + _mask_log(m.group(2))
            ),
            text,
        )
        count += hits

    if "BEGIN" in text and "-----" in text:
        text, hits = _sub_counting(
            _PRIVATE_KEY_RE, lambda _m: "[redacted private key]", text
        )
        count += hits

    if "://" in text:
        text, hits = _sub_counting(
            _DB_CONNSTR_RE, lambda m: f"{m.group(1)}***{m.group(3)}", text
        )
        count += hits
        text, hits = _sub_counting(
            _URL_BARE_TOKEN_RE,
            lambda m: f"{m.group(1)}{_mask_log(m.group(2))}{m.group(3)}",
            text,
        )
        count += hits

    if "eyJ" in text:
        text, hits = _sub_counting(_JWT_RE, lambda m: _mask_log(m.group(0)), text)
        count += hits

    return Scrubbed(text, count)


def _already_masked(value: str) -> bool:
    """Whether an earlier pass has already replaced this value.

    Each pass is narrower than the one before it, so a later pass re-masking an
    earlier pass's output only ever loses information — `«redacted:ghp_…»`
    becoming `***` throws away which vendor's credential is in the file, which
    was the one useful thing the first mask kept.
    """
    return value.startswith("«redacted") or value in {"***", "[redacted private key]"}


def _replace_assignment(match: re.Match[str]) -> str:
    name, quote, value = match.group(1), match.group(2), match.group(3)
    if _already_masked(value):
        return match.group(0)
    if _ENV_LOOKUP_RE.match(value):
        return match.group(0)
    if not _key_has_secret_keyword(name):
        return match.group(0)
    return f"{name}={quote}{_mask_log(value)}{quote}"


def _replace_any_assignment(match: re.Match[str]) -> str:
    """Mask an assignment in a file that holds nothing but secrets.

    An empty value is left alone: `FOO=` says the variable exists and is unset,
    which is information, and `FOO=***` would say the opposite.

    A value an earlier pass already masked is left alone too. This pass runs
    after the prefix pass, and re-masking `«redacted:ghp_…»` down to
    `«redacted-secret»` throws away the one useful thing the first mask kept —
    which vendor's credential is in the file.
    """
    name, quote, value = match.group(1), match.group(2), match.group(3)
    if not value.strip():
        return match.group(0)
    if _already_masked(value):
        return match.group(0)
    if _ENV_LOOKUP_RE.match(value):
        return match.group(0)
    return f"{name}={quote}{_mask_unusable(value)}{quote}"


def _replace_json(match: re.Match[str]) -> str:
    key, value = match.group(1), match.group(2)
    if _already_masked(value):
        return match.group(0)
    if _ENV_LOOKUP_RE.match(value):
        return match.group(0)
    return f'{key}: "{_mask_log(value)}"'


def _replace_yaml(match: re.Match[str]) -> str:
    key, separator, value = match.group(1), match.group(2), match.group(3)
    if _already_masked(value):
        return match.group(0)
    if _ENV_LOOKUP_RE.match(value):
        return match.group(0)
    if not _key_has_secret_keyword(key):
        return match.group(0)
    return f"{key}{separator}{_mask_log(value)}"


def _has_prefix_substring(text: str) -> bool:
    return any(substring in text for substring in _PREFIX_SUBSTRINGS)


def _sub_counting(
    pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str], text: str
) -> tuple[str, int]:
    """Substitute, counting only the matches that actually changed.

    `re.subn` counts every match the pattern found, including the ones a
    replacement function declined by returning the text it was given. The count
    is what the user is told — "3 values were masked" — so it has to be the
    number of secrets removed, not the number of times the word `secret`
    appeared. Getting this wrong puts a redaction notice on a file that was
    never redacted, which teaches people to ignore the notice.
    """
    hits = 0

    def wrapper(match: re.Match[str]) -> str:
        nonlocal hits
        replaced = replace(match)
        if replaced != match.group(0):
            hits += 1
        return replaced

    return pattern.sub(wrapper, text), hits


# ---------------------------------------------------------------------------
# Deciding how to scrub a particular tool result
# ---------------------------------------------------------------------------

# Commands whose stdout is a credential dump rather than source. For these the
# named-assignment pass has to run, because that is the only pass that catches
# `MY_SERVICE_TOKEN=abc123` — an opaque value with no vendor shape.
_ENV_DUMP_COMMANDS = frozenset({"env", "printenv", "set", "export", "declare"})

_FILE_READ_COMMANDS = frozenset(
    {"cat", "head", "tail", "bat", "batcat", "less", "more", "nl", "tac", "zcat", "view"}
)

# Basenames that hold secrets by convention. `.env.example` is deliberately
# absent: it documents the shape and holds no values, and scrubbing it would
# hide the one file that exists to be read.
_ENV_FILE_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".env.staging",
        ".envrc",
        "credentials",
        "credentials.json",
        ".netrc",
        ".pgpass",
    }
)

# Tools whose result is file content somebody may copy out of. These get the
# unusable sentinel rather than a head-and-tail mask.
_FILE_READ_TOOLS = frozenset({"read_file", "search_files"})


def _segments(command: str) -> Iterable[list[str]]:
    for segment in re.split(r"[|;&]+", command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if tokens:
            yield tokens


def is_env_dump(command: str | None) -> bool:
    """Whether `command` prints the environment.

    Conservative on purpose: an indirect read (`sudo env`, `$(printenv)`,
    a redirect) is not detected, and the caller then falls back to the safer
    `code_file=True` path, which still masks every recognisable shape.
    """
    if not command or not isinstance(command, str):
        return False
    return any(tokens[0] in _ENV_DUMP_COMMANDS for tokens in _segments(command))


def reads_env_file(command: str | None) -> bool:
    """Whether `command` prints a secret-bearing file to stdout."""
    if not command or not isinstance(command, str):
        return False
    for tokens in _segments(command):
        if tokens[0] not in _FILE_READ_COMMANDS:
            continue
        for argument in tokens[1:]:
            if argument.startswith("-"):
                continue
            basename = argument.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if basename.lower() in _ENV_FILE_BASENAMES:
                return True
    return False


def scrub_tool_result(
    tool_name: str, arguments: dict[str, Any] | None, text: str
) -> Scrubbed:
    """Scrub one tool's output, choosing the pass from what the tool is.

    The single classifier. Every tool result goes through here, so a tool added
    later gets the conservative default without anyone remembering to wire it
    up, and the aggressive passes stay on the two surfaces that earn them.
    """
    arguments = arguments or {}

    if tool_name in _FILE_READ_TOOLS:
        path = str(arguments.get("path") or "")
        secrets = path.rsplit("/", 1)[-1].lower() in _ENV_FILE_BASENAMES
        # A file that exists to hold secrets gets every pass there is: the
        # named-assignment one, and the blanket one that does not care what the
        # key is called. The sentinel mask applies either way — this is content
        # somebody may copy back out.
        return scrub(
            text, file_read=True, code_file=not secrets, secrets_file=secrets
        )

    if tool_name in {"terminal", "process"}:
        command = str(arguments.get("command") or "")
        # `cat .env` is the same output as `printenv` and has to be treated the
        # same way, or blocking one and not the other just teaches the agent
        # which of the two to reach for.
        return scrub(
            text,
            code_file=not is_env_dump(command),
            secrets_file=reads_env_file(command),
        )

    # Everything else — a fetched page, a browser snapshot, an MCP server's
    # reply, a memory hit — is unknown text, and unknown text is prose more
    # often than it is a credential dump. Known values and known shapes still
    # run; the named-assignment pass does not.
    return scrub(text)


# A note appended to a redacted file read, because the sentinel is only useful
# if the reader knows what it means. Without this the model has been observed
# treating `«redacted:sk-…»` as the literal contents and writing it onward.
REDACTION_NOTICE = (
    "\n\n[{count} value(s) above were masked before you saw them. They are not "
    "the real values and must never be copied, written to another file, or sent "
    "anywhere. If the real value is needed, ask the user to move it themselves.]"
)


def notice(result: Scrubbed) -> str:
    return REDACTION_NOTICE.format(count=result.count) if result.changed else ""
