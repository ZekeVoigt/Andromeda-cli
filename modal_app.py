"""Andromeda's hosted runner, on Modal.

The same runner, on a host that does not give each user a machine.

## Why this file exists at all

On a machine-per-user host, a fire arrives at a long-lived process that owns a
disk. Two properties came free from that: the job store was a file only one
process could open, and the at-most-once claim was a SQLite transaction.

Neither survives a shared pool. Modal's Volumes are last-write-wins and their
own documentation says not to put SQLite on one, so **the claim moved to the
server** (`RemoteFires`) — where a mutation is already a transaction and the
scheduler already knows when a job is due. That is the only semantic change in
the whole migration, and it made the system more correct rather than less: a
claim that works across N containers also works across one.

## What runs where

    Convex scheduler  ──POST──▶  fire()          this file, on demand
                                    │
                                    ├─ verify the HMAC        (serve.verify_hmac)
                                    ├─ claim in Convex        (RemoteFires)
                                    ├─ 202 immediately        (spawn, then return)
                                    └─ run                    (runner.execute)

**Per-user state is a directory, not a machine.** One Modal Volume, mounted at
`/data`, with `/data/users/<user_id>/` as that user's `ANDROMEDA_HOME`. Their
whole durable state measured at 2.7 MB on a heavy install — job specs, monitor
baselines, notepads, transcripts. `state.db` is deliberately *not* on the
volume: it is a rebuildable index over `sessions/`, `transfer.py` already
excludes it from backups, and it is SQLite, which is the one thing a Volume must
not hold.

## Deploying

    pip install modal && modal setup
    modal deploy cli/modal_app.py

Then point the server at the web endpoint Modal prints, and set two secrets:
`andromeda-fire` (ANDROMEDA_FIRE_SECRET) and `andromeda-runner`
(ANDROMEDA_BASE_URL, ANDROMEDA_DEVICE_TOKEN, ANDROMEDA_DEVICE_ID).
"""

# NOT `from __future__ import annotations`, and it matters here.
#
# It turns every annotation into a string, and FastAPI resolves a handler's
# annotations against the *module* namespace. `Request` is imported inside
# `fire()`, so FastAPI could not find it and fell back to treating the parameter
# as a query field — the endpoint answered 422 "field required: request" to
# every fire. The imports stay local (the image has FastAPI, this machine may
# not, and `modal deploy` imports this file locally), so the postponed
# annotations have to go instead.

import os
from pathlib import Path

import modal

APP_NAME = "andromeda-runner"

# Pinned, for the same reason the Dockerfile pins its base by digest: a rebuild
# that silently changes the interpreter is one that can change behaviour with no
# diff to point at.
IMAGE = (
    modal.Image.debian_slim(python_version="3.13")
    # The full list, and it is short on purpose. Each is here because a tool the
    # agent can call shells out to it. A job that calls a binary this image
    # lacks fails in a way that is invisible exactly while you are looking at
    # it — the `PATH` lesson `cron install` already paid for once.
    .apt_install("git", "ripgrep", "ca-certificates")
    .pip_install(
        "openai==2.24.0",
        "httpx==0.28.1",
        "prompt_toolkit==3.0.52",
        "rich==14.3.3",
        "pyyaml==6.0.3",
        "platformdirs==4.5.0",
        "croniter==6.0.0",
        "textual==8.2.8",
        # The web endpoint's own dependency. Modal used to inject it and no
        # longer does, so it is named here rather than inherited.
        "fastapi[standard]==0.121.1",
    )
    .env({"PYTHONPATH": "/opt/andromeda", "PYTHONUNBUFFERED": "1"})
    # `add_local_*` goes LAST, and Modal enforces it: a build step after one
    # rebuilds the whole image on every source edit, so the source is mounted at
    # container start instead. That is also why the three packages are added
    # separately rather than the whole of `cli/` — `tests/`, `.venv/` and
    # `install/` have no business in a runner, and this is the .dockerignore
    # equivalent for a platform that has no such file.
    .add_local_dir(
        Path(__file__).parent / "andromeda_agent",
        remote_path="/opt/andromeda/andromeda_agent",
    )
    .add_local_dir(
        Path(__file__).parent / "andromeda_cli",
        remote_path="/opt/andromeda/andromeda_cli",
    )
    .add_local_dir(
        Path(__file__).parent / "andromeda_tools",
        remote_path="/opt/andromeda/andromeda_tools",
    )
)

# One Volume for every user, with a directory each. Not one per user: a Volume
# per user would re-create the exact coupling this migration is undoing, and at
# 2.7 MB a head there is nothing to isolate that a directory does not.
VOLUME = modal.Volume.from_name("andromeda-runner-state", create_if_missing=True)
STATE_ROOT = "/data"

app = modal.App(APP_NAME)


def _write_credentials(home: Path) -> None:
    """Materialise the runner's device credential into this user's home.

    0600, and from the environment every time. A stale file left by a previous
    fire would be a credential this container did not verify it still holds.
    """
    import json
    import stat

    payload = {
        "device_token": os.environ.get("ANDROMEDA_DEVICE_TOKEN", ""),
        "device_id": os.environ.get("ANDROMEDA_DEVICE_ID", ""),
        "base_url": os.environ.get("ANDROMEDA_BASE_URL", ""),
    }
    path = home / "credentials.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _home_for(user_id: str) -> Path:
    """This user's ANDROMEDA_HOME on the shared volume.

    The id is sanitised rather than trusted. It arrives from a signed fire, so
    it is not attacker-controlled today — but a path built from an identifier is
    one refactor away from being a traversal, and `../` in a home directory is
    another user's data.
    """
    safe = "".join(c for c in user_id if c.isalnum() or c in "-_")[:64]
    if not safe:
        raise ValueError("a fire must name the user it belongs to")
    home = Path(STATE_ROOT) / "users" / safe
    home.mkdir(parents=True, exist_ok=True)
    return home


@app.function(
    image=IMAGE,
    volumes={STATE_ROOT: VOLUME},
    secrets=[
        modal.Secret.from_name("andromeda-fire"),
        modal.Secret.from_name("andromeda-runner"),
    ],
    # Long enough for a real agent turn and no longer. A job still running at
    # this point is a job whose lease will expire and be reported `unknown`,
    # which is the honest outcome — better than a container held open forever.
    timeout=30 * 60,
    # Modal bills per second with no idle tail, so there is nothing to gain by
    # holding a container open between fires and a bill to pay for it.
    scaledown_window=60,
)
def run_fire(user_id: str, job_id: str, fire_at: str) -> dict:
    """One fire, start to finish. Called by `fire()` after it has answered."""
    import sys

    sys.path.insert(0, "/opt/andromeda")

    home = _home_for(user_id)
    os.environ["ANDROMEDA_HOME"] = str(home)


    from andromeda_agent import cloud_client
    from andromeda_agent.fires import Outcome, RemoteFires
    from andromeda_agent.notepad import Notepad
    from andromeda_agent.schedule import Schedule
    from andromeda_cli import config as config_module
    from andromeda_cli.commands import cron as cron_cmd

    base = os.environ.get("ANDROMEDA_BASE_URL", "")
    token = os.environ.get("ANDROMEDA_DEVICE_TOKEN", "")
    device = os.environ.get("ANDROMEDA_DEVICE_ID", "")
    claims = cloud_client.FireClaims(base, token, device)
    fires = RemoteFires(claims, user_id, holder=os.environ.get("MODAL_TASK_ID", ""))

    # Read before anything else: the volume is shared and another container may
    # have written this user's store since this one started.
    VOLUME.reload()

    # The runner's own credential, written AFTER the reload.
    #
    # `VOLUME.reload()` re-reads the shared volume, so anything written
    # before it is at the mercy of what another container last committed.
    # The credential is derived from this container's own environment and
    # must not be, so it is written once the volume has settled.
    #
    # `cloud_client` reads the device token from the environment, but the
    # *model* path does not: `build_provider` resolves it through
    # `config.load_credentials()`, which reads `credentials.json` from
    # ANDROMEDA_HOME. A container has no such file, so every fire reached the
    # relay unauthenticated and the run failed with nothing useful said about
    # why.
    #
    # Written per fire rather than kept on the volume: the volume is shared
    # across users and a credential belongs to exactly one of them, so it lives
    # in that user's own directory and is rewritten from the environment each
    # time rather than trusted from disk.
    _write_credentials(home)

    # The definition comes from the server, not from this volume.
    #
    # A runner is one of many interchangeable containers and has never met the
    # machine that created the job. The volume holds what *accrues* — monitor
    # baselines, the notepad, transcripts — and the server holds what the job
    # *is*. An earlier version read the definition from the volume and answered
    # "no such job" to every fire for a job created on a laptop.
    schedule = Schedule(home / "cron" / "cron.json")
    if schedule.load_error:
        # Not "no such job". A store this container could not read is not a
        # store without that job in it — the distinction that cost a real
        # debugging session on the previous host.
        return {"ok": False, "error": schedule.load_error}

    job = schedule.resolve(job_id)
    if job is None:
        try:
            remote = cloud_client.fetch_job(base, token, device, job_id)
        except cloud_client.CloudUnavailable as exc:
            # Retryable, and reported as such: a definition we could not fetch
            # is not a job that does not exist.
            return {"ok": False, "error": f"could not fetch the job: {exc}"}
        if not remote or not remote.get("spec"):
            return {"ok": False, "error": "no such job"}

        from andromeda_agent.schedule import Job

        job = Job.from_json(remote["spec"])
        if job is None:
            return {"ok": False, "error": "the stored job definition is unreadable"}

        # Cached on the volume so the notepad, the monitor baseline and the run
        # history have somewhere to live and survive to the next fire — the
        # things that make the *next* run cheaper than this one.
        schedule._jobs[job.id] = job
        schedule.save()
    if job.runs_on != "cloud":
        return {"ok": False, "error": "this job belongs to the user's own machine"}

    # Credentials, resolved in-process and never written to the volume.
    # `secrets.py`'s standing rule: somebody who moved a key into a vault did it
    # to stop the value living in a file, and writing it back under a different
    # name gives them the file back and takes away the revocation.
    try:
        values, unopenable = cloud_client.resolve_secrets(base, token, device)
        for name, value in values.items():
            os.environ[name] = value
            # Registered with the redactor the moment it exists, before any
            # tool output or transcript could carry it back out.
            from andromeda_agent import redact

            redact.register_known(value, f"secret:{name}")
        if unopenable:
            # Named, not silently dropped. A secret that will not decrypt is a
            # configuration failure a person has to fix; letting the job fail on
            # a missing variable instead points at the wrong thing entirely.
            print(f"[andromeda] these secrets could not be opened: {unopenable}")
    except cloud_client.CloudUnavailable as exc:
        # Not fatal. A job that needs none of them still runs, and one that does
        # will fail on the variable it wanted — with this line above it in the
        # log saying why.
        print(f"[andromeda] could not fetch hosted secrets: {exc}")

    # A repo job works on a fresh clone, not on a directory that persists. Every
    # run starts from the remote's actual state, so a job cannot slowly drift
    # onto a stale local branch and start deciding from a tree nobody else has
    # seen. The scratch path is the container's own disk, never the shared
    # volume — it is throwaway, and it is the one place a job may write freely.
    checkout = None
    if job.workspace_kind == "repo":
        from andromeda_agent import repo as repo_module

        scratch = Path("/tmp/andromeda-work") / job.id
        try:
            checkout = repo_module.prepare(
                job.repo_url,
                scratch,
                job.id,
                base_ref=job.repo_ref,
                branch_prefix=job.repo_branch_prefix,
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            job.workspace = str(checkout.path)
        except repo_module.RepoError as exc:
            try:
                fires.settle(job_id, fire_at, False)
            finally:
                VOLUME.commit()
            return {"ok": False, "error": str(exc)}

    ok = False
    run = None
    try:
        # `cron_cmd.execute`, not `runner.execute`. The CLI's wrapper is what
        # opens the execution-ledger row before anything with a side effect,
        # fires the lifecycle hooks, writes the output file and attaches the run
        # to its session — the same path `cron run` and the local daemon take.
        # Calling the inner one directly skipped all of it and, because the two
        # signatures differ, failed with `execute() got multiple values for
        # argument 'schedule'` at the first real fire.
        run = cron_cmd.execute(job, config_module.load(), schedule=schedule, source="fire")
        ok = run.status != "failed"
        try:
            cloud_client.report_run(base, token, device, job_id, fire_at, run)
        except cloud_client.CloudUnavailable:
            # A run that happened is still a run. Failing to *say so* must not
            # turn it into a failure — the report is retried by nothing, and
            # the output file on the volume is still the record.
            pass
        # Push only what the job actually changed, and only onto the branch this
        # run created. Nothing to commit is the *normal* outcome — a job that
        # checks something and finds it fine should leave no trace, and an empty
        # commit per tick is a repository nobody wants to read.
        if checkout is not None:
            from andromeda_agent import repo as repo_module

            try:
                changed = repo_module.commit_all(
                    checkout, f"{job.name}\n\n{(run.summary or '')[:1500]}"
                )
                if changed:
                    gh = os.environ.get("GITHUB_TOKEN", "")
                    branch = repo_module.push(checkout, token=gh)
                    print(f"[andromeda] pushed {branch}")
                    # A pushed branch nobody is told about is work that happened
                    # and did not arrive — the third shape of a failure this
                    # scheduler has already fixed twice. Best-effort: the commit
                    # is the durable artefact, and failing to announce it must
                    # not turn a successful run into a failed one.
                    try:
                        url = repo_module.open_pull_request(
                            checkout,
                            title=f"Andromeda: {job.name}",
                            body=(run.summary or "")[:20_000],
                            token=gh,
                        )
                        print(f"[andromeda] opened {url}")
                        run.summary = f"{run.summary}\n\nPull request: {url}".strip()
                    except repo_module.RepoError as exc:
                        print(f"[andromeda] branch pushed, no pull request: {exc}")
                        run.summary = (
                            f"{run.summary}\n\nPushed branch `{branch}` "
                            f"(no pull request: {exc})"
                        ).strip()
            except repo_module.RepoError as exc:
                # A run that did the work and could not push is not a run that
                # did nothing. Reported as a failure so it reaches the inbox,
                # because the work is in a container that is about to disappear.
                ok = False
                run.error = f"{run.error} · {exc}".strip(" ·")

        current = schedule.resolve(job_id)
        if current is not None and current.next_run_at:
            try:
                cloud_client.push_job(base, token, device, current)
            except cloud_client.CloudUnavailable:
                pass
    finally:
        # Settle first, then persist. A settled fire with unwritten state is a
        # job that repeats work; an unsettled fire with written state is a job
        # reported `unknown` forever. The first is recoverable and the second
        # needs a person, so the order is deliberate.
        try:
            fires.settle(job_id, fire_at, ok)
        finally:
            VOLUME.commit()

    # The error travels back, so `modal run` and a direct `.remote()` say why a
    # run failed. Without it the only answer is `{"ok": false}`, and the reason
    # is in a container that has already gone.
    return {
        "ok": ok,
        "status": getattr(run, "status", ""),
        "error": getattr(run, "error", ""),
        # Which secrets the container had, by presence and never by value. A
        # missing secret and a wrong one fail identically from the outside, and
        # this is the difference between guessing and knowing — it is what
        # turned "not signed in" from a mystery into a one-line answer.
        "env": {
            k: bool(os.environ.get(k))
            for k in (
                "ANDROMEDA_DEVICE_TOKEN",
                "ANDROMEDA_DEVICE_ID",
                "ANDROMEDA_BASE_URL",
                "ANDROMEDA_FIRE_SECRET",
            )
        },
        "home": str(home),
        # Where the agent actually looked, versus where the credential was
        # written. If these disagree, a profile or an env var resolved
        # differently than this function assumed — which is the failure that
        # reads as "not signed in" while the file plainly exists.
    }


@app.function(
    image=IMAGE,
    secrets=[
        modal.Secret.from_name("andromeda-fire"),
        modal.Secret.from_name("andromeda-runner"),
    ],
)
@modal.asgi_app()
def fire():
    """The runner's only door, served at the path the contract names.

    An ASGI app rather than a bare `fastapi_endpoint`, and the reason is the
    whole point of having a wire contract: `@modal.fastapi_endpoint` serves at
    `/`, so the fire would have had to arrive at the bare host — and the caller
    would need to know which host it was talking to in order to build the URL.
    §6 says the route is `/cron/fire`, so the host serves `/cron/fire`. A
    contract that bends per platform is not one.

    Identical in behaviour to `andromeda cron serve`: same signature, same
    refusals, same `202` before the work. The difference is that the work is
    `run_fire.spawn(...)` rather than a thread, which is what turns "one machine
    per user" into "any container that can reach the volume".
    """
    import sys

    sys.path.insert(0, "/opt/andromeda")

    from fastapi import FastAPI, Request, Response

    web = FastAPI()

    def answer(status: int, body: dict) -> Response:
        import json as _json

        return Response(
            content=_json.dumps(body), status_code=status, media_type="application/json"
        )

    @web.post("/cron/fire")
    async def handle(request: Request) -> Response:  # noqa: D401
        from andromeda_agent import cloud_client
        from andromeda_agent.fires import Outcome, RemoteFires
        from andromeda_agent.serve import FireError, verify_hmac

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - a body that is not JSON is not a fire
            return answer(400, {"error": "body must be JSON"})
        if not isinstance(payload, dict):
            return answer(400, {"error": "body must be an object"})

        secret = os.environ.get("ANDROMEDA_FIRE_SECRET", "")
        if not secret:
            # Refused, not started. A runner with no secret either accepts every
            # fire or refuses every fire while looking healthy, and a machine
            # that answers nothing is the failure that takes longest to find.
            return answer(503, {"error": "this runner has no fire secret configured"})

        token = str(payload.get("token") or "")
        try:
            verify_hmac(secret, payload, token)
        except FireError as exc:
            return answer(exc.status, {"error": exc.reason})

        user_id = str(payload.get("user_id") or "")
        job_id = str(payload.get("job_id") or "")
        fire_at = str(payload.get("fire_at") or "")
        if not user_id:
            return answer(400, {"error": "user_id is required"})

        base = os.environ.get("ANDROMEDA_BASE_URL", "")
        device_token = os.environ.get("ANDROMEDA_DEVICE_TOKEN", "")
        device = os.environ.get("ANDROMEDA_DEVICE_ID", "")
        fires = RemoteFires(
            cloud_client.FireClaims(base, device_token, device), user_id
        )

        try:
            outcome = fires.claim(job_id, fire_at)
        except Exception as exc:  # noqa: BLE001
            # An unanswerable claim is not a claim. `503` is retryable; assuming
            # a win would let two containers run one job.
            return answer(503, {"error": f"could not claim this fire: {exc}"})

        if outcome is Outcome.UNKNOWN:
            return answer(
                409,
                {
                    "error": "a previous attempt at this fire never reported. Its "
                    "side effects may or may not have run, so it is not retried "
                    "automatically."
                },
            )
        if outcome in (Outcome.IN_FLIGHT, Outcome.SETTLED):
            # Accepted, not refused: the caller's contract is "202 means stop
            # retrying", and a duplicate is exactly when it should.
            return answer(202, {"status": "duplicate", "job_id": job_id})

        # `.aio`, because this handler is async. The blocking form works and
        # warns, and a warning on every fire is a warning nobody reads.
        await run_fire.spawn.aio(user_id, job_id, fire_at)
        return answer(202, {"status": "accepted", "job_id": job_id})

    return web
