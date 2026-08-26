# The runner on Modal

The same runner, on a host that does not give each user a machine. Read
`RUNBOOK.md` first for what the runner *is*; this is only what changes.

## Why the move

Fly's default cap is **50 machines per organisation**, counting stopped ones, so
machine-per-user has a hard ceiling at roughly the fiftieth user. Modal bills per
second with no idle tail against Fly's ~303s minimum per fire — and since ~80% of
fires are monitor ticks that suppress in about three seconds, that minimum is a
15x multiplier on exactly this workload. At 1,000 users the difference is
~$340/month against ~$27.

## The one semantic change

**The at-most-once claim moved from SQLite to the server.**

It had to. Modal's Volumes are last-write-wins and their own documentation says
not to put SQLite on one; two containers claiming the same fire from two copies
of a database is precisely the double-send the claim exists to prevent. So it
lives in Convex, where a mutation is already a transaction and the scheduler
already knows when a job is due.

The four outcomes are unchanged — `won`, `in_flight`, `settled`, and an expired
lease that is **`unknown` and never reclaimed**. `serve.Runner` takes either
backend and cannot tell them apart, which is what makes the host a deployment
decision rather than a rewrite.

It also made the system *more* correct: a claim that works across N containers
also works across one.

## The one security change

**`user_id` is now part of the signed material.**

On a machine-per-user runner the tenant was implied by which machine a fire
reached, so it never travelled. A shared pool serves every tenant from one
endpoint, and an unsigned `user_id` there means anyone holding one valid fire can
replay it with the field swapped — claiming and settling against somebody else's
job. Signed on both sides: `serve._SIGNED_FIELDS` and `convex/cloudFire.ts`.

## State

One Volume, mounted at `/data`, with `/data/users/<user_id>/` as that user's
`ANDROMEDA_HOME`. Not a volume each: at 2.7 MB a head there is nothing to isolate
that a directory does not, and a volume per user would recreate the exact
coupling this migration undoes.

`state.db` is deliberately **not** on the volume. It is a rebuildable index over
`sessions/`, `transfer.py` already excludes it from backups, and it is SQLite —
the one thing a Volume must not hold.

`VOLUME.reload()` runs before reading the store, because another container may
have written since this one started. `VOLUME.commit()` runs after settling, in
that order: a settled fire with unwritten state repeats some work, an unsettled
fire with written state is reported `unknown` forever. The first is recoverable
and the second needs a person.

## Deploying

```sh
pip install modal
modal setup                      # opens a browser, one time

modal secret create andromeda-fire \
  ANDROMEDA_FIRE_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(24))')"

modal secret create andromeda-runner \
  ANDROMEDA_BASE_URL=https://ai-andromeda.com \
  ANDROMEDA_DEVICE_TOKEN=<the runner's own device token> \
  ANDROMEDA_DEVICE_ID=<its device id>

modal deploy cli/modal_app.py
```

Modal prints a web endpoint. Register it as the machine's `callbackUrl` so
`cloudFire.fire` posts there:

```sh
# via the CLI once `cloud up` exists, or directly against the mutation
#   cloudCron.registerMachine { provider: "modal", callbackUrl: "<endpoint>" }
```

The same fire secret must be sealed server-side, because Convex mints the token
and the runner verifies it. They are two copies of one secret and there is no
way around that with symmetric signing — the asymmetric upgrade path is the seam
`VERIFIERS` already takes.

## Verifying

```sh
# 1. an unauthenticated POST should be 401 — also the cold-start measurement
time curl -sS -X POST -d '{}' -o /dev/null -w '%{http_code}\n' <endpoint>

# 2. a signed fire for a real job should be 202 accepted
# 3. the identical fire again should be 202 duplicate, and run nothing
# 4. `andromeda cron runs` should show the outcome
```

There is deliberately no health endpoint. A `GET` answering `200` would be an
unauthenticated way to keep a container warm that is supposed to be cold.

## State, 2026-08-25

**Deployed and answering.** `https://zekevoigt--andromeda-runner-fire.modal.run`
— cold 5.9s, warm 0.30s, and every refusal verified against the live endpoint:
no token 401, forged token 401, and no route but the fire route.

**Fly is retired.** `andromeda-runner` and its volume are destroyed. This is the
only runner.

**One thing blocks the end-to-end loop, and it is not in this directory.** The
`/api/cloud/*` routes need Convex functions on whichever Convex deployment the
website uses, and that is not the dev deployment these were pushed to. Deploying
them there is not currently safe:

```
  merge-base ── 12 commits ──▶  codex/marketing-release-20260813  (production)
      │
      └───────  164 commits ──▶  feat/cli-cloud-autonomy          (this work)

  production defines 20+ tables this branch's schema does not.
  `convex deploy` from here would drop them.
```

Convex replaces the whole schema on deploy, so pushing this branch's
`schema.ts` to production would remove `quickbooksAccounts`, `shopifyAnalytics`,
`asanaWebhooks`, `gatewayCronJobs` and about seventeen others that the live site
defines. The two lines diverged 164 commits ago and production is not an
ancestor of `main`.

**The fix is a merge, not a deploy.** This branch has to be reconciled with the
production line before its Convex functions can go anywhere near it. Until then
the runner is live and correct and has nothing to talk to.
