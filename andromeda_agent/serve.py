"""The runner's only door.

A hosted runner is stopped almost all of the time. Something outside it decides
a job is due, and the way it says so is an HTTP request — which is also what
wakes the machine. This module is that request's entire surface.

**One route, and nothing else.** No dashboard, no status page, no metrics
endpoint, no readiness probe. Every additional route on a box that holds a live
credential and runs model-chosen commands is another thing to get exactly
right, and none of them is worth it: the wake is measurable by POSTing this
route without a token and timing the `401`, which proves the machine came up
*and* that the lock works, in one call.

Three decisions worth reading before editing:

**`202` before the work, always.** An agent turn is minutes; an HTTP timeout is
seconds. Answering after the run means every long job looks to the caller like a
delivery failure, gets retried, and — without the claim in `fires.py` — runs
twice. So the request is acknowledged in milliseconds and the job runs on a
background thread. The consequence is that this server looks idle to a load
balancer even while several jobs are in flight, which is why the real
concurrency ceiling is here and not in a proxy.

**No secret, no server.** `ANDROMEDA_FIRE_SECRET` is required to start. The two
alternatives are both worse than refusing: a server with no secret either
accepts everything, or rejects everything while looking healthy. A machine that
boots and then silently answers nothing is the failure that takes longest to
find.

**A stop is a drain, not a kill.** A container runtime stops a machine with
`SIGTERM` and then, some seconds later, `SIGKILL`. The default behaviour of a
Python process under `SIGTERM` is to die immediately, which for this server
means a job's thread vanishes mid-run: the work may have half-happened, nothing
settles the fire, and it surfaces later as `unknown` — the one outcome that
needs a person. So the signal stops *accepting* and waits for what is already
running, and only the `SIGKILL` that follows a too-slow drain can still produce
an unknown. That is the correct trade: a job that will not finish in the grace
period was going to be interrupted regardless, and the difference is whether the
machine got the chance to say so.

**Symmetric HMAC, not an asymmetric token — for now.** The machine only ever
fires its own jobs, so a machine that could forge a fire to itself gains nothing
it could not get by running the job directly. The real risk is a leaked secret
letting an outsider fire this machine's jobs, which is the same risk class as
the device token already on the box. `VERIFIERS` is the seam: an Ed25519/JWKS
verifier drops in with no change to the handler, for the day someone other than
the operator hosts these.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .fires import Fires, Outcome

ROUTE = "/cron/fire"
SECRET_ENV = "ANDROMEDA_FIRE_SECRET"

# Clock skew tolerance on `exp`. Two machines that agree to within half a minute
# is a low bar; one that does not has a problem this module cannot fix, and
# stretching the window to hide it would widen the replay window instead.
LEEWAY_SECONDS = 30

# How many agent turns may be in flight at once. The binding constraint is
# memory — each turn holds a conversation, its tool registry and its transcript
# — so this belongs here, where that cost is visible, and not in a proxy that
# can only count requests. Over the cap the answer is `503`, which is retryable:
# the caller comes back in a moment rather than having its fire accepted and
# then starved behind three others.
DEFAULT_MAX_CONCURRENT = 2
CONCURRENCY_ENV = "ANDROMEDA_MAX_CONCURRENT_JOBS"

# The stop-everything switch, read on **every** fire rather than at boot. Read
# once at startup it would be a switch you have to restart a machine to use,
# which is exactly the wrong shape for the thing you reach for when something is
# running away from you at 3am.
#
# It refuses fires and leaves the server up: a machine that vanishes when the
# switch is thrown looks identical to one that crashed, and the person who
# threw it then cannot tell whether it worked.
CLOUD_OFF_ENV = "ANDROMEDA_CLOUD_OFF"

# How long a drain waits for jobs already running before giving up on them.
# Bounded because the runtime's own grace period is finite and will `SIGKILL`
# regardless; waiting longer than it just means being killed while still
# waiting.
DRAIN_SECONDS = 25

# The body of a fire, and the material the signature covers. Kept as a tuple so
# the signing and verifying sides cannot drift in field order — a mismatch there
# fails closed, but fails closed *silently*, which is a bad afternoon.
#
# **`user_id` is signed, and it has to be.** On a machine-per-user runner the
# tenant was implied by which machine the fire reached, so it never travelled.
# A shared-pool runner serves every tenant from one endpoint, and an unsigned
# `user_id` there means anyone holding one valid fire can replay it with the
# field swapped — claiming, and settling, against somebody else's job. The
# signature is the only thing that makes the tenant claim trustworthy, so the
# tenant belongs inside it.
_SIGNED_FIELDS = ("user_id", "job_id", "fire_at", "exp")


class FireError(RuntimeError):
    """A refusal with an HTTP status attached."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def _payload(user_id: str, job_id: str, fire_at: str, exp: int) -> bytes:
    return "|".join((user_id, job_id, fire_at, str(exp))).encode("utf-8")


def mint(
    secret: str,
    job_id: str,
    fire_at: str,
    ttl_seconds: int = 120,
    user_id: str = "",
) -> dict[str, Any]:
    """A fire, signed. The caller sends `token` as a bearer and the rest as JSON.

    Exposed rather than kept private because the tests and the eventual server
    side must sign exactly what this verifies, and a second implementation of
    that string is a second chance to get it wrong.
    """
    exp = int(time.time()) + ttl_seconds
    return {
        "user_id": user_id,
        "job_id": job_id,
        "fire_at": fire_at,
        "exp": exp,
        "token": hmac.new(
            secret.encode("utf-8"),
            _payload(user_id, job_id, fire_at, exp),
            hashlib.sha256,
        ).hexdigest(),
    }


def verify_hmac(secret: str, body: dict[str, Any], token: str) -> None:
    """Raise `FireError` unless this token signs this body, unexpired.

    Order matters and is deliberate: **presence of a token, then shape, then
    expiry, then signature.**

    The token comes first so a caller who presented no credential is told that,
    rather than critiqued on its formatting. Checking shape first — which an
    earlier version did — answered a bare `POST {}` with "job_id is required",
    and made the runbook's documented wake measurement return 400 where it said
    401. The protection is narrow and honest about it: once a token *string* is
    present the body must be parsed to verify anything at all, because the
    signature covers it.

    A malformed body still must not reach `compare_digest`, and an expired token
    is worth telling apart from a forged one in a log — but both answer `401`,
    because telling an attacker which of the two they achieved is free
    information too.
    """
    if not isinstance(token, str) or not token:
        raise FireError(401, "unauthorized")

    for field in _SIGNED_FIELDS:
        if field not in body:
            raise FireError(400, f"{field} is required")

    try:
        exp = int(body["exp"])
    except (TypeError, ValueError):
        raise FireError(400, "exp must be a number") from None

    if exp + LEEWAY_SECONDS < time.time():
        raise FireError(401, "expired")

    expected = hmac.new(
        secret.encode("utf-8"),
        _payload(
            str(body["user_id"]), str(body["job_id"]), str(body["fire_at"]), exp
        ),
        hashlib.sha256,
    ).hexdigest()

    # `compare_digest` raises on a non-ASCII argument rather than returning
    # False, so a token that is not hex has to be refused before it gets here.
    if not token.isascii():
        raise FireError(401, "bad token")
    if not hmac.compare_digest(expected, token):
        raise FireError(401, "bad token")


def verify_ed25519(public_key: str, body: dict[str, Any], token: str) -> None:
    """Raise `FireError` unless this signature was made by the server's key.

    The asymmetric half, and the reason to have it: with HMAC the runner holds
    the same secret the server signs with, so a compromised runner can forge
    fires for itself. That is tolerable while the operator is the only host —
    forging a fire to yourself gains nothing over running the job — and stops
    being tolerable the moment somebody else runs a runner.

    Here the runner holds only a **public** key. It can check that a fire came
    from the server and cannot produce one, which is the property that makes a
    third-party runner safe to hand a job to.

    Same signed material, same order, same separator as `verify_hmac`. The
    signature scheme changed; the contract did not, which is what lets this drop
    in under `VERIFIERS` with no change to the handler or to either caller.
    """
    if not isinstance(token, str) or not token:
        raise FireError(401, "unauthorized")

    for field in _SIGNED_FIELDS:
        if field not in body:
            raise FireError(400, f"{field} is required")

    try:
        exp = int(body["exp"])
    except (TypeError, ValueError):
        raise FireError(400, "exp must be a number") from None
    if exp + LEEWAY_SECONDS < time.time():
        raise FireError(401, "expired")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        # Refused, never waved through. A missing verifier is the one condition
        # under which "allow it" would be catastrophic, and a deployment that
        # selected this scheme did so on purpose.
        raise FireError(
            503,
            "this runner is configured for ed25519 fires but has no `cryptography` "
            "installed, so it cannot check one. Refusing rather than guessing.",
        ) from None

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(
            bytes.fromhex(token),
            _payload(
                str(body["user_id"]), str(body["job_id"]), str(body["fire_at"]), exp
            ),
        )
    except (InvalidSignature, ValueError):
        # One answer for a bad signature and a malformed one. Telling a caller
        # which of the two it achieved is free information.
        raise FireError(401, "bad token") from None


# The seam, with both halves in it. A deployment picks one with
# `ANDROMEDA_FIRE_SCHEME`; the handler and the callers do not change, which is
# what made deferring the asymmetric infrastructure a decision rather than a
# corner cut.
VERIFIERS: dict[str, Callable[[str, dict[str, Any], str], None]] = {
    "hmac": verify_hmac,
    "ed25519": verify_ed25519,
}

SCHEME_ENV = "ANDROMEDA_FIRE_SCHEME"


def configured_scheme() -> str:
    """Which verifier this runner uses.

    An unrecognised name is **refused**, unlike the `cron_provider` seam where
    an unknown name falls back to the built-in. The two are not the same kind of
    setting: falling back there means jobs still run, and falling back here
    would mean a runner configured for asymmetric fires quietly accepting
    symmetric ones — a downgrade attack spelled as a typo.
    """
    name = (os.environ.get(SCHEME_ENV) or "hmac").strip().lower()
    if name not in VERIFIERS:
        raise FireError(
            0,
            f"{SCHEME_ENV}={name!r} is not a scheme this build knows. "
            f"Known: {', '.join(sorted(VERIFIERS))}.",
        )
    return name


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class Runner:
    """What a fire actually does, separated from how it arrives.

    Injected rather than imported so the transport can be tested without a
    model and the model path without a socket — the same split `runner.execute`
    already uses for its `build`.
    """

    def __init__(
        self,
        fires: Fires,
        resolve: Callable[[str], Any],
        execute: Callable[[Any, str], None],
        max_concurrent: int = 0,
    ) -> None:
        self.fires = fires
        self.resolve = resolve
        self.execute = execute
        self.max_concurrent = max_concurrent or _configured_concurrency()
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self.in_flight = 0
        self.draining = False
        self._counter = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def accept(self, job_id: str, fire_at: str) -> tuple[int, dict[str, Any]]:
        """Decide, claim and start. Returns the response, before the work."""
        # First, and before the job is even looked up. A kill switch that only
        # applies to jobs it can resolve is a kill switch with exceptions.
        if cloud_is_off():
            raise FireError(503, "cloud jobs are switched off on this runner")

        # Draining: already-running work finishes, nothing new starts. `503`
        # rather than a refusal, because the caller should come back — to
        # another machine, or to this one after it restarts.
        if self.draining:
            raise FireError(503, "this runner is shutting down")

        job = self.resolve(job_id)
        if job is None:
            raise FireError(404, "no such job")

        # A job marked `runs_on: device` belongs to the person's own machine.
        # Firing it here would be the exact confusion the location axis exists
        # to prevent: it would run against a workspace this container does not
        # have and report that it found nothing, which is indistinguishable
        # from the watched thing not changing. Found by driving a real fire
        # against a real container, where the first job to hand was a local one.
        if getattr(job, "runs_on", "device") != "cloud":
            raise FireError(
                409,
                "this job runs on the user's own machine, not here. Move it "
                f"with `andromeda cron approve {job_id} --run-on cloud` if that "
                "is what was meant.",
            )

        # Checked before the claim, so a paused job does not burn its one
        # claimable fire on a refusal.
        if not getattr(job, "enabled", True):
            raise FireError(409, "job is disabled")
        if getattr(job, "paused_reason", ""):
            raise FireError(409, f"job is paused: {job.paused_reason}")
        if getattr(job, "retired", False):
            raise FireError(409, "job has finished its run count")

        # Capacity before the claim, for the same reason: refusing after
        # claiming would consume the fire and run nothing, and the caller's
        # retry would then be told the fire was already taken.
        if not self._slots.acquire(blocking=False):
            raise FireError(503, "at capacity")

        try:
            outcome = self.fires.claim(job_id, fire_at)
        except FireError:
            self._slots.release()
            raise
        except BaseException as exc:
            # A claim backend that could not answer is **not** a claim. With a
            # remote claim the server may be unreachable, and treating that as a
            # win is how two containers run the same job — the one failure this
            # whole path exists to prevent. `503` is retryable, so the caller
            # comes back; the cost is a delayed run against a duplicated side
            # effect, and those are not the same size.
            self._slots.release()
            raise FireError(503, f"could not claim this fire: {exc}") from None

        if outcome is Outcome.UNKNOWN:
            self._slots.release()
            raise FireError(
                409,
                "a previous attempt at this fire never reported. Its side "
                "effects may or may not have run, so it is not retried "
                "automatically — `andromeda cron fires --unresolved`",
            )
        if outcome in (Outcome.IN_FLIGHT, Outcome.SETTLED):
            self._slots.release()
            # Accepted, not refused. The caller's contract is "202 means stop
            # retrying", and a duplicate is precisely the case where it should.
            return 202, {"status": "duplicate", "job_id": job_id}

        thread = threading.Thread(
            target=self._work, args=(job, job_id, fire_at), daemon=False
        )
        with self._counter:
            self.in_flight += 1
            self._idle.clear()
        thread.start()
        return 202, {"status": "accepted", "job_id": job_id}

    def _work(self, job: Any, job_id: str, fire_at: str) -> None:
        ok = False
        try:
            self.execute(job, fire_at)
            ok = True
        except BaseException:
            # A failed job must not stop the server, and must still settle:
            # an unsettled fire is indistinguishable from a machine that died,
            # and would be reported as `unknown` forever.
            ok = False
        finally:
            self.fires.settle(job_id, fire_at, ok)
            with self._counter:
                self.in_flight -= 1
                if self.in_flight == 0:
                    self._idle.set()
            self._slots.release()


    def drain(self, timeout: float = DRAIN_SECONDS) -> int:
        """Stop accepting, wait for what is running. Returns what did not finish.

        A non-zero return is not a failure to report loudly — it is the number
        of jobs that are about to be killed by the runtime's `SIGKILL` and will
        therefore surface as `unknown`. Saying it out loud at shutdown is the
        difference between a person finding that in a ledger a week later and
        seeing it in the logs of the deploy that caused it.
        """
        self.draining = True
        self._idle.wait(timeout)
        with self._counter:
            return self.in_flight


def cloud_is_off() -> bool:
    """The stop-everything switch, read fresh every time.

    Anything other than an explicit falsey value counts as on, because the cost
    of misreading this is asymmetric: refusing a fire that should have run is a
    job that is late, and running one that should have been stopped is the thing
    somebody threw the switch to prevent.
    """
    value = (os.environ.get(CLOUD_OFF_ENV) or "").strip().lower()
    return bool(value) and value not in {"0", "false", "no", "off"}


def _configured_concurrency() -> int:
    try:
        value = int(os.environ.get(CONCURRENCY_ENV, "") or DEFAULT_MAX_CONCURRENT)
    except ValueError:
        value = DEFAULT_MAX_CONCURRENT
    return max(1, value)


def handler_for(runner: Runner, secret: str, scheme: str = "hmac"):
    verify = VERIFIERS.get(scheme, verify_hmac)

    class FireHandler(BaseHTTPRequestHandler):
        # The default logs every request to stderr with a reverse-DNS lookup on
        # the peer, which on a machine that wakes for one request is a DNS
        # round-trip on the critical path of every single fire.
        def log_message(self, *_args: Any) -> None:  # noqa: D102
            return

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != ROUTE:
                self._respond(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._respond(400, {"error": "bad length"})
                return
            # A body big enough to matter is not a fire. Read a bounded amount
            # rather than whatever the peer claims to be sending.
            if length > 8192:
                self._respond(400, {"error": "body too large"})
                return

            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                self._respond(400, {"error": "body must be JSON"})
                return
            if not isinstance(body, dict):
                self._respond(400, {"error": "body must be an object"})
                return

            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header[:7].lower() == "bearer " else ""

            try:
                verify(secret, body, token)
                status, payload = runner.accept(
                    str(body["job_id"]), str(body["fire_at"])
                )
            except FireError as exc:
                self._respond(exc.status, {"error": exc.reason})
                return
            except Exception:  # noqa: BLE001 - a bad fire must not kill the server
                self._respond(500, {"error": "internal error"})
                return

            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            # Deliberately not a health endpoint. A `GET` that answered 200
            # would be an unauthenticated way to keep the machine awake.
            self._respond(404, {"error": "not found"})

    return FireHandler


def secret_from_environment(scheme: str = "") -> str:
    """The material this runner verifies with.

    For `hmac` it is a shared secret and must be long. For `ed25519` it is the
    server's **public** key, which is not a secret at all — the length rule is
    dropped there rather than pretended, because a rule that exists for one
    scheme and is enforced on another is a rule people work around.
    """
    scheme = scheme or "hmac"
    secret = (os.environ.get(SECRET_ENV) or "").strip()

    if scheme == "ed25519":
        if not secret:
            raise FireError(
                0,
                f"{SECRET_ENV} must hold the server's ed25519 public key, hex "
                "encoded, when the scheme is ed25519.",
            )
        try:
            if len(bytes.fromhex(secret)) != 32:
                raise ValueError
        except ValueError:
            raise FireError(
                0, f"{SECRET_ENV} is not a 32-byte hex ed25519 public key."
            ) from None
        return secret

    if not secret:
        raise FireError(
            0,
            f"{SECRET_ENV} is not set. A runner with no secret would either "
            "accept every fire or refuse every fire while looking healthy, and "
            "both are worse than not starting.",
        )
    if len(secret) < 32:
        raise FireError(
            0,
            f"{SECRET_ENV} is shorter than 32 characters. It is the only thing "
            "standing between a stranger and this machine's jobs.",
        )
    return secret


def build_server(
    runner: Runner, host: str, port: int, secret: str, scheme: str = "hmac"
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler_for(runner, secret, scheme))
    # A fire's work outlives its request, and the threads doing it are not
    # daemons — a shutdown must not cut a job in half and leave a lease to time
    # out into `unknown`.
    server.daemon_threads = False
    return server
