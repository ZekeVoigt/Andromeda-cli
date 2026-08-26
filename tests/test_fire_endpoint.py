"""The runner's only door, and the claim behind it.

Everything here is about a request that arrives more than once. That is not an
edge case — the caller cannot tell "the machine never got it" from "the machine
got it and died", so anything short of a confirmed acceptance must be retried or
a job silently stops firing the first time a packet is lost. Retries are the
feature; these tests are what make them safe.

Grouped the way the failures would arrive:

1. **Nothing runs without a valid token**, and every refusal is asserted by a
   side effect that would have happened, not by the status code alone. A 401
   that still ran the job is a 401 that proved nothing.
2. **A fire runs once**, however many times it is delivered.
3. **A fire nobody reported is not retried**, because its side effects may have
   happened and nobody can know.
4. **A long job does not sit in the connection**, which is the whole reason for
   answering before working.
5. **The server refuses to start rather than start unsafely.**
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from andromeda_agent import serve as serve_module
from andromeda_agent.fires import Fires, Outcome

SECRET = "x" * 48


class _Job:
    def __init__(self, job_id: str = "job_1", runs_on: str = "cloud") -> None:
        self.id = job_id
        self.enabled = True
        self.paused_reason = ""
        self.retired = False
        self.runs_on = runs_on


@pytest.fixture
def fires(tmp_path) -> Fires:
    return Fires(tmp_path / "fires.db")


@pytest.fixture
def runner(fires):
    """A runner whose "work" is appending to a list.

    The list is the side effect every refusal test asserts against: a status
    code says what the server *replied*, and only the list says whether the job
    actually ran.
    """
    ran: list[tuple[str, str]] = []
    done = threading.Event()

    def execute(job, fire_at: str) -> None:
        ran.append((job.id, fire_at))
        done.set()

    built = serve_module.Runner(
        fires=fires,
        resolve=lambda job_id: _Job(job_id) if job_id.startswith("job_") else None,
        execute=execute,
        max_concurrent=2,
    )
    built.ran = ran  # type: ignore[attr-defined]
    built.done = done  # type: ignore[attr-defined]
    return built


@pytest.fixture
def server(runner):
    built = serve_module.build_server(runner, "127.0.0.1", 0, SECRET)
    thread = threading.Thread(target=built.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{built.server_address[1]}"
    built.shutdown()


def _post(base: str, body: dict, token: str = "", path: str = serve_module.ROUTE):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _signed(job_id: str = "job_1", fire_at: str = "2026-08-25T02:00:00+00:00", **over):
    body = serve_module.mint(SECRET, job_id, fire_at)
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# 1. Nothing runs without a valid token
# ---------------------------------------------------------------------------


def test_a_valid_fire_is_accepted_and_runs(server, runner):
    body = _signed()
    status, payload = _post(server, body, body["token"])
    assert status == 202
    assert payload["status"] == "accepted"
    assert runner.done.wait(5)
    assert runner.ran == [("job_1", body["fire_at"])]


@pytest.mark.parametrize(
    "mangle,label",
    [
        (lambda b: {**b, "token": "deadbeef"}, "forged"),
        (lambda b: {**b, "token": ""}, "absent"),
        (lambda b: {**b, "job_id": "job_other"}, "signed for another job"),
        (lambda b: {**b, "fire_at": "2026-01-01T00:00:00+00:00"}, "signed for another time"),
    ],
)
def test_a_bad_token_is_refused_and_nothing_runs(server, runner, mangle, label):
    body = mangle(_signed())
    status, _ = _post(server, body, body["token"])
    assert status == 401, label
    # The point of the test. A 401 that still ran the job proved nothing.
    time.sleep(0.2)
    assert runner.ran == [], label


def test_a_non_ascii_token_is_refused_rather_than_raising():
    """Not reachable over HTTP — headers are latin-1, so a client cannot even
    send one — but `compare_digest` *raises* on a non-ASCII argument instead of
    returning False, and a verifier that reads the token from a JSON body would
    hand it one. A guard that turns a crash into a refusal, tested where it can
    actually be exercised.
    """
    body = serve_module.mint(SECRET, "job_1", "t1")
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.verify_hmac(SECRET, body, "\u2603" * 8)
    assert caught.value.status == 401


def test_an_expired_token_is_refused(server, runner):
    body = serve_module.mint(SECRET, "job_1", "2026-08-25T02:00:00+00:00", ttl_seconds=-120)
    status, _ = _post(server, body, body["token"])
    assert status == 401
    time.sleep(0.2)
    assert runner.ran == []


def test_a_token_inside_the_skew_window_still_works(server, runner):
    """30s of leeway, because two machines that disagree by a second are normal
    and widening the window to hide a real clock problem would only widen the
    replay window."""
    body = serve_module.mint(SECRET, "job_1", "2026-08-25T02:00:00+00:00", ttl_seconds=-5)
    status, _ = _post(server, body, body["token"])
    assert status == 202
    assert runner.done.wait(5)


def test_the_only_route_is_the_fire_route(server, runner):
    body = _signed()
    status, _ = _post(server, body, body["token"], path="/health")
    assert status == 404
    time.sleep(0.2)
    assert runner.ran == []


def test_get_is_not_a_health_check(server):
    """A GET answering 200 would be an unauthenticated way to keep a machine
    that is supposed to be stopped awake."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(server + serve_module.ROUTE, timeout=5)
    assert caught.value.code == 404


def test_an_unknown_job_is_a_404_not_a_401(server):
    body = _signed(job_id="nope_1")
    status, _ = _post(server, body, body["token"])
    assert status == 404


def test_a_job_that_belongs_to_the_users_own_machine_is_refused(server, runner, fires):
    """A hosted runner must not fire a `runs_on: device` job.

    It would run against a workspace this container does not have and report
    that it found nothing — which is indistinguishable from the watched thing
    not changing, and is the exact confusion the location axis exists to
    prevent. Found by driving a real fire at a real container, where the first
    job to hand happened to be a local one.
    """
    runner.resolve = lambda job_id: _Job(job_id, runs_on="device")
    body = _signed()
    status, payload = _post(server, body, body["token"])
    assert status == 409
    assert "own machine" in payload["error"]
    time.sleep(0.2)
    assert runner.ran == []
    # And its fire was never claimed, so moving it and re-delivering works.
    assert fires.claim("job_1", body["fire_at"]) is Outcome.WON


def test_a_paused_job_is_refused_without_consuming_its_fire(server, runner, fires):
    runner.resolve = lambda job_id: _paused(job_id)
    body = _signed()
    status, payload = _post(server, body, body["token"])
    assert status == 409
    assert "paused" in payload["error"]
    # The fire was never claimed, so resuming the job and re-delivering works.
    assert fires.claim("job_1", body["fire_at"]) is Outcome.WON


def _paused(job_id: str) -> _Job:
    job = _Job(job_id)
    job.paused_reason = "paused after 5 failures in a row"
    return job


def test_a_request_with_no_credential_is_unauthorized_not_malformed(server, runner):
    """A bare POST answers 401, not "job_id is required".

    The body's shape is not a secret — it is in this repository. The point is
    narrower and still worth it: a caller who presented no credential should be
    told that, rather than critiqued on its formatting. It is also the request
    the runbook uses to measure a machine's wake, and that was documented as
    returning 401 while an earlier ordering returned 400.

    Only the no-token case is covered by this. Once a token *string* is present
    the body has to be parsed to verify anything at all, because the signature
    covers it — so shape errors are reachable by anyone willing to send eight
    random characters, and that is fine.
    """
    status, payload = _post(server, {})
    assert status == 401
    assert "job_id" not in json.dumps(payload)
    time.sleep(0.2)
    assert runner.ran == []


def test_a_body_that_is_not_json_is_refused(server, runner):
    request = urllib.request.Request(
        server + serve_module.ROUTE,
        data=b"not json at all",
        headers={"Authorization": f"Bearer {'a' * 64}"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 400
    assert runner.ran == []


# ---------------------------------------------------------------------------
# 2. A fire runs once
# ---------------------------------------------------------------------------


def test_a_redelivered_fire_runs_once(server, runner):
    body = _signed()
    first = _post(server, body, body["token"])
    assert runner.done.wait(5)
    second = _post(server, body, body["token"])

    assert first == (202, {"status": "accepted", "job_id": "job_1"})
    # 202 again, not a refusal: the caller's contract is "202 means stop
    # retrying", and a duplicate is exactly when it should.
    assert second[0] == 202
    assert second[1]["status"] == "duplicate"
    time.sleep(0.2)
    assert len(runner.ran) == 1


def test_two_requests_in_the_same_instant_produce_one_run(fires):
    """The check and the write are one transaction, or this is a coin flip.

    Doing them as two statements is the classic version of this bug and it only
    appears under exactly the load that matters.
    """
    outcomes: list[Outcome] = []
    barrier = threading.Barrier(8)

    def race() -> None:
        barrier.wait()
        outcomes.append(fires.claim("job_1", "2026-08-25T02:00:00+00:00"))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(Outcome.WON) == 1
    assert outcomes.count(Outcome.IN_FLIGHT) == 7


def test_a_different_fire_time_is_a_different_fire(fires):
    assert fires.claim("job_1", "2026-08-25T02:00:00+00:00") is Outcome.WON
    assert fires.claim("job_1", "2026-08-25T03:00:00+00:00") is Outcome.WON


def test_a_settled_fire_is_not_reclaimable(fires):
    fires.claim("job_1", "t1")
    assert fires.settle("job_1", "t1", ok=True)
    assert fires.claim("job_1", "t1") is Outcome.SETTLED


def test_settling_twice_is_refused(fires):
    """Terminal states are immutable — a late process must not overwrite what
    actually happened. The execution ledger's rule, on this table."""
    fires.claim("job_1", "t1")
    assert fires.settle("job_1", "t1", ok=True)
    assert not fires.settle("job_1", "t1", ok=False)
    assert fires.recent("job_1")[0]["ok"] == 1


# ---------------------------------------------------------------------------
# 3. A fire nobody reported is not retried
# ---------------------------------------------------------------------------


def test_an_expired_lease_is_unknown_and_is_not_reclaimed(fires):
    """The side effects may have run. Re-running is how a machine sends the
    same email twice at 3am, forever."""
    fires.claim("job_1", "t1", lease_seconds=-1)
    assert fires.claim("job_1", "t1") is Outcome.UNKNOWN
    assert fires.claim("job_1", "t1") is Outcome.UNKNOWN


def test_an_unknown_fire_is_refused_by_the_endpoint_with_a_reason(server, runner, fires):
    fires.claim("job_1", "2026-08-25T02:00:00+00:00", lease_seconds=-1)
    body = _signed()
    status, payload = _post(server, body, body["token"])
    assert status == 409
    assert "never reported" in payload["error"]
    time.sleep(0.2)
    assert runner.ran == []


def test_unknown_fires_are_listed_and_never_pruned(fires):
    fires.claim("job_old", "t1", lease_seconds=-1)
    for index in range(50):
        fires.claim("job_noise", f"t{index}")
        fires.settle("job_noise", f"t{index}", ok=True)
    unresolved = fires.unresolved()
    assert [row["job_id"] for row in unresolved] == ["job_old"]


def test_a_job_that_raises_still_settles(fires):
    """An unsettled fire is indistinguishable from a machine that died, and
    would be reported as unknown forever."""
    def explode(job, fire_at):
        raise RuntimeError("the job failed")

    runner = serve_module.Runner(
        fires=fires, resolve=lambda job_id: _Job(job_id), execute=explode
    )
    runner.accept("job_1", "t1")
    for _ in range(100):
        if fires.recent("job_1") and fires.recent("job_1")[0]["settled_at"]:
            break
        time.sleep(0.02)
    row = fires.recent("job_1")[0]
    assert row["settled_at"]
    assert row["ok"] == 0


# ---------------------------------------------------------------------------
# 4. The work does not sit in the connection
# ---------------------------------------------------------------------------


def test_the_response_arrives_before_the_work_finishes(fires):
    """An agent turn is minutes and an HTTP timeout is seconds. Answering after
    the run means every long job looks like a delivery failure and gets
    retried."""
    started = threading.Event()
    release = threading.Event()

    def slow(job, fire_at):
        started.set()
        release.wait(5)

    runner = serve_module.Runner(
        fires=fires, resolve=lambda job_id: _Job(job_id), execute=slow
    )
    server = serve_module.build_server(runner, "127.0.0.1", 0, SECRET)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        body = _signed()
        began = time.monotonic()
        status, _ = _post(base, body, body["token"])
        elapsed = time.monotonic() - began
        assert status == 202
        assert started.wait(5)
        # The job is still running while we already have the answer.
        assert elapsed < 1.0
        assert runner.in_flight == 1
    finally:
        release.set()
        server.shutdown()


def test_over_capacity_is_503_and_retryable(fires):
    """Refused, not accepted-and-starved. A `503` brings the caller back; an
    accepted fire that waits behind three others has consumed its one claim."""
    release = threading.Event()

    def slow(job, fire_at):
        release.wait(5)

    runner = serve_module.Runner(
        fires=fires,
        resolve=lambda job_id: _Job(job_id),
        execute=slow,
        max_concurrent=1,
    )
    try:
        assert runner.accept("job_1", "t1")[0] == 202
        with pytest.raises(serve_module.FireError) as caught:
            runner.accept("job_2", "t2")
        assert caught.value.status == 503
        # And the refused fire was never claimed, so the retry can win it.
        assert fires.claim("job_2", "t2") is Outcome.WON
    finally:
        release.set()


# ---------------------------------------------------------------------------
# 5. It refuses to start rather than start unsafely
# ---------------------------------------------------------------------------


def test_no_secret_means_no_server(monkeypatch):
    monkeypatch.delenv(serve_module.SECRET_ENV, raising=False)
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.secret_from_environment()
    assert "worse than not starting" in str(caught.value)


def test_a_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv(serve_module.SECRET_ENV, "hunter2")
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.secret_from_environment()
    assert "32 characters" in str(caught.value)


def test_the_verifier_is_a_seam(monkeypatch):
    """Swappable for an asymmetric one with no change to the handler, which is
    what makes deferring that infrastructure a decision and not a corner cut."""
    assert "hmac" in serve_module.VERIFIERS
    assert serve_module.VERIFIERS["hmac"] is serve_module.verify_hmac


# ---------------------------------------------------------------------------
# 6. Stopping is a drain, not a kill
#
# A container runtime sends SIGTERM and then, seconds later, SIGKILL. Dying on
# the spot means a running job's thread vanishes: the work may have
# half-happened, nothing settles the fire, and it surfaces later as `unknown` —
# the one outcome that needs a person to look at it.
# ---------------------------------------------------------------------------


def test_a_draining_runner_refuses_new_fires_but_stays_up(fires):
    release = threading.Event()

    def slow(job, fire_at):
        release.wait(5)

    runner = serve_module.Runner(
        fires=fires, resolve=lambda job_id: _Job(job_id), execute=slow
    )
    try:
        assert runner.accept("job_1", "t1")[0] == 202
        runner.draining = True
        with pytest.raises(serve_module.FireError) as caught:
            runner.accept("job_2", "t2")
        # 503, not a refusal: the caller should come back — to another machine,
        # or to this one after it restarts.
        assert caught.value.status == 503
        assert fires.claim("job_2", "t2") is Outcome.WON
    finally:
        release.set()


def test_a_drain_waits_for_a_running_job_and_it_settles(fires):
    release = threading.Event()
    finished = threading.Event()

    def slow(job, fire_at):
        release.wait(5)
        finished.set()

    runner = serve_module.Runner(
        fires=fires, resolve=lambda job_id: _Job(job_id), execute=slow
    )
    runner.accept("job_1", "t1")

    drained: list[int] = []
    waiter = threading.Thread(target=lambda: drained.append(runner.drain(timeout=5)))
    waiter.start()
    time.sleep(0.2)
    assert not drained, "drain returned before the job finished"

    release.set()
    waiter.join(5)
    assert drained == [0]
    assert finished.is_set()
    # The whole point: an ordinary recorded run rather than an unknown.
    assert fires.recent("job_1")[0]["settled_at"]


def test_a_drain_that_times_out_reports_what_it_stranded(fires):
    """Not silently. A person finding these in a ledger a week later has nothing
    to connect them to the deploy that caused them."""
    release = threading.Event()

    def slow(job, fire_at):
        release.wait(10)

    runner = serve_module.Runner(
        fires=fires, resolve=lambda job_id: _Job(job_id), execute=slow
    )
    try:
        runner.accept("job_1", "t1")
        assert runner.drain(timeout=0.3) == 1
    finally:
        release.set()


# ---------------------------------------------------------------------------
# 7. The stop-everything switch
# ---------------------------------------------------------------------------


def test_the_kill_switch_refuses_every_fire(server, runner, monkeypatch):
    monkeypatch.setenv(serve_module.CLOUD_OFF_ENV, "1")
    body = _signed()
    status, payload = _post(server, body, body["token"])
    assert status == 503
    assert "switched off" in payload["error"]
    time.sleep(0.2)
    assert runner.ran == []


def test_the_kill_switch_is_read_per_fire_not_at_boot(server, runner, monkeypatch):
    """Read once at startup it would be a switch you have to restart a machine
    to use, which is the wrong shape for the thing you reach for at 3am."""
    monkeypatch.setenv(serve_module.CLOUD_OFF_ENV, "1")
    body = _signed()
    assert _post(server, body, body["token"])[0] == 503

    monkeypatch.delenv(serve_module.CLOUD_OFF_ENV)
    assert _post(server, body, body["token"])[0] == 202
    assert runner.done.wait(5)


def test_the_kill_switch_applies_before_the_job_is_even_resolved(fires, monkeypatch):
    """A kill switch that only applies to jobs it can resolve is a kill switch
    with exceptions."""
    monkeypatch.setenv(serve_module.CLOUD_OFF_ENV, "yes")

    def explode(job_id):
        raise AssertionError("the switch must be checked before resolving")

    runner = serve_module.Runner(
        fires=fires, resolve=explode, execute=lambda job, fire_at: None
    )
    with pytest.raises(serve_module.FireError) as caught:
        runner.accept("job_1", "t1")
    assert caught.value.status == 503


@pytest.mark.parametrize("value,off", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_only_an_explicit_falsey_value_means_on(monkeypatch, value, off):
    """The cost of misreading this is asymmetric: refusing a fire that should
    have run makes a job late, and running one that should have been stopped is
    the thing somebody threw the switch to prevent."""
    monkeypatch.setenv(serve_module.CLOUD_OFF_ENV, value)
    assert serve_module.cloud_is_off() is off


def test_the_switch_is_absent_by_default(monkeypatch):
    monkeypatch.delenv(serve_module.CLOUD_OFF_ENV, raising=False)
    assert serve_module.cloud_is_off() is False


# ---------------------------------------------------------------------------
# 6. The tenant is signed, because the runner is shared
#
# On a machine-per-user host the tenant was implied by which machine a fire
# reached, so it never travelled. A shared pool serves every tenant from one
# endpoint, and an unsigned `user_id` there means anyone holding one valid fire
# can replay it with the field swapped — claiming and settling against somebody
# else's job.
# ---------------------------------------------------------------------------


def test_swapping_the_tenant_invalidates_the_signature():
    """The attack the shared-pool migration created, and the fix for it."""
    body = serve_module.mint(SECRET, "job_1", "t1", user_id="alice")
    stolen = {**body, "user_id": "bob"}

    with pytest.raises(serve_module.FireError) as caught:
        serve_module.verify_hmac(SECRET, stolen, body["token"])
    assert caught.value.status == 401


def test_a_fire_signed_for_one_tenant_verifies_for_that_tenant():
    """The other direction, so the test above cannot pass by everything failing."""
    body = serve_module.mint(SECRET, "job_1", "t1", user_id="alice")
    serve_module.verify_hmac(SECRET, body, body["token"])  # does not raise


def test_the_tenant_is_part_of_the_signed_material():
    """Pinned on the constant, not only on behaviour.

    The signing and verifying sides live in two languages — `serve.py` and
    `convex/cloudFire.ts` — and a field that quietly leaves this tuple fails
    closed *silently*, as a runner that 401s every fire while looking healthy.
    """
    assert serve_module._SIGNED_FIELDS == ("user_id", "job_id", "fire_at", "exp")


def test_a_missing_tenant_is_refused_rather_than_defaulted():
    body = serve_module.mint(SECRET, "job_1", "t1", user_id="alice")
    del body["user_id"]
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.verify_hmac(SECRET, body, body["token"])
    assert caught.value.status == 400


# ---------------------------------------------------------------------------
# 7. The claim, when the runner cannot hold a lock
#
# Modal's Volumes are last-write-wins and documented as unsafe for SQLite, so
# the claim moved to the server. The four outcomes are the contract; only the
# storage changed.
# ---------------------------------------------------------------------------


class _FakeClaims:
    """A server that answers claims, and can be told to stop answering."""

    def __init__(self, outcome="won", explode=False):
        self.outcome = outcome
        self.explode = explode
        self.calls: list = []

    def claim_fire(self, user_id, job_id, fire_at, holder):
        if self.explode:
            raise RuntimeError("the server is unreachable")
        self.calls.append(("claim", user_id, job_id, fire_at, holder))
        return self.outcome

    def settle_fire(self, user_id, job_id, fire_at, ok):
        self.calls.append(("settle", user_id, job_id, fire_at, ok))
        return True

    def unresolved_fires(self, user_id):
        return []


def test_the_remote_claim_speaks_the_same_four_outcomes():
    from andromeda_agent.fires import Outcome, RemoteFires

    for name, expected in [
        ("won", Outcome.WON),
        ("in_flight", Outcome.IN_FLIGHT),
        ("settled", Outcome.SETTLED),
        ("unknown", Outcome.UNKNOWN),
    ]:
        remote = RemoteFires(_FakeClaims(outcome=name), "alice")
        assert remote.claim("job_1", "t1") is expected


def test_an_outcome_this_build_does_not_know_is_not_a_win():
    """A newer server adding a fifth case must not let an older runner run.

    Unknown reads as UNKNOWN, the refusing one — the same direction every
    corrupt-field rule in this codebase takes.
    """
    from andromeda_agent.fires import Outcome, RemoteFires

    remote = RemoteFires(_FakeClaims(outcome="deferred"), "alice")
    assert remote.claim("job_1", "t1") is Outcome.UNKNOWN


def test_an_unreachable_claim_server_is_a_503_and_never_a_run(fires):
    """The failure that would let two containers run one job.

    Assuming a win when the claim could not be checked is the whole thing this
    guards. Failing closed costs a delayed run; failing open costs a duplicated
    side effect, and those are not the same size.
    """
    from andromeda_agent.fires import RemoteFires

    ran: list = []
    runner = serve_module.Runner(
        fires=RemoteFires(_FakeClaims(explode=True), "alice"),
        resolve=lambda job_id: _Job(job_id),
        execute=lambda job, fire_at: ran.append(job.id),
    )
    with pytest.raises(serve_module.FireError) as caught:
        runner.accept("job_1", "t1")
    assert caught.value.status == 503
    assert ran == []


def test_the_remote_claim_carries_the_tenant():
    """Every call is scoped. A claim without a tenant is a claim on everybody."""
    from andromeda_agent.fires import RemoteFires

    claims = _FakeClaims()
    remote = RemoteFires(claims, "alice", holder="container-7")
    remote.claim("job_1", "t1")
    remote.settle("job_1", "t1", ok=True)

    assert claims.calls[0] == ("claim", "alice", "job_1", "t1", "container-7")
    assert claims.calls[1] == ("settle", "alice", "job_1", "t1", True)


def test_both_backends_satisfy_the_same_interface():
    """The seam that makes the host a deployment decision rather than a rewrite.

    `serve.Runner` takes either and must not be able to tell them apart.
    """
    from andromeda_agent.fires import Fires, RemoteFires

    for name in ("claim", "settle", "unresolved", "recent"):
        assert callable(getattr(Fires, name))
        assert callable(getattr(RemoteFires, name))


# ---------------------------------------------------------------------------
# 8. The asymmetric half
#
# With HMAC the runner holds the same secret the server signs with, so a
# compromised runner can forge fires for itself. That is tolerable while the
# operator is the only host — forging a fire to yourself gains nothing over
# running the job — and stops being tolerable the moment somebody else runs one.
# ---------------------------------------------------------------------------


def _ed25519_pair():
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public.hex()


def _ed25519_fire(private, user_id="alice", job_id="job_1", fire_at="t1", ttl=120):
    exp = int(time.time()) + ttl
    body = {"user_id": user_id, "job_id": job_id, "fire_at": fire_at, "exp": exp}
    signature = private.sign(
        serve_module._payload(user_id, job_id, fire_at, exp)
    ).hex()
    return body, signature


def test_an_ed25519_fire_verifies_against_the_public_key():
    private, public = _ed25519_pair()
    body, signature = _ed25519_fire(private)
    serve_module.verify_ed25519(public, body, signature)  # does not raise


def test_a_runner_holding_only_the_public_key_cannot_forge_a_fire():
    """The property that makes a third-party runner safe to hand a job to."""
    private, public = _ed25519_pair()
    other, _ = _ed25519_pair()
    body, _ = _ed25519_fire(private)
    _, forged = _ed25519_fire(other)

    with pytest.raises(serve_module.FireError) as caught:
        serve_module.verify_ed25519(public, body, forged)
    assert caught.value.status == 401


def test_the_tenant_is_signed_here_too():
    private, public = _ed25519_pair()
    body, signature = _ed25519_fire(private, user_id="alice")
    with pytest.raises(serve_module.FireError):
        serve_module.verify_ed25519(public, {**body, "user_id": "bob"}, signature)


def test_an_expired_ed25519_fire_is_refused():
    private, public = _ed25519_pair()
    body, signature = _ed25519_fire(private, ttl=-120)
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.verify_ed25519(public, body, signature)
    assert caught.value.status == 401


def test_an_unrecognised_scheme_is_refused_and_never_falls_back(monkeypatch):
    """Unlike `cron_provider`, where an unknown name falls back to the built-in.

    They are not the same kind of setting: falling back there means jobs still
    run, and falling back here would mean a runner configured for asymmetric
    fires quietly accepting symmetric ones — a downgrade attack spelled as a
    typo.
    """
    monkeypatch.setenv(serve_module.SCHEME_ENV, "ed25519-ish")
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.configured_scheme()
    assert "not a scheme this build knows" in str(caught.value)


def test_the_default_scheme_is_hmac(monkeypatch):
    monkeypatch.delenv(serve_module.SCHEME_ENV, raising=False)
    assert serve_module.configured_scheme() == "hmac"


def test_an_ed25519_runner_wants_a_public_key_not_a_long_secret(monkeypatch):
    """The length rule is dropped rather than pretended.

    A public key is not a secret, and a rule that exists for one scheme and is
    enforced on another is a rule people work around.
    """
    _, public = _ed25519_pair()
    monkeypatch.setenv(serve_module.SECRET_ENV, public)
    assert serve_module.secret_from_environment("ed25519") == public

    monkeypatch.setenv(serve_module.SECRET_ENV, "not-hex")
    with pytest.raises(serve_module.FireError) as caught:
        serve_module.secret_from_environment("ed25519")
    assert "32-byte hex" in str(caught.value)


def test_both_schemes_are_reachable_through_the_one_seam():
    assert set(serve_module.VERIFIERS) == {"hmac", "ed25519"}
