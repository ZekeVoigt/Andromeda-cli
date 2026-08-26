"""Shell scripts as hooks.

The `hooks:` block of `config.yaml` names a script per event. This module
parses that block, asks the user once per `(event, command)` pair whether the
script may run, and registers a callback on the hook bus so every fire site in
`hooks.py` reaches the script with no knowledge that it exists.

Consent is the reason this file is as long as it is. A hook runs with the
user's full credentials, from a file that a `git pull` can change under them,
at a moment nobody is watching. So:

  * an unseen `(event, command)` pair is refused until a person says yes at a
    TTY, or the run carries an explicit `--accept-hooks`;
  * the approval records the script's mtime, and `hooks doctor` reports when
    the file has changed since — approval is for the script that was reviewed,
    not for the path;
  * a non-interactive run with no opt-in registers nothing at all, rather than
    defaulting to trust because there is nobody to ask.

The wire protocol
-----------------
**stdin** — one JSON object::

    {
      "hook_event_name": "pre_tool_call",
      "tool_name":       "terminal",
      "tool_input":      {"command": "rm -rf /"},
      "session_id":      "01J...",
      "cwd":             "/Users/me/project",
      "extra":           {...}       # every other kwarg the event carries
    }

**stdout** — one JSON object, or nothing. Anything unrecognised is ignored::

    {"action": "block",  "message": "Not on main"}     # pre_tool_call
    {"decision": "block", "reason": "Not on main"}     # same, other spelling
    {"action": "modify", "args": {"command": "ls"}}    # rewrite the call
    {"decision": "modify", "tool_input": {...}}        # same, other spelling
    {"action": "approve", "message": "confirm this",
     "rule_key": "terminal:git-push"}                  # escalate to the gate
    {"context": "Today is Friday"}                     # pre_llm_call
    {"output": "..."}                                  # transform_* events

Both spellings are accepted on purpose. People arrive with scripts written for
other harnesses, and a hook that silently does nothing because it said
`decision` instead of `action` is the worst failure this feature can have.

**exit codes** — exit 2 blocks a `pre_tool_call`, with or without stdout, so a
one-line guard script needs no JSON at all. The message is taken from stdout
block JSON if present, then the first 400 characters of stderr, then a
default. Every other non-zero exit is logged and stdout is still parsed.

**failure** — hooks fail *open*: a missing script, a timeout or garbage on
stdout logs a warning and contributes nothing. An entry may set
`fail_closed: true` to invert that for `pre_tool_call`, which is what a
secret scanner or a policy check wants: a crashed gate must not read as
permission.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import hooks

try:  # POSIX only. Without it, cross-process locking degrades to in-process.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
ALLOWLIST_FILENAME = "shell-hooks-allowlist.json"

# Exit code that means "block", independent of stdout.
BLOCK_EXIT_CODE = 2

DEFAULT_BLOCK_MESSAGE = "Blocked by a shell hook."

# How much stderr may become a block message, or a log line.
STDERR_MESSAGE_LIMIT = 400

# Sub-keys under `hooks:` that are settings rather than event names.
RESERVED_KEYS: frozenset[str] = frozenset()

# (event, matcher, command) triples already wired in this process. The matcher
# is part of the key because the same script may legitimately register once per
# tool it gates. A second attempt at the same triple is a no-op, so start-up
# paths can all call `register_from_config` without coordinating.
_registered: set[tuple[str, str | None, str]] = set()
_registered_lock = threading.Lock()

# Guards the allowlist read-modify-write where `fcntl` is unavailable. Separate
# from `_registered_lock` on purpose: registration holds that one while it
# records an approval, and reusing it here would deadlock on a non-reentrant
# lock.
_allowlist_write_lock = threading.Lock()


@dataclass
class ShellHookSpec:
    """One validated `hooks:` entry."""

    event: str
    command: str
    matcher: str | None = None
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    fail_closed: bool = False
    compiled_matcher: re.Pattern[str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # YAML folding leaves whitespace in odd places, and a matcher of
        # " terminal" fails to match "terminal" with no diagnostic at all.
        if isinstance(self.matcher, str):
            stripped = self.matcher.strip()
            self.matcher = stripped or None
        if self.matcher:
            try:
                self.compiled_matcher = re.compile(self.matcher)
            except re.error as exc:
                logger.warning(
                    "hook matcher %r is not a valid regex (%s) — matching it "
                    "as a literal name instead",
                    self.matcher,
                    exc,
                )
                self.compiled_matcher = None

    def matches_tool(self, tool_name: str | None) -> bool:
        if not self.matcher:
            return True
        if tool_name is None:
            return False
        if self.compiled_matcher is not None:
            return self.compiled_matcher.fullmatch(tool_name) is not None
        return tool_name == self.matcher


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_from_config(
    config: dict[str, Any] | None, *, accept_hooks: bool = False
) -> list[ShellHookSpec]:
    """Wire every configured, allowlisted hook onto the bus.

    Returns the specs that ended up registered. Everything skipped — unknown
    event, malformed entry, not allowlisted, already registered — is logged
    and omitted rather than raised: a broken hook must not stop the agent
    starting.
    """
    if not isinstance(config, dict):
        return []

    if _env_enabled("ANDROMEDA_SAFE_MODE"):
        # Safe mode exists to answer "is it me or is it my config?", so it has
        # to skip hooks along with everything else the user added.
        logger.info("ANDROMEDA_SAFE_MODE=1 — shell hooks not registered")
        return []

    effective_accept = resolve_effective_accept(config, accept_hooks)
    specs = iter_configured_hooks(config)
    if not specs:
        return []

    registered: list[ShellHookSpec] = []

    for spec in specs:
        key = (spec.event, spec.matcher, spec.command)
        with _registered_lock:
            if key in _registered:
                continue
            already_allowed = is_allowlisted(spec.event, spec.command)

        # The prompt runs outside the lock: it blocks on a human, and holding
        # a lock across that parks every other thread that wants to register.
        if not already_allowed:
            if not _prompt_and_record(
                spec.event, spec.command, accept_hooks=effective_accept
            ):
                logger.warning(
                    "hook %s -> %s is not allowlisted and was skipped. Approve "
                    "it at the prompt next run, or start once with "
                    "--accept-hooks.",
                    spec.event,
                    spec.command,
                )
                continue

        with _registered_lock:
            # Re-checked because two callers can race through the prompt.
            if key in _registered:
                continue
            hooks.register(spec.event, make_callback(spec))
            _registered.add(key)
            registered.append(spec)
            logger.info(
                "hook registered: %s -> %s (matcher=%s, timeout=%ds, "
                "fail_closed=%s)",
                spec.event,
                spec.command,
                spec.matcher,
                spec.timeout,
                spec.fail_closed,
            )

    return registered


def iter_configured_hooks(config: dict[str, Any] | None) -> list[ShellHookSpec]:
    """Parse the `hooks:` block without registering anything."""
    if not isinstance(config, dict):
        return []
    return _parse_hooks_block(config.get("hooks"))


def reset_for_tests() -> None:
    """Forget what this process has registered. Tests and config reloads."""
    with _registered_lock:
        _registered.clear()


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_effective_accept(config: dict[str, Any], accept_hooks: bool) -> bool:
    """Combine the three ways to say "do not ask me about hooks".

    Any one of them is enough: the `--accept-hooks` flag, the
    `ANDROMEDA_ACCEPT_HOOKS` environment variable, or `hooks_auto_accept: true`
    in the config.
    """
    if accept_hooks:
        return True
    if _env_enabled("ANDROMEDA_ACCEPT_HOOKS"):
        return True
    value = config.get("hooks_auto_accept", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _parse_hooks_block(raw: Any) -> list[ShellHookSpec]:
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "hooks: must be a mapping of event name to a list of hooks; "
                "got %s",
                type(raw).__name__,
            )
        return []

    specs: list[ShellHookSpec] = []

    for event, entries in raw.items():
        if event in RESERVED_KEYS:
            continue
        if event in hooks.SHELL_UNSUPPORTED_HOOKS:
            # Registering would "work" while the script's answer went nowhere.
            logger.warning(
                "hook event %r cannot be driven from a shell script — its "
                "return value has no channel in the wire protocol, so the "
                "entry is refused rather than silently ignored",
                event,
            )
            continue
        if event not in hooks.VALID_HOOKS:
            near = difflib.get_close_matches(
                str(event), sorted(hooks.VALID_HOOKS), n=1, cutoff=0.6
            )
            if near:
                logger.warning(
                    "unknown hook event %r in hooks: — did you mean %r?",
                    event,
                    near[0],
                )
            else:
                logger.warning(
                    "unknown hook event %r in hooks: (valid: %s)",
                    event,
                    ", ".join(sorted(hooks.VALID_HOOKS)),
                )
            continue

        if entries is None:
            continue
        if not isinstance(entries, list):
            logger.warning(
                "hooks.%s must be a list of hook definitions; got %s",
                event,
                type(entries).__name__,
            )
            continue

        for index, entry in enumerate(entries):
            spec = _parse_entry(event, index, entry)
            if spec is not None:
                specs.append(spec)

    return specs


def _parse_entry(event: str, index: int, raw: Any) -> ShellHookSpec | None:
    if not isinstance(raw, dict):
        logger.warning(
            "hooks.%s[%d] must be a mapping with a 'command'; got %s",
            event,
            index,
            type(raw).__name__,
        )
        return None

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        logger.warning("hooks.%s[%d] has no non-empty 'command'", event, index)
        return None

    matcher = raw.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        logger.warning(
            "hooks.%s[%d].matcher must be a string regex; ignoring it",
            event,
            index,
        )
        matcher = None

    if matcher is not None and event not in hooks.TOOL_SCOPED_EVENTS:
        # Said out loud, because the silent version is a hook the user thinks
        # is narrowed to one tool firing on every event of its kind.
        logger.warning(
            "hooks.%s[%d].matcher=%r is ignored — a matcher only applies to "
            "the tool-scoped events (%s). This hook will fire on every %s.",
            event,
            index,
            matcher,
            ", ".join(sorted(hooks.TOOL_SCOPED_EVENTS)),
            event,
        )
        matcher = None

    timeout_raw = raw.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        logger.warning(
            "hooks.%s[%d].timeout must be a whole number of seconds (got %r); "
            "using %ds",
            event,
            index,
            timeout_raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        timeout = DEFAULT_TIMEOUT_SECONDS

    if timeout < 1:
        logger.warning(
            "hooks.%s[%d].timeout must be at least 1s; using %ds",
            event,
            index,
            DEFAULT_TIMEOUT_SECONDS,
        )
        timeout = DEFAULT_TIMEOUT_SECONDS
    elif timeout > MAX_TIMEOUT_SECONDS:
        logger.warning(
            "hooks.%s[%d].timeout=%ds is above the %ds maximum; clamping",
            event,
            index,
            timeout,
            MAX_TIMEOUT_SECONDS,
        )
        timeout = MAX_TIMEOUT_SECONDS

    # `failClosed` is accepted so a config written for another harness works.
    raw_fail_closed = raw.get("fail_closed", raw.get("failClosed", False))
    if not isinstance(raw_fail_closed, bool):
        logger.warning(
            "hooks.%s[%d].fail_closed must be true or false (got %r); "
            "failing open",
            event,
            index,
            raw_fail_closed,
        )
        raw_fail_closed = False
    fail_closed = raw_fail_closed

    if fail_closed and event not in hooks.BLOCKING_EVENTS:
        logger.warning(
            "hooks.%s[%d].fail_closed is ignored — only %s can block, so "
            "there is nothing for a failure to close. This hook fails open.",
            event,
            index,
            ", ".join(sorted(hooks.BLOCKING_EVENTS)),
        )
        fail_closed = False

    return ShellHookSpec(
        event=event,
        command=command.strip(),
        matcher=matcher,
        timeout=timeout,
        fail_closed=fail_closed,
    )


# ---------------------------------------------------------------------------
# Running one hook
# ---------------------------------------------------------------------------

# Keys that get their own place in the payload rather than landing in `extra`.
TOP_LEVEL_PAYLOAD_KEYS = frozenset({"tool_name", "args", "session_id", "parent_session_id"})


def serialize_payload(event: str, kwargs: dict[str, Any]) -> str:
    """Render the stdin JSON. Values that will not serialise are stringified
    rather than dropped — a script seeing `"<Workspace ...>"` can at least say
    so, where a missing key looks like the harness never sent one."""
    extra = {k: v for k, v in kwargs.items() if k not in TOP_LEVEL_PAYLOAD_KEYS}
    try:
        cwd = str(Path.cwd())
    except OSError:
        cwd = ""
    args = kwargs.get("args")
    payload = {
        "hook_event_name": event,
        "tool_name": kwargs.get("tool_name"),
        "tool_input": args if isinstance(args, dict) else None,
        "session_id": kwargs.get("session_id") or kwargs.get("parent_session_id") or "",
        "cwd": cwd,
        "extra": extra,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Take down the hook and anything it started.

    Killing only the direct child leaves helpers running — and, because they
    hold the write end of the pipe, holding the drain open behind them.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def spawn(spec: ShellHookSpec, stdin_json: str) -> dict[str, Any]:
    """Run the command with the payload on stdin.

    Every outcome returns the same keys, so callers never have to ask which
    kind of failure they got before they can read the result.
    """
    result: dict[str, Any] = {
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    try:
        argv = shlex.split(os.path.expanduser(spec.command))
    except ValueError as exc:
        result["error"] = f"command {spec.command!r} cannot be parsed: {exc}"
        return result
    if not argv:
        result["error"] = "empty command"
        return result

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            # Its own process group, so a timeout can reap the whole tree. A
            # hook that finishes in time keeps its children: `some-daemon &`
            # from a hook is a deliberate thing to do.
            start_new_session=True,
            env={**os.environ, "ANDROMEDA_HOOK_EVENT": spec.event},
        )
    except FileNotFoundError:
        result["error"] = "command not found"
        return result
    except PermissionError:
        result["error"] = "command not executable"
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result

    try:
        stdout, stderr = proc.communicate(input=stdin_json, timeout=spec.timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:  # noqa: BLE001 - already killed; drain is best effort
            pass
        result["timed_out"] = True
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result
    except Exception as exc:  # noqa: BLE001 - defensive
        _kill_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:  # noqa: BLE001
            pass
        result["error"] = str(exc)
        return result

    result["returncode"] = proc.returncode
    result["stdout"] = stdout or ""
    result["stderr"] = stderr or ""
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def make_callback(spec: ShellHookSpec) -> Callable[..., dict[str, Any] | None]:
    """The closure the bus calls on every firing of this event."""

    def callback(**kwargs: Any) -> dict[str, Any] | None:
        if spec.event in hooks.TOOL_SCOPED_EVENTS and not spec.matches_tool(
            kwargs.get("tool_name")
        ):
            return None
        outcome = spawn(spec, serialize_payload(spec.event, kwargs))
        return evaluate(spec, outcome)

    callback.__name__ = f"shell_hook[{spec.event}:{spec.command}]"
    callback.__qualname__ = callback.__name__
    return callback


def _fail_closed_block(spec: ShellHookSpec, reason: str) -> dict[str, Any]:
    return {
        "action": "block",
        "message": f"hook {spec.command} failed closed: {reason}",
    }


def evaluate(spec: ShellHookSpec, outcome: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a `spawn` result into what the hook contributes.

    The single place the failure semantics live, so `hooks test` shows exactly
    what the dispatcher would have received rather than an approximation of it.
    """
    blocking = spec.event in hooks.BLOCKING_EVENTS
    fail_closed = spec.fail_closed and blocking

    if outcome["error"]:
        logger.warning(
            "hook failed (event=%s command=%s): %s",
            spec.event,
            spec.command,
            outcome["error"],
        )
        return _fail_closed_block(spec, outcome["error"]) if fail_closed else None

    if outcome["timed_out"]:
        logger.warning(
            "hook timed out after %.2fs (event=%s command=%s)",
            outcome["elapsed_seconds"],
            spec.event,
            spec.command,
        )
        if fail_closed:
            return _fail_closed_block(spec, f"timed out after {spec.timeout}s")
        return None

    stderr = (outcome["stderr"] or "").strip()
    if stderr:
        logger.debug(
            "hook stderr (event=%s command=%s): %s",
            spec.event,
            spec.command,
            stderr[:STDERR_MESSAGE_LIMIT],
        )

    if outcome["returncode"] == BLOCK_EXIT_CODE and blocking:
        parsed = parse_response(spec.event, outcome["stdout"])
        if isinstance(parsed, dict) and parsed.get("action") == "block":
            return parsed
        message = stderr[:STDERR_MESSAGE_LIMIT] or DEFAULT_BLOCK_MESSAGE
        logger.info(
            "hook exited %d — blocking (event=%s command=%s): %s",
            BLOCK_EXIT_CODE,
            spec.event,
            spec.command,
            message,
        )
        return {"action": "block", "message": message}

    if outcome["returncode"] != 0:
        # Logged, but stdout is still read: a script may exit non-zero *and*
        # return a directive, and dropping it would punish the honest one.
        logger.warning(
            "hook exited %s (event=%s command=%s); stderr=%s",
            outcome["returncode"],
            spec.event,
            spec.command,
            stderr[:STDERR_MESSAGE_LIMIT],
        )

    stdout = (outcome["stdout"] or "").strip()
    parsed = parse_response(spec.event, stdout)

    if parsed is None and fail_closed and stdout:
        # A gate that printed a stack trace has not allowed anything.
        try:
            valid_json = isinstance(json.loads(stdout), dict)
        except json.JSONDecodeError:
            valid_json = False
        if not valid_json:
            return _fail_closed_block(
                spec, "unparseable stdout (expected a JSON object)"
            )

    return parsed


def _message(primary: Any, secondary: Any) -> str:
    raw = primary or secondary
    return raw if isinstance(raw, str) and raw else DEFAULT_BLOCK_MESSAGE


def parse_response(event: str, stdout: str) -> dict[str, Any] | None:
    """Translate a script's stdout into the shape the bus expects.

    Both spellings of every directive are accepted here and normalised to one.
    This is the most load-bearing function in the file: skip the translation
    and every block written the other way round silently does nothing.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning(
            "hook stdout was not valid JSON (event=%s): %s", event, stdout[:200]
        )
        return None

    if not isinstance(data, dict):
        return None

    if event == "pre_tool_call":
        if data.get("action") == "block":
            return {
                "action": "block",
                "message": _message(data.get("message"), data.get("reason")),
            }
        if data.get("decision") == "block":
            return {
                "action": "block",
                "message": _message(data.get("reason"), data.get("message")),
            }
        if data.get("action") == "approve":
            approve: dict[str, Any] = {"action": "approve"}
            message = data.get("message") or data.get("reason")
            if isinstance(message, str) and message.strip():
                approve["message"] = message.strip()
            rule_key = data.get("rule_key")
            if isinstance(rule_key, str) and rule_key.strip():
                approve["rule_key"] = rule_key.strip()
            return approve
        if data.get("action") == "modify":
            new_args = data.get("args")
            if isinstance(new_args, dict):
                return {"action": "modify", "args": new_args}
        if data.get("decision") == "modify":
            new_args = data.get("tool_input")
            if isinstance(new_args, dict):
                return {"action": "modify", "args": new_args}
        return None

    if event in hooks.TRANSFORM_EVENTS:
        output = data.get("output")
        if isinstance(output, str) and output.strip():
            return {"output": output}
        return None

    context = data.get("context")
    if isinstance(context, str) and context.strip():
        return {"context": context}

    return None


def run_once(spec: ShellHookSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Fire one hook against a synthetic payload, for `hooks test`/`doctor`.

    Routed through the same `serialize_payload` and `evaluate` as a live
    firing, so a script that passes here behaves the same in a real session.
    Anything less makes the test command a second implementation that can
    drift from the first.
    """
    outcome = spawn(spec, serialize_payload(spec.event, kwargs))
    outcome["parsed"] = evaluate(spec, outcome)
    return outcome


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


def allowlist_path() -> Path:
    from andromeda_cli import config as config_module

    return config_module.home() / ALLOWLIST_FILENAME


def load_allowlist() -> dict[str, Any]:
    try:
        raw = json.loads(allowlist_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"approvals": []}
    if not isinstance(raw, dict):
        return {"approvals": []}
    if not isinstance(raw.get("approvals"), list):
        raw["approvals"] = []
    return raw


def save_allowlist(data: dict[str, Any]) -> None:
    """Write via a temp file and a rename, so a crash mid-write cannot leave a
    half-written allowlist that reads as "nothing is approved"."""
    path = allowlist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "could not write the hook allowlist to %s: %s. The approval holds "
            "for this run; the next start will ask again.",
            path,
            exc,
        )


@contextmanager
def _locked_approvals() -> Iterator[dict[str, Any]]:
    """Serialise read-modify-write across processes.

    Two terminals starting at once is the ordinary case, not the exotic one,
    and without this the second write drops the first one's approval.
    """
    path = allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover - non-POSIX
        with _allowlist_write_lock:
            data = load_allowlist()
            yield data
            save_allowlist(data)
        return

    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = load_allowlist()
            yield data
            save_allowlist(data)
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def is_allowlisted(event: str, command: str) -> bool:
    return entry_for(event, command) is not None


def entry_for(event: str, command: str) -> dict[str, Any] | None:
    for entry in load_allowlist().get("approvals", []):
        if (
            isinstance(entry, dict)
            and entry.get("event") == event
            and entry.get("command") == command
        ):
            return entry
    return None


def _prompt_and_record(event: str, command: str, *, accept_hooks: bool) -> bool:
    if accept_hooks:
        record_approval(event, command)
        logger.info("hook approved without a prompt: %s -> %s", event, command)
        return True

    import sys

    if not sys.stdin.isatty():
        # Nobody to ask. Refusing is the only safe answer: a cron run must not
        # be the moment a new script in a pulled config first executes.
        return False

    print(
        f"\n  Andromeda is about to register a shell hook. It will run a "
        f"command on your behalf.\n\n"
        f"    Event:   {event}\n"
        f"    Command: {command}\n\n"
        f"  It runs with your credentials. Approve only what you trust."
    )
    try:
        answer = input("Allow this hook to run? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer in {"y", "yes"}:
        record_approval(event, command)
        return True
    return False


def record_approval(event: str, command: str) -> None:
    entry = {
        "event": event,
        "command": command,
        "approved_at": _now_iso(),
        # What the file looked like when a person read it. `hooks doctor`
        # compares this so an edited script is surfaced rather than inherited.
        "script_mtime_at_approval": script_mtime_iso(command),
    }
    with _locked_approvals() as data:
        data["approvals"] = [
            existing
            for existing in data.get("approvals", [])
            if not (
                isinstance(existing, dict)
                and existing.get("event") == event
                and existing.get("command") == command
            )
        ] + [entry]


def revoke(command: str) -> int:
    """Drop every approval for `command`; return how many went.

    Callbacks already live in a running process stay live — this only stops
    the next start from registering them.
    """
    with _locked_approvals() as data:
        before = len(data.get("approvals", []))
        data["approvals"] = [
            entry
            for entry in data.get("approvals", [])
            if not (isinstance(entry, dict) and entry.get("command") == command)
        ]
        after = len(data["approvals"])
    return before - after


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Inspecting a command without running it
# ---------------------------------------------------------------------------

SCRIPT_EXTENSIONS: tuple[str, ...] = (
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".py",
    ".pyw",
    ".rb",
    ".pl",
    ".lua",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
)


def command_script_path(command: str) -> str:
    """The script inside a command line.

    `python3 ~/hooks/guard.py`, `/usr/bin/env bash guard.sh` and a bare
    `./guard.sh` all have to resolve to the file a person would open.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if not parts:
        return command
    for part in parts:
        if part.lower().endswith(SCRIPT_EXTENSIONS):
            return part
    for part in parts:
        if "/" in part or part.startswith("~"):
            return part
    return parts[0]


def script_mtime_iso(command: str) -> str | None:
    path = command_script_path(command)
    if not path:
        return None
    try:
        expanded = os.path.expanduser(path)
        return (
            datetime.fromtimestamp(os.path.getmtime(expanded), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        return None


def script_is_executable(command: str) -> bool:
    """Whether the command can actually run as written.

    A bare `./guard.sh` needs the execute bit. `python3 guard.py` does not —
    the interpreter only has to read it. Checking for `+x` in both cases
    reports a healthy hook as broken, which teaches people to ignore doctor.
    """
    path = command_script_path(command)
    if not path:
        return False
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    bare = bool(argv) and argv[0] == path
    return os.access(expanded, os.X_OK if bare else os.R_OK)
