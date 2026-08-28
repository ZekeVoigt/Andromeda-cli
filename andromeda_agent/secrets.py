"""Credentials that live in a vault instead of in a file.

Until now the only place a BYOK key could come from was the environment, which
in practice means one of three things: a `.env` beside the project, a line in a
shell profile, or an `export` typed at the start of every session. The first two
are a plaintext credential on disk that outlives every reason it was put there;
the third is why people give up and use the first two.

So a config value may name a *reference* instead of a value:

    secrets:
      OPENROUTER_API_KEY: "op://Personal/OpenRouter/credential"
      GITHUB_TOKEN:       "keychain://github-token"
      ANTHROPIC_API_KEY:  "cmd://pass show anthropic/api"

Resolved once, at startup, into the process environment — before anything reads
it. That is what makes it universal rather than per-consumer: the BYOK lane, an
MCP server's `env` block, a hook and a `terminal` call all read the environment,
and none of them need to know a vault was involved.

**References, not sources.** Upstream's equivalent is an orchestrator over
pluggable backends, and most of its size is the problem that creates: a backend
that injects a whole project's worth of variables implicitly needs precedence
rules, conflict detection, shadowing warnings and a provenance table to say
which of four sources won. A reference has exactly one resolver and names
exactly one variable, so none of that exists here. The seam is kept —
`RESOLVERS` is a dict — but the orchestration is not, because there is nothing
to orchestrate.

**Nothing is cached to disk.** Upstream writes a 0600 TTL cache. Somebody who
moved their key into a vault did it so that the value would stop living in a
file; writing it back into one under a different name gives them the file back
and takes away the revocation. The cache here is in-process and dies with it.

**Nothing is ever installed.** A missing helper is named with the command that
would install it. Same rule as the language servers.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import redact

# A helper against an unlocked vault answers in well under a second. This is
# generous enough for a cold `op` process and a network round trip, and short
# enough that a helper which decided to prompt fails the session rather than
# hanging it.
TIMEOUT = 15.0

# How long a resolved value is reused within one process. Long enough that a
# session does not re-shell for every subagent; short enough that rotating a
# credential takes effect within a session rather than at the next restart.
CACHE_TTL = 300.0

REFERENCE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.+)$", re.IGNORECASE)

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Whole CSI and OSC sequences, including an unterminated OSC — a helper killed
# mid-write leaves one, and it would otherwise carry escape codes straight into
# this program's own output.
ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")


class Problem(str, Enum):
    """Why a resolution failed, in a fixed vocabulary.

    Fixed so that the status command and the startup warning say the same thing
    about the same failure, and so a remediation can be chosen by kind rather
    than by matching on a message that will be reworded.
    """

    NO_HELPER = "no_helper"          # the CLI is not installed
    LOCKED = "locked"                # installed, but not signed in or unlocked
    NOT_FOUND = "not_found"          # the reference names nothing
    EMPTY = "empty"                  # it names something, which is blank
    TIMEOUT = "timeout"              # the helper did not answer in time
    BAD_REFERENCE = "bad_reference"  # the reference is malformed
    FAILED = "failed"                # anything else


@dataclass
class Resolution:
    """One reference, resolved or not. Never an exception.

    A vault that is locked at eight in the morning must not stop the session —
    it must say which command unlocks it, and let everything that does not need
    that credential carry on working.
    """

    name: str
    reference: str
    value: str = ""
    problem: Problem | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None and bool(self.value)

    @property
    def remedy(self) -> str:
        """What to run next. Empty when there is nothing useful to say."""
        scheme = scheme_of(self.reference)
        resolver = RESOLVERS.get(scheme)
        if resolver is None:
            return f"Unknown scheme `{scheme}://`. Known: " + ", ".join(sorted(RESOLVERS))
        return resolver.remedy(self.problem, self.reference)


@dataclass
class Report:
    """What one startup pass did, for `andromeda secrets status` to print."""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[Resolution] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Running a helper
# ---------------------------------------------------------------------------


@dataclass
class Output:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing: bool = False


# Passed to every helper. Never a copy of the whole environment: by the time
# this runs the environment holds every credential this process knows about,
# and a helper is a third-party program.
_BASE_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TMPDIR",
    "TEMP",
    "LANG",
    "LC_ALL",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)


def _environment(allow: tuple[str, ...] = ()) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in (*_BASE_ENV, *allow)
        if name in os.environ
    }
    env.setdefault("NO_COLOR", "1")
    return env


def run(
    argv: list[str],
    *,
    allow: tuple[str, ...] = (),
    shell: bool = False,
    timeout: float | None = None,
) -> Output:
    """Run a secret helper. Never raises.

    stdin is `/dev/null` so a helper that decides to prompt for a touch or a
    master password fails immediately instead of hanging a session that may
    have nobody watching it. stderr is captured and ANSI-scrubbed, and it is
    only ever shown as a *reason* — a helper's diagnostics can quote the thing
    it was asked for.
    """
    # Read here rather than defaulted in the signature: a default is bound at
    # import, which made the module constant decorative — a test that lowered
    # it changed nothing, and so would a future config knob.
    if timeout is None:
        timeout = TIMEOUT

    try:
        completed = subprocess.run(  # noqa: S603 - argv list unless `shell`
            argv if not shell else argv[0],
            shell=shell,
            env=_environment(allow),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return Output(ok=False, timed_out=True)
    except FileNotFoundError:
        return Output(ok=False, missing=True)
    except OSError as exc:
        return Output(ok=False, stderr=str(exc))

    return Output(
        ok=completed.returncode == 0,
        stdout=completed.stdout or "",
        stderr=ANSI.sub("", completed.stderr or "").strip(),
    )


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolver:
    """One way of getting a secret out of somewhere.

    `binary` is what has to be installed, `install` is how to install it, and
    `resolve` does the work. A resolver never raises and never prompts.
    """

    scheme: str
    label: str
    binary: str
    install: str
    resolve: Callable[[str], Resolution]
    # Why this scheme cannot follow a job into a hosted container, or "" if it
    # can. Every scheme here except `env://` resolves against *this* machine — a
    # keychain, a signed-in helper, a binary on this PATH — and in a container
    # they do not fail interestingly, they fail as a missing variable at fire
    # time in a log nobody is reading. `cloud.secrets_refusal` reads this at
    # creation, where a person is present. A reason, not a boolean, because the
    # refusal has to say which one of them it is.
    cloud_refusal: str = ""

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def remedy(self, problem: Problem | None, reference: str) -> str:
        if problem is Problem.NO_HELPER:
            return f"{self.label} is not installed — {self.install}"
        if problem is Problem.LOCKED:
            return f"{self.label} is locked or signed out — unlock it and try again."
        if problem is Problem.NOT_FOUND:
            return f"{self.label} has no entry matching `{reference}`."
        if problem is Problem.EMPTY:
            return f"`{reference}` exists in {self.label} but is empty."
        if problem is Problem.TIMEOUT:
            return f"{self.label} did not answer within {TIMEOUT:.0f}s."
        return ""


def _fail(reference: str, problem: Problem, detail: str = "") -> Resolution:
    return Resolution(name="", reference=reference, problem=problem, detail=detail)


def _from_output(reference: str, result: Output, locked_hints: tuple[str, ...]) -> Resolution:
    """One helper's output, as a resolution. Shared by every CLI-backed scheme."""
    if result.missing:
        return _fail(reference, Problem.NO_HELPER)
    if result.timed_out:
        return _fail(reference, Problem.TIMEOUT)
    if not result.ok:
        lowered = result.stderr.lower()
        if any(hint in lowered for hint in locked_hints):
            return _fail(reference, Problem.LOCKED, result.stderr[:200])
        if "not found" in lowered or "no item" in lowered or "isn't an item" in lowered:
            return _fail(reference, Problem.NOT_FOUND, result.stderr[:200])
        return _fail(reference, Problem.FAILED, result.stderr[:200])

    value = result.stdout.strip()
    if not value:
        return _fail(reference, Problem.EMPTY)
    return Resolution(name="", reference=reference, value=value)


def _onepassword(reference: str) -> Resolution:
    """`op://vault/item/field` through the 1Password CLI.

    The reference is passed after `--`, as data, and `op read` takes the whole
    `op://` URI as it was written — so there is no place for a hostile config
    value to become an option.
    """
    return _from_output(
        reference,
        run(["op", "read", "--no-newline", "--", reference], allow=("OP_SERVICE_ACCOUNT_TOKEN",)),
        locked_hints=("not signed in", "authorization", "unlock", "session expired"),
    )


def _bitwarden(reference: str) -> Resolution:
    """`bw://<secret-id>` through the Bitwarden Secrets Manager CLI.

    Secrets Manager rather than the password-manager CLI: this is the one that
    exists for machine credentials, takes an access token from the environment,
    and does not need an interactive unlock.
    """
    identifier = reference.split("://", 1)[1].strip()
    if not identifier:
        return _fail(reference, Problem.BAD_REFERENCE, "no secret id")
    result = run(
        ["bws", "secret", "get", "--output", "none", identifier],
        allow=("BWS_ACCESS_TOKEN",),
    )
    return _from_output(
        reference,
        result,
        locked_hints=("access token", "unauthorized", "401", "authentication"),
    )


def _keychain(reference: str) -> Resolution:
    """`keychain://<service>[/<account>]` through the macOS keychain.

    Worth having on this platform for one reason: it is already there. No
    install, no subscription, no daemon — `security add-generic-password` and
    the credential is out of every file on the machine.
    """
    if os.uname().sysname != "Darwin":
        return _fail(
            reference, Problem.NO_HELPER, "the keychain scheme is macOS only"
        )
    rest = reference.split("://", 1)[1]
    service, _, account = rest.partition("/")
    if not service:
        return _fail(reference, Problem.BAD_REFERENCE, "no service name")

    argv = ["security", "find-generic-password"]
    if account:
        argv += ["-a", account]
    argv += ["-s", service, "-w"]

    result = run(argv)
    if not result.ok and "could not be found" in result.stderr.lower():
        return _fail(reference, Problem.NOT_FOUND)
    return _from_output(reference, result, locked_hints=("user interaction",))


def _command(reference: str) -> Resolution:
    """`cmd://<shell command>` — whatever the user says.

    Run through the shell, because it *is* a shell command and it comes from
    the user's own config file, which is the same trust level as their shell
    profile. What that trust does not extend to is the environment: the helper
    gets the allowlist above and nothing else, so a command that decides to
    `env | curl` cannot exfiltrate the keys this process is already holding.
    """
    command = reference.split("://", 1)[1].strip()
    if not command:
        return _fail(reference, Problem.BAD_REFERENCE, "no command")
    if os.name == "nt":
        return _fail(reference, Problem.NO_HELPER, "the cmd scheme needs a POSIX shell")
    return _from_output(reference, run([command], shell=True), locked_hints=())


def _environment_variable(reference: str) -> Resolution:
    """`env://NAME` — for a value that really is in the environment already.

    Present so that a `secrets:` block can hold every credential in one place
    even when one of them is not in a vault yet, rather than being half a list.
    """
    name = reference.split("://", 1)[1].strip()
    if not ENV_NAME.match(name):
        return _fail(reference, Problem.BAD_REFERENCE, f"{name!r} is not a variable name")
    value = os.environ.get(name, "")
    if not value:
        return _fail(reference, Problem.NOT_FOUND)
    return Resolution(name="", reference=reference, value=value)


def _hosted(reference: str) -> Resolution:
    """A secret this account stores server-side, for jobs that run elsewhere.

    Unlike every other scheme here, this one does not shell out to a helper —
    there is no helper, because the whole point is that the machine running the
    job has none. The value is fetched over the network by whoever is about to
    need it and injected into the process environment.

    **Resolved by the runner, not here.** On a laptop, `andromeda://GITHUB_TOKEN`
    is a reference to something a *cloud* job will be given, and resolving it
    locally would pull a credential onto a machine that did not ask for one.
    So this reports it as recognised-and-not-local rather than fetching, and
    `cloud_client.resolve_secrets` is the only thing that opens it.
    """
    return _fail(
        reference,
        Problem.NO_HELPER,
        "hosted secrets are resolved by the runner, not on this machine",
    )


RESOLVERS: dict[str, Resolver] = {
    "andromeda": Resolver(
        scheme="andromeda",
        label="your hosted secrets",
        # No binary. Named `sh` only because `available()` needs something to
        # find, and a scheme with no local dependency is always available.
        binary="sh",
        install="`andromeda secrets put <NAME> --cloud` stores one",
        resolve=_hosted,
    ),
    "op": Resolver(
        scheme="op",
        label="1Password",
        binary="op",
        install="see https://developer.1password.com/docs/cli",
        resolve=_onepassword,
        cloud_refusal="1Password needs a session on this machine; a container has none",
    ),
    "bw": Resolver(
        scheme="bw",
        label="Bitwarden Secrets Manager",
        binary="bws",
        install="see https://bitwarden.com/help/secrets-manager-cli",
        resolve=_bitwarden,
        cloud_refusal="Bitwarden needs a session on this machine; a container has none",
    ),
    "keychain": Resolver(
        scheme="keychain",
        label="the macOS keychain",
        binary="security",
        install="it ships with macOS",
        resolve=_keychain,
        cloud_refusal="the macOS keychain is on this Mac and nowhere else",
    ),
    "cmd": Resolver(
        scheme="cmd",
        label="your helper command",
        binary="sh",
        install="it ships with your system",
        resolve=_command,
        cloud_refusal="your helper command runs here, against this PATH and this login",
    ),
    "env": Resolver(
        scheme="env",
        label="the environment",
        binary="sh",
        install="it ships with your system",
        resolve=_environment_variable,
    ),
}


def safe_reference(reference: str) -> str:
    """A reference, safe to print.

    `op://Personal/OpenRouter/credential` is a path and prints as written. A
    `cmd://` reference is whatever the user typed, and people do type
    `cmd://printf %s sk-…` while they are working out whether the block works —
    at which point every surface that echoes the reference is echoing a key.
    Found by a test asserting that `secrets get` prints no secret; it was
    printing one, out of the reference rather than the value.
    """
    return redact.scrub(reference, code_file=False, force=True).text


@dataclass(frozen=True)
class _PluginResolver(Resolver):
    """A secret source a plugin registered.

    Overrides `available` for one reason: the inherited check asks whether
    `binary` is on the PATH, and a plugin source has no binary — it is Python
    that is already imported into this process. Left inherited, every plugin
    source would report "not installed" and never be called.
    """

    def available(self) -> bool:
        return True


def _resolver_for(scheme: str) -> "Resolver | None":
    """The resolver for a scheme, built-in first, then plugins.

    Built-in first is not a preference, it is a guard: a plugin that could
    claim `env://` or `keychain://` would be handed every secret this install
    resolves, and it would be handed them without the user ever choosing it —
    references are already written in their config.
    """
    found = RESOLVERS.get(scheme)
    if found is not None:
        return found
    return _plugin_resolvers().get(scheme)


def _plugin_resolvers() -> dict[str, "Resolver"]:
    """Secret sources a plugin registered, wrapped as resolvers.

    `cloud_refusal` is set for all of them, and it is not a placeholder. The
    hosted runner's image is built from this repository and installs no user
    plugins, so a `repo` or `detached` job whose config names a plugin scheme
    would fail at fire time as a missing environment variable, at 3am, in a log
    nobody is reading. Refusing at creation is the same rule the workspace
    trichotomy already follows.
    """
    try:
        from . import plugins as plugins_module
    except ImportError:  # pragma: no cover - half-installed package
        return {}

    built: dict[str, Resolver] = {}
    for scheme, resolve_fn in plugins_module.secret_sources().items():
        if scheme in RESOLVERS:
            # Refused rather than shadowed. See `_resolver_for`.
            continue
        built[scheme] = _PluginResolver(
            scheme=scheme,
            label=f"the {scheme} plugin source",
            binary="",
            install="provided by a plugin — `andromeda plugins list`",
            resolve=resolve_fn,
            cloud_refusal=(
                f"`{scheme}://` comes from a plugin, and the hosted runner "
                f"installs no plugins"
            ),
        )
    return built


def scheme_of(reference: str) -> str:
    found = REFERENCE.match(reference or "")
    return found.group("scheme").lower() if found else ""


def is_reference(value: object) -> bool:
    """Whether `value` names a secret rather than being one.

    Only a scheme this build actually has. A config that says
    `https://example.com/key` means a URL, and treating it as a reference would
    fail with a message about vaults.
    """
    return isinstance(value, str) and _resolver_for(scheme_of(value)) is not None


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()

# Names this process put into its own environment. Kept because "is the shell
# shadowing this reference?" is unanswerable once we have answered it ourselves
# — `andromeda secrets` reported every working reference as shadowed, because
# startup had exported it into the process the command was running in.
_applied: set[str] = set()


def applied_names() -> frozenset[str]:
    """The names this process resolved, as opposed to inherited."""
    with _cache_lock:
        return frozenset(_applied)


def clear_cache() -> None:
    """Drop everything resolved so far. For tests, and for a profile switch."""
    with _cache_lock:
        _cache.clear()
        _applied.clear()


def resolve(name: str, reference: str, *, use_cache: bool = True) -> Resolution:
    """One reference to one value. Never raises.

    The resolved value is registered for redaction on the way out, which is the
    point at which this becomes worth more than an `export`: a credential that
    came from a vault is now masked in every tool result, every transcript and
    every export for the rest of the session, without anyone wiring it up.
    """
    if not ENV_NAME.match(name or ""):
        return Resolution(
            name=name,
            reference=reference,
            problem=Problem.BAD_REFERENCE,
            detail=f"{name!r} is not a valid environment-variable name",
        )

    found = REFERENCE.match(reference or "")
    if not found:
        return Resolution(
            name=name,
            reference=reference,
            problem=Problem.BAD_REFERENCE,
            detail="a reference looks like `scheme://…`",
        )

    resolver = _resolver_for(found.group("scheme").lower())
    if resolver is None:
        return Resolution(
            name=name,
            reference=reference,
            problem=Problem.BAD_REFERENCE,
            detail=f"no resolver for `{found.group('scheme')}://`",
        )

    if use_cache:
        with _cache_lock:
            cached = _cache.get(reference)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return Resolution(name=name, reference=reference, value=cached[1])

    if not resolver.available():
        return Resolution(
            name=name, reference=reference, problem=Problem.NO_HELPER
        )

    result = resolver.resolve(reference)
    result.name = name

    if result.ok:
        with _cache_lock:
            _cache[reference] = (time.time(), result.value)
        redact.register_known(result.value, name)

    return result


def apply(
    mapping: dict[str, str] | None,
    *,
    environ: dict[str, str] | None = None,
    override: bool = False,
) -> Report:
    """Resolve a `secrets:` block into the environment. Never raises.

    Something the shell already set wins, unless `override`. That order is the
    one people expect and the one that keeps a vault from breaking a debugging
    session: `OPENROUTER_API_KEY=sk-test andromeda` has to work whatever the
    config says, or the config becomes something to comment out.
    """
    report = Report()
    if not mapping:
        return report
    target = os.environ if environ is None else environ

    for name, reference in mapping.items():
        name = str(name)
        if not override and target.get(name):
            report.skipped.append(name)
            continue

        result = resolve(name, str(reference))
        if result.ok:
            target[name] = result.value
            report.applied.append(name)
            if target is os.environ:
                with _cache_lock:
                    _applied.add(name)
        else:
            report.failures.append(result)

    return report


def from_config(config: dict) -> dict[str, str]:
    """The `secrets:` block, defensively.

    A hand-edited file can hold anything, and a malformed block must produce a
    message rather than a traceback on the startup path.

    Entries whose value is not a reference are dropped rather than used — see
    :func:`literal_values`, which is what says so out loud.
    """
    block = config.get("secrets")
    if not isinstance(block, dict):
        return {}
    return {
        str(name): str(reference).strip()
        for name, reference in block.items()
        if is_reference(reference)
    }


def literal_values(config: dict) -> list[str]:
    """Names in the `secrets:` block whose value is not a reference.

    Almost always a pasted credential. Someone reads the example, understands
    that this is where keys go, and puts the key there — at which point the
    file that this whole feature exists to keep credentials *out* of has a
    credential in it, and `config.yaml` is documented as safe to print and to
    commit to a dotfiles repo.

    So it is never treated as a value to use, and it is reported by name — but
    never with the value, because the report would then be the leak.
    """
    block = config.get("secrets")
    if not isinstance(block, dict):
        return []
    return [
        str(name)
        for name, value in block.items()
        if not is_reference(value) and str(value or "").strip()
    ]
