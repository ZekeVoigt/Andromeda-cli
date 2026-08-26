# The hosted runner — build, deploy, verify

Everything measured below was measured, not estimated. Where a number is still
unknown it says so.

## What this is for

A scheduled job today only fires while the user's own computer is awake. The
runner is a machine that is **stopped almost all of the time** and woken by the
HTTP request that fires a job. It exists so `detached` jobs — the ones that
touch the network and not the user's disk — keep running overnight.

## Build

```sh
cd cli
docker build --network=host -f docker/Dockerfile -t andromeda-runner:dev .
```

**`--network=host` is not optional on Colima.** BuildKit's build containers get
a network namespace that cannot reach the VM's resolver, so `apt-get update`
fails with `Unable to connect to deb.debian.org` — which then surfaces as
`Unable to locate package git`, an error that reads like a missing package and
is actually a dead network. A plain `docker run` container resolves and connects
fine, which is what makes this confusing to diagnose: every test you reach for
first passes. On Docker Desktop and in CI the flag is harmless.

## Verify — the boot check

```sh
docker volume create andromeda-data
docker run --rm -v andromeda-data:/data andromeda-runner:dev doctor --cloud
```

The image's `CMD` is `cron serve`; `doctor --cloud` is the boot check and is run
explicitly, as above. It **exits non-zero** when this container could not
actually do the job. Seven checks, each a real failure
somebody has had:

| check | what it catches |
|---|---|
| `git present` / `rg present` | a job shells out to one, it works on your Mac, and it fails the moment the runner tries — invisible exactly while you are looking at it |
| `home on volume` | `ANDROMEDA_HOME` pointed at the image layer, so every monitor baseline and notepad is discarded on the next boot |
| `home writable` | a read-only mount, which fails *after* the model call |
| `free space` | a partial write to `state.db` is worse than a skipped run |
| `no model key` | a provider key on hardware the user does not control, next to agent-authored scripts |
| `provider` | a runner must be on the relay lane, where billing authority is server-side |
| `skills` | informational, never a failure — skills live on the volume (`/data/skills`), so a fresh runner correctly has none. It is reported because the failure it precedes is confusing: a job naming a skill the runner lacks fails on a missing file, which reads as a broken job rather than as un-synced content |

Expected on a healthy runner: all green, exit `0`. Expected on your laptop:
`home on volume` and `no model key` both fail, exit `1`. That is correct — a
Mac is not a runner, and a check that can only pass is worth nothing.

## Verify — state survives a restart

```sh
docker run --rm -v andromeda-data:/data andromeda-runner:dev \
  cron add "every 1h" "watch the status page" --cloud --detached
docker run --rm -v andromeda-data:/data andromeda-runner:dev cron list
```

The second command is a **different container**. Seeing the job proves the
volume, not the layer, is holding the store.

## Measured, 2026-08-25

On Colima, 2 CPU / 4GB, arm64:

| | |
|---|---|
| image size | 488 MB |
| bare interpreter | 0.03 s |
| import everything a fire needs | **0.29 s** |
| import the whole CLI entrypoint | 0.38 s |
| container create + start + run + exit | 1.15–1.26 s |
| stopped container → started → output | **1.12–1.21 s** |

The last row is the closest local analogue to a Fly wake, and it is the number
that mattered: the plan's C0 gate was "if cold start exceeds ~10s the wake
mechanism changes before anything else is built." It does not. The Python import
graph — the only term we control — is 0.29s of it, so there is nothing to go and
optimise.

**Still unmeasured:** Fly's own stopped→running time, which is the term this
machine cannot produce. It is the difference between the 1.2s above and the real
number, and it needs an actual deploy.

## Deploy

```sh
fly launch --no-deploy --copy-config --name andromeda-runner   # first time only
fly volumes create andromeda_data --size 1 --region iad
fly deploy --config docker/fly.toml
```

Then set the runner's own device token — it pairs as **its own device**, never
by copying the laptop's:

```sh
fly ssh console -C "andromeda auth login <code>"
```

`transfer.py backup` includes a live device token, so seeding the volume from a
Mac backup would put one credential on two machines. `getDeviceByDeviceId` takes
`.first()` off the index, so the second row silently shadows the first and
authentication breaks for a machine that was just told it was paired.

## One machine, and why that is not a limit on concurrent jobs

Fly maps volumes to Machines **one-to-one** — a volume cannot be attached to two
Machines at once — so a single-volume app is a single-machine app by
construction. Keep it that way with `fly scale count 1`, and note that Fly never
creates Machines from traffic: autostart only starts ones that already exist.

That is a limit on *schedulers*, not on *jobs*. Two jobs whose cron expressions
land in the same second both run:

```
  02:00:00.000  fire A arrives  ──▶  202 accepted   (~50ms, connection closed)
  02:00:00.030  fire B arrives  ──▶  202 accepted   (~50ms, connection closed)
  02:00:00.050  ┌─ job A running in the background ─────────────┐
                └─ job B running in the background ─────────────┘  minutes
```

The `202`-before-run design is what makes this true: the HTTP request is
milliseconds and the work is minutes, so the proxy sees a machine that is
essentially never busy. Binding the proxy's concurrency limits low would queue
fires against each other while the machine sat idle — which an earlier version
of `fly.toml` did, with a comment claiming it capped machine count. It does not;
those limits control routing only.

**The real ceiling is memory**: N concurrent agent turns in one 512MB machine.
`cron serve` enforces it with a semaphore and answers the N+1th fire `503`,
which is retryable — the caller comes back rather than having its fire accepted
and then starved behind three others. `ANDROMEDA_MAX_CONCURRENT_JOBS` sets it;
the default is 2.

Measured in-container, 2026-08-25:

| | RSS |
|---|---|
| bare interpreter | 11 MB |
| + the serve module | 54 MB |
| + session assembly and the whole tool registry | 55 MB |
| the idle `cron serve` process | 59 MB |

So the fixed cost is ~60MB and imports are shared across threads. **The
per-turn increment is still unmeasured** — it needs a real model call, and the
transcript is the part that grows. Until that number exists, 2 is a
deliberately conservative default rather than a tuned one, and raising it
without measuring would be guessing with somebody's job.

## Drive a fire by hand

```sh
SECRET=$(python3 -c "import secrets;print(secrets.token_hex(24))")
docker run -d --name andro-serve -e ANDROMEDA_FIRE_SECRET="$SECRET" \
  -v andromeda-data:/data -p 18080:8080 andromeda-runner:dev

JOB=<a job id from `cron list`>
BODY=$(SECRET="$SECRET" JOB="$JOB" python3 -c "
import hmac,hashlib,json,time,os
s=os.environ['SECRET']; j=os.environ['JOB']
f='2026-08-25T02:00:00+00:00'; e=int(time.time())+120
print(json.dumps({'job_id':j,'fire_at':f,'exp':e,
  'token':hmac.new(s.encode(),f'{j}|{f}|{e}'.encode(),hashlib.sha256).hexdigest()}))")
TOKEN=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -X POST localhost:18080/cron/fire -H "Authorization: Bearer $TOKEN" -d "$BODY"
```

Expected: `202 {"status":"accepted"}`, then the same request again gives
`202 {"status":"duplicate"}` and the job runs exactly once. `cron fires` shows
it settled; `cron logs <job>` shows the output.

## Stopping a runner, and the switch

`cron serve` catches `SIGTERM` — which is how a container runtime says stop —
and **drains**: it refuses new fires with `503` and waits up to 25s for jobs
already running, so they settle as ordinary recorded runs. `fly.toml`'s
`kill_timeout = "30s"` is the matching promise. Set it shorter than the drain
and the grace period is decorative: the process is killed mid-drain, and the
fire it was finishing becomes an `unknown` that needs a person.

Anything still running when the drain times out is named in the logs at that
moment, rather than left for somebody to find in a ledger a week later with
nothing to connect it to the deploy that caused it.

**`ANDROMEDA_CLOUD_OFF=1` refuses every fire** and leaves the server up. Read
fresh on every fire, not at boot — read once at startup it would be a switch you
have to restart a machine to use, which is the wrong shape for the thing you
reach for at 3am. It stays up deliberately: a machine that vanishes when the
switch is thrown looks identical to one that crashed, and the person who threw
it then cannot tell whether it worked.

```sh
fly secrets set ANDROMEDA_CLOUD_OFF=1     # stop
fly secrets unset ANDROMEDA_CLOUD_OFF     # go
```

## Management commands run as **root**, and that will break the runner

`fly ssh console` gives you a root shell. The runner runs as the unprivileged
`andromeda` user, and `Schedule.save` writes `cron.json` mode 0600 — owned by
whoever wrote it. So this:

```sh
fly ssh console --app andromeda-runner -C "andromeda cron add ..."   # WRONG
```

leaves a root-owned 0600 store the runner cannot read. Always drop privileges:

```sh
fly ssh console --app andromeda-runner \
  -C "su andromeda -s /bin/sh -c 'andromeda cron add ...'"
```

This was found the first time a real fire was sent to a real deployment, and the
symptom was worth the trip: the endpoint answered **"no such job"** for a job
`cron list` had shown one command earlier. The store was fine; the runner simply
could not open it, and `load` swallowed the `PermissionError`.

Repair, if it has already happened:

```sh
fly ssh console --app andromeda-runner -C "chown -R andromeda:andromeda /data"
```

`Schedule.load` now records *why* a read failed instead of leaving an empty
schedule, `cron serve` prints it at boot, `cron list` prints it under the
listing, and the fire endpoint answers **`503`** rather than `404` — retryable,
because a `chown` fixes it, and telling the caller to stop retrying would not.

## Measured on Fly, 2026-08-25

`andromeda-runner`, `shared-cpu-1x` / 512MB, `iad`, 1GB volume, deployed image
91 MB:

| | |
|---|---|
| warm request (machine running) | 0.09 s |
| **stopped → woken by the fire request → answered** | **4.60 – 4.86 s** |
| **idle → Fly stops the machine again** | **~303 s (≈5 min)** |

Three cold runs, machine confirmed `stopped` before each. The C0 gate was ~10s,
so it passes — with less headroom than the 1.2s local container suggested.

The wake split is the useful part: **~4.4s is Fly booting the machine and
routing, 0.29s is our Python.** The term we control is 6% of the total. There is
no import graph worth optimising, and any future work on wake latency is a
conversation with the platform, not with this codebase.

### The five minutes shapes machine cost — but machine cost is the small term

**Corrected 2026-08-25, after pricing it.** An earlier version of this section
called the five-minute idle window "the cost model". It is not. Priced out:

| | |
|---|---|
| one fire (5 min of shared-cpu-1x/512MB) | **$0.000384** |
| one typical agent turn (20k in / 2k out, deepseek-v4-flash) | **$0.0034** |

**The model costs about nine times the machine.** A heavy turn is 26x. So the
limit that actually protects the business is the inference budget — which
already exists as credits — and a fires-per-day cap is a guard on a term worth
roughly a tenth as much.

What survives from the original claim, and is still worth designing around:

Fly stops an idle machine after about five minutes, not immediately. So **the
minimum billable unit is ~5 minutes of machine time per fire, however fast the
job is.** A two-second watchdog and a four-minute agent turn cost the same.

Three things follow, and they run against normal scheduling advice:

- **Monitor mode is worth far more here than locally.** On a laptop a suppressed
  tick saves a model call. On a runner a tick that never fires at all costs
  *nothing*, and one that fires costs five minutes of machine whether or not the
  agent runs. The cheap-source hash is now the difference between a job that is
  free and a job that is not.
- **Fires that land within five minutes of each other are free after the
  first.** They share one awake window. So jobs should be scheduled to *cluster*
  — on the hour, together — which is the opposite of the usual "add jitter to
  spread load" advice. Jitter here buys nothing and costs a whole extra window.
- **Within machine cost, frequency is the variable and duration is not.**
  Bounding how long a job runs once awake is nearly free by comparison. The
  fires-per-day cap is still the right *shape* for machine time — it is simply
  guarding the smaller of the two bills.

A duty cycle falls out of it: an hourly job keeps the machine up ~5 of every 60
minutes, so ~8%. A job every ten minutes keeps it up ~50%. A job every five
minutes never lets it stop at all, which is the cliff worth knowing about before
somebody writes `every 5m` and wonders why their bill looks like a
always-on server.

## Measure the real wake, once deployed

```sh
fly machine stop <id>
time curl -sS -X POST -d '{}' \
  -o /dev/null -w '%{http_code}\n' https://andromeda-runner.fly.dev/cron/fire
```

`401` is the expected answer, and it is the right probe: it proves the machine
came up **and** that the lock works, in one call. There is deliberately no
health endpoint — a `GET` answering `200` would be an unauthenticated way to
keep awake a machine that is supposed to be stopped.
