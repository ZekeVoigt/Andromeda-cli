"""Talking to the server about cloud jobs.

Three statements — arm this, forget this, here is what happened — and the tests
are mostly about the third, because it is the one whose absence is invisible.

The failure this whole layer exists to prevent has already been made once in
this system: a job ran, wrote its output somewhere, and nothing told anybody. On
a laptop that somewhere was a directory you had to remember existed. On a hosted
runner it is a container volume the person has never seen and cannot reach — the
same bug with a longer commute.

The other half is that **every call fails soft**. A job that ran correctly must
not be reported as failed because the network was down while we tried to say so,
and a job created on a train must not be refused because the arming call timed
out. These tests pin that a failure is a returned reason and never an exception
that escapes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from andromeda_agent import cloud_client


class _Run:
    def __init__(self, status="ok", summary="did the thing", error=""):
        self.status = status
        self.summary = summary
        self.error = error
        self.ok = status == "ok"


class _Job:
    """A job, as much of one as arming needs.

    `to_json` is here because arming sends the whole definition — a runner that
    has never seen this machine has to be able to execute it — and a double
    without it stops being a double for the thing under test. Kept as a real
    dict rather than a `Mock` so a test can assert on what actually crossed the
    wire.
    """

    def __init__(self, job_id="job_1"):
        self.id = job_id
        self.name = "a job"
        self.schedule = "every 1h"
        self.next_run_at = 1_800_000_000.5
        self.prompt = "watch the tide"
        self.workspace = "/tmp/workspace"

    def to_json(self):
        return {
            "id": self.id,
            "name": self.name,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "workspace": self.workspace,
            "nextRunAt": self.next_run_at,
            # Present so `test_arming_sends_the_timing_and_not_the_prompt` can
            # prove it is stripped rather than merely absent.
            "runs": [{"startedAt": 1, "status": "ok"}],
        }


def _server(status: int = 200, body: dict | None = None, capture: list | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if capture is not None:
                capture.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "device": self.headers.get("X-Device-Id"),
                        "body": json.loads(raw) if raw else None,
                    }
                )
            payload = json.dumps(body if body is not None else {"ok": True}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = _answer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_arming_sends_the_definition_and_strips_the_run_history():
    """What crosses the wire, and what deliberately does not.

    **This reverses an earlier contract, on purpose.** Arming used to send
    timing only — `jobId`, `name`, `schedule`, `nextRunAt` — on the reasoning
    that a trigger decides *when* and has no business holding what a job does.
    That does not survive a runner: a container that has never seen this
    machine cannot execute a job it has only been told the name of. So the
    definition goes too, under `spec`.

    What is still stripped is the run history. That is this machine's own
    account of what happened, it grows without bound, and the server keeps
    outcomes in `cloudRuns` already — so sending it would be paying to
    duplicate a record that is already there.
    """
    seen: list = []
    httpd, base = _server(capture=seen)
    try:
        cloud_client.push_job(base, "t" * 40, "device-1", _Job())
    finally:
        httpd.shutdown()

    assert len(seen) == 1
    body = seen[0]["body"]
    assert set(body) == {"jobId", "name", "schedule", "nextRunAt", "spec"}

    spec = body["spec"]
    assert spec["prompt"] == "watch the tide"
    assert spec["workspace"] == "/tmp/workspace"
    assert "runs" not in spec, "the run history must not cross the wire"


def test_the_fire_time_is_sent_in_milliseconds():
    """The server's scheduler works in them.

    A unit mismatch here does not error anywhere — it arms a fire fifty years
    out, and the job simply never runs again.
    """
    seen: list = []
    httpd, base = _server(capture=seen)
    try:
        cloud_client.push_job(base, "t" * 40, "device-1", _Job())
    finally:
        httpd.shutdown()
    assert seen[0]["body"]["nextRunAt"] == 1_800_000_000_500


def test_every_call_carries_the_device_identity():
    seen: list = []
    httpd, base = _server(capture=seen)
    try:
        cloud_client.push_job(base, "secret-token-value-padding-x" * 2, "device-1", _Job())
    finally:
        httpd.shutdown()
    assert seen[0]["auth"].startswith("Bearer ")
    assert seen[0]["device"] == "device-1"


def test_reporting_a_run_bounds_what_it_sends():
    """Model output from an unattended run, going into a database row.

    Unbounded, it is a row nobody can read and a bill nobody expected.
    """
    seen: list = []
    httpd, base = _server(capture=seen)
    try:
        cloud_client.report_run(
            base, "t" * 40, "device-1", "job_1", "2026-08-25T02:00:00+00:00",
            _Run(summary="x" * 10_000),
        )
    finally:
        httpd.shutdown()
    assert len(seen[0]["body"]["summary"]) == 4000


# ---------------------------------------------------------------------------
# Failing soft
# ---------------------------------------------------------------------------


def test_an_unreachable_server_is_a_reason_not_a_crash():
    """A job that ran correctly must not be reported as failed because the
    network was down while we tried to say so."""
    with pytest.raises(cloud_client.CloudUnavailable) as caught:
        # Port 1 is reserved and nothing listens there.
        cloud_client.push_job("http://127.0.0.1:1", "t" * 40, "device-1", _Job())
    assert "could not reach" in str(caught.value)


def test_the_servers_own_words_survive_a_refusal():
    """A plan-limit message is worth reading. "HTTP 400" is not."""
    httpd, base = _server(
        status=400, body={"ok": False, "error": "Your plan allows 2 cloud job(s)"}
    )
    try:
        with pytest.raises(cloud_client.CloudUnavailable) as caught:
            cloud_client.push_job(base, "t" * 40, "device-1", _Job())
    finally:
        httpd.shutdown()
    assert "Your plan allows 2 cloud job(s)" in str(caught.value)


def test_an_unpaired_machine_says_so_rather_than_calling_nothing():
    with pytest.raises(cloud_client.CloudUnavailable) as caught:
        cloud_client.push_job("", "", "", _Job())
    assert "auth login" in str(caught.value)


def test_the_device_token_is_registered_for_redaction_before_it_can_leak():
    """A credential that escapes through an exception message is still leaked.

    Registration happens before the request is built, so a failure on the very
    first call is already covered.
    """
    from andromeda_agent import redact

    token = "tok-" + "9" * 44
    with pytest.raises(cloud_client.CloudUnavailable):
        cloud_client.push_job("http://127.0.0.1:1", token, "device-1", _Job())
    assert token not in redact.scrub(f"the token is {token}", code_file=False).text


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def test_recent_runs_returns_a_list_even_when_the_shape_is_wrong():
    """An older or half-deployed server must degrade to "nothing to show"
    rather than to a traceback in a greeting."""
    httpd, base = _server(body={"ok": True, "runs": "not a list"})
    try:
        assert cloud_client.recent_runs(base, "t" * 40, "device-1") == []
    finally:
        httpd.shutdown()


def test_recent_runs_passes_the_limit_through():
    seen: list = []
    httpd, base = _server(body={"ok": True, "runs": []}, capture=seen)
    try:
        cloud_client.recent_runs(base, "t" * 40, "device-1", limit=5)
    finally:
        httpd.shutdown()
    assert "limit=5" in seen[0]["path"]
