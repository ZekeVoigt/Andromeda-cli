"""Talking to the server about cloud jobs.

Three statements, and the whole module is these three:

  "arm this job"            a job exists and wants firing at this time
  "forget this job"         it does not any more
  "here is what happened"   a fire finished, and this is its outcome

The last one is the one that matters most and is the easiest to skip. `cron
serve` writes a run's output to the container's volume, which on a hosted runner
is a disk the person has never seen and cannot reach. A scheduler nobody hears
from is a cron job writing to `/dev/null` — a failure this system already made
once locally and fixed by writing output files. On a runner the output file is
not enough, because the file is somewhere else.

**Every call fails soft.** A job that ran correctly must not be reported as
failed because the network was down while we tried to say so, and a job the user
created locally must not be refused because the arming call timed out. So each
function returns a reason rather than raising, and the callers say what happened
without stopping. The cost of a lost report is one run missing from a list; the
cost of raising is a working scheduler that looks broken.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import redact

# Short. These calls sit on the end of a run, and a person waiting on a terminal
# should never be held up by a server that is not answering.
TIMEOUT_SECONDS = 15

# Extra headers, as a JSON object, for reaching a deployment that sits behind
# something. The case this exists for is a hosting provider's preview
# protection: a preview build is the only place a change can be verified before
# it reaches production, and it answers a redirect to an SSO page rather than
# the route — which is indistinguishable from the route being broken.
#
# Deliberately additive-only and deliberately not a config key. It cannot
# replace `Authorization` or `X-Device-Id`, because a setting that could
# overwrite the credential would be a way to make a machine act as another; and
# it lives in the environment because it belongs to one invocation against one
# preview, not to an install.
EXTRA_HEADERS_ENV = "ANDROMEDA_EXTRA_HEADERS"

_RESERVED_HEADERS = {"authorization", "x-device-id", "content-type"}

JOBS_PATH = "/api/cloud/jobs"
RUNS_PATH = "/api/cloud/runs"
# Where this machine asks what it should be running. The only route through
# which the local scheduler learns anything is due (D15).
DUE_PATH = "/api/cloud/due"


def _extra_headers() -> dict[str, str]:
    """Additional headers from the environment, minus anything load-bearing.

    Malformed JSON is ignored rather than raised: this is a testing convenience,
    and a typo in it must not stop a scheduled job from reporting its run.
    """
    import os

    raw = (os.environ.get(EXTRA_HEADERS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in parsed.items()
        if str(k).lower() not in _RESERVED_HEADERS
    }


class CloudUnavailable(RuntimeError):
    """The server could not be reached or refused. Carries a printable reason."""


def _request(
    base_url: str,
    token: str,
    device_id: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not base_url or not token or not device_id:
        raise CloudUnavailable(
            "this machine is not signed in — `andromeda auth login`"
        )

    # The token is registered with the redactor before it can reach a log, an
    # error string or a transcript. A credential that leaks through an
    # exception message is still a leaked credential.
    redact.register_known(token, "device-token")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
    }
    for name, value in _extra_headers().items():
        headers[name] = value

    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("error", "")
        except Exception:  # noqa: BLE001 - an error page is not always JSON
            detail = ""
        # The server's own words when it has them: a plan limit refusal is
        # worth reading, and "HTTP 400" is not.
        raise CloudUnavailable(detail or f"the server answered {exc.code}") from None
    except urllib.error.URLError as exc:
        raise CloudUnavailable(f"could not reach {base_url}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise CloudUnavailable(f"could not reach {base_url}: {exc}") from None


def _placement_of(job: Any) -> str:
    """Where this job's fires go. `device` or `cloud`, never `auto`.

    `Job.placement()` resolves it properly and is the answer wherever it exists.
    The fallback is for anything job-shaped that is not a `Job` — the server has
    to be told something, and reading a raw `auto` as `device` is the safe way
    to be wrong: a job that runs here when it could have run hosted is late and
    correct, where the reverse is a job with no filesystem looking for one.
    """
    resolve = getattr(job, "placement", None)
    if callable(resolve):
        try:
            return str(resolve())
        except Exception:  # noqa: BLE001 - fall through to the raw field
            pass
    return "cloud" if str(getattr(job, "runs_on", "")) == "cloud" else "device"


def push_job(base_url: str, token: str, device_id: str, job: Any) -> None:
    """Tell the server this job exists and when it next wants firing.

    **Every job, not only the hosted ones.** That is D15: the server holds the
    clock for everything, and this machine executes. A job it has not heard of
    is a job nothing will ever decide is due, because the tick loop no longer
    reaches that conclusion on its own.

    What travels is the timing, the placement, and the definition. The
    placement, because the server has to know which of two very different
    deliveries a job wants — a POST to a runner, or a row a laptop comes and
    asks for. The definition, because a hosted runner has never met this
    machine; a device job's copy is redundant there and sent anyway, so one
    shape covers both and there is no second path to keep correct.
    """
    _request(
        base_url,
        token,
        device_id,
        "POST",
        JOBS_PATH,
        {
            "jobId": job.id,
            "name": job.name,
            "schedule": job.schedule,
            # Resolved, not raw. `auto` is a question — "wherever makes sense" —
            # and the server needs the answer, because it cannot re-run
            # `resolve_placement` without the workspace this machine has.
            "runsOn": _placement_of(job),
            # The pin (D38). A job touching this machine's files must run on
            # this machine, so it is claimed by this device and no other.
            "deviceId": device_id if _placement_of(job) == "device" else None,
            # Milliseconds: the server's scheduler works in them, and a unit
            # mismatch here arms a fire fifty years out with no error anywhere.
            "nextRunAt": int(job.next_run_at * 1000),
            # The definition, so a runner that has never seen this machine can
            # run it. Its run history is stripped: that is this machine's own
            # account of what happened and grows without bound, and the server
            # keeps outcomes in `cloudRuns` already.
            "spec": {k: v for k, v in job.to_json().items() if k != "runs"},
        },
    )


def due_jobs(
    base_url: str,
    token: str,
    device_id: str,
    horizon_seconds: int = 900,
) -> list[dict[str, Any]]:
    """What the server says this machine should run, now and shortly.

    The one place the local scheduler learns that anything is due. It used to
    work this out itself from `next_run_at`, which made two components capable
    of reaching the same conclusion — and `I-TRIGGER-7` exists because that is
    how one job runs twice.

    The horizon is what keeps a laptop useful offline: it asks for a little of
    the near future as well as the present and holds those fires locally, so a
    dropped connection costs new *arming*, not the runs already handed over.

    **Raises on failure, and the caller must not treat that as "nothing is
    due".** An empty list is a real answer that means run nothing; an outage
    that could return one would be indistinguishable from a quiet afternoon.
    """
    answer = _request(
        base_url,
        token,
        device_id,
        "GET",
        f"{DUE_PATH}?horizonMs={int(max(horizon_seconds, 0)) * 1000}",
    )
    due = answer.get("due")
    return due if isinstance(due, list) else []


def fetch_job(
    base_url: str, token: str, device_id: str, job_id: str
) -> dict[str, Any] | None:
    """One job's definition, for a runner about to execute it."""
    answer = _request(
        base_url, token, device_id, "GET", f"{JOBS_PATH}?jobId={job_id}"
    )
    job = answer.get("job")
    return job if isinstance(job, dict) else None


def remove_job(base_url: str, token: str, device_id: str, job_id: str) -> None:
    _request(
        base_url, token, device_id, "DELETE", f"{JOBS_PATH}?jobId={job_id}"
    )


def report_run(
    base_url: str,
    token: str,
    device_id: str,
    job_id: str,
    fire_at: str,
    run: Any,
) -> None:
    """Say what happened, so somebody who was asleep can find out."""
    _request(
        base_url,
        token,
        device_id,
        "POST",
        RUNS_PATH,
        {
            "jobId": job_id,
            "fireAt": fire_at,
            "status": run.status or ("ok" if run.ok else "failed"),
            "summary": (run.summary or "")[:4000],
            "error": (run.error or "")[:2000],
        },
    )


def recent_runs(
    base_url: str, token: str, device_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    answer = _request(
        base_url, token, device_id, "GET", f"{RUNS_PATH}?limit={int(limit)}"
    )
    runs = answer.get("runs")
    return runs if isinstance(runs, list) else []


# ---------------------------------------------------------------------------
# The fire claim, when the runner cannot hold a lock
# ---------------------------------------------------------------------------

CLAIM_PATH = "/api/cloud/fires"


class FireClaims:
    """`RemoteFires`'s view of the server. Three calls, and one rule.

    **Every failure raises.** `RemoteFires` must never be handed a guess: an
    unreachable server is not permission to run, because assuming a win when the
    claim could not be checked is how two containers run the same job. The cost
    of failing closed is a delayed run; the cost of failing open is a duplicated
    side effect, and those are not the same size.
    """

    def __init__(self, base_url: str, token: str, device_id: str) -> None:
        self.base_url = base_url
        self.token = token
        self.device_id = device_id

    def claim_fire(self, user_id: str, job_id: str, fire_at: str, holder: str) -> str:
        answer = _request(
            self.base_url,
            self.token,
            self.device_id,
            "POST",
            CLAIM_PATH,
            {"action": "claim", "jobId": job_id, "fireAt": fire_at, "holder": holder},
        )
        return str(answer.get("outcome") or "unknown")

    def settle_fire(self, user_id: str, job_id: str, fire_at: str, ok: bool) -> bool:
        answer = _request(
            self.base_url,
            self.token,
            self.device_id,
            "POST",
            CLAIM_PATH,
            {"action": "settle", "jobId": job_id, "fireAt": fire_at, "ok": ok},
        )
        return bool(answer.get("ok"))

    def unresolved_fires(self, user_id: str) -> list[dict[str, Any]]:
        answer = _request(
            self.base_url, self.token, self.device_id, "GET", CLAIM_PATH
        )
        rows = answer.get("fires")
        return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Credentials that can follow a job into a container
# ---------------------------------------------------------------------------

SECRETS_PATH = "/api/cloud/secrets"


def put_secret(base_url: str, token: str, device_id: str, name: str, value: str) -> None:
    """Store one, sealed server-side. The value never touches a file here."""
    _request(
        base_url, token, device_id, "POST", SECRETS_PATH, {"name": name, "value": value}
    )


def list_secrets(base_url: str, token: str, device_id: str) -> list[dict[str, Any]]:
    """Names and times. Never values — there is no route that returns one here."""
    answer = _request(base_url, token, device_id, "GET", SECRETS_PATH)
    rows = answer.get("secrets")
    return rows if isinstance(rows, list) else []


def forget_secret(base_url: str, token: str, device_id: str, name: str) -> bool:
    answer = _request(
        base_url, token, device_id, "DELETE", f"{SECRETS_PATH}?name={name}"
    )
    return bool(answer.get("ok"))


def resolve_secrets(
    base_url: str, token: str, device_id: str, names: list[str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Open this account's secrets, for a runner about to execute a job.

    Returns the values and the names that could not be opened. The second half
    matters: a secret that will not decrypt is a configuration failure somebody
    has to fix, and dropping it silently turns that into "the job failed on a
    missing variable", which points at the wrong thing entirely.

    Nothing here is cached to disk. `secrets.py`'s standing rule, and it exists
    because somebody who moved a key into a vault did it to stop the value
    living in a file — writing it back under a different name gives them the
    file back and takes away the revocation.
    """
    query = f"{SECRETS_PATH}?resolve=1"
    if names:
        query += "&names=" + ",".join(names)
    answer = _request(base_url, token, device_id, "GET", query)
    values = answer.get("secrets")
    failed = answer.get("failed")
    return (
        values if isinstance(values, dict) else {},
        failed if isinstance(failed, list) else [],
    )


MACHINES_PATH = "/api/cloud/machines"


def provision_machine(
    base_url: str, token: str, device_id: str, callback_url: str, provider: str
) -> dict[str, Any]:
    """Register a runner and receive its two credentials, once.

    The response is the only time either is readable. Everything stored is a
    hash or a sealed envelope, which is what makes a database dump insufficient
    to impersonate a runner.
    """
    return _request(
        base_url,
        token,
        device_id,
        "POST",
        MACHINES_PATH,
        {"callbackUrl": callback_url, "provider": provider},
    )


def machine_status(base_url: str, token: str, device_id: str) -> dict[str, Any] | None:
    answer = _request(base_url, token, device_id, "GET", MACHINES_PATH)
    machine = answer.get("machine")
    return machine if isinstance(machine, dict) else None


def teardown_machine(base_url: str, token: str, device_id: str) -> dict[str, Any]:
    """Disarm every cloud job, then revoke the runner's credential."""
    return _request(base_url, token, device_id, "DELETE", MACHINES_PATH)
