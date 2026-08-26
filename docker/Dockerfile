# The hosted runner.
#
# One job: run Andromeda's scheduler loop on a machine that is switched off
# most of the time, so a scheduled job fires whether or not the person's own
# computer is awake.
#
# Three properties this image is built for, in priority order:
#
#   1. **It starts fast.** The machine is stopped between fires and woken by the
#      request that fires it, so boot time is on the critical path of every
#      single run. Nothing goes in here that is not needed to answer a fire.
#   2. **It holds no model credential.** The provider is the hosted relay and
#      the credential is a device token, exactly as the packaged desktop app
#      works. A container with an OpenRouter key in it would be a key on
#      hardware the user does not control, next to agent-authored scripts.
#   3. **Its state is on a volume, not in the layer.** `/data` is the whole of
#      ANDROMEDA_HOME. The image is disposable; the volume is not.

# Pinned by digest, not by tag. `python:3.13-slim` is a moving target and a
# rebuild that silently changes the interpreter is a rebuild that can change
# behaviour with no diff to point at.
FROM python:3.13.7-slim@sha256:5f55cdf0c5d9dc1a415637a5ccc4a9e18663ad203673173b8cda8f8dcacef689 AS base

# uv, by digest for the same reason. Copied from the published image rather
# than curl-installed so the version is a build argument and not whatever the
# installer script serves today.
COPY --from=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 /uv /uvx /usr/local/bin/

# The full list, and it is short on purpose. Each one is here because a tool
# the agent can call shells out to it:
#
#   git             `repo` jobs (C8) and any job that reads a checkout
#   ripgrep         `search_files`
#   ca-certificates every outbound HTTPS call, including the relay itself
#
# Nothing else. A job that shells out to a binary this image lacks fails in a
# way that is invisible exactly while you are looking at it — it works when you
# run it by hand on your Mac and fails the moment the runner tries. `andromeda
# doctor --cloud` asserts this list at boot rather than leaving it to discovery.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# Not root. The agent runs arbitrary model-chosen commands inside this
# container; the blast radius of a mistake should stop at a user who owns one
# directory.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin andromeda

WORKDIR /opt/andromeda

# Dependencies before source, so an edit to the CLI does not re-resolve the
# whole environment. `--frozen` refuses to update the lock: a build that
# silently resolves a different tree is a build whose test run proved nothing.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY andromeda_agent ./andromeda_agent
COPY andromeda_cli ./andromeda_cli
COPY andromeda_tools ./andromeda_tools
COPY andromeda_tui ./andromeda_tui
RUN uv sync --frozen --no-dev

# `/data` is ANDROMEDA_HOME in full: config.yaml, credentials.json (0600),
# cron/, sessions/, scripts/, state.db, SOUL.md. Every path the CLI touches
# already routes through `config.home()`, so this is one variable rather than a
# refactor.
#
# Declared as a VOLUME so a `docker run` with no mount still gets a writable
# path instead of failing at the first save — but a real deployment mounts a
# durable disk here, because a job's monitor baseline and notepad are the only
# things that make the *next* run cheaper than the last.
ENV ANDROMEDA_HOME=/data \
    PATH="/opt/andromeda/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data && chown andromeda:andromeda /data
VOLUME ["/data"]

USER andromeda

# Deliberately NOT `cron daemon`. The local tick loop is the wrong shape for a
# machine that is meant to be stopped: it would keep the container alive around
# the clock to discover, sixty times an hour, that nothing is due — which is
# both the bill and the reason scale-to-zero exists.
#
# `cron serve` is the runner's whole job: one route, woken by the request that
# fires a job. It refuses to start without ANDROMEDA_FIRE_SECRET, so a
# misconfigured deploy fails loudly at boot rather than answering nothing while
# looking healthy.
#
# `docker run <image> doctor --cloud` still works and is the boot check —
# ENTRYPOINT is the bare command precisely so both are reachable.
ENTRYPOINT ["andromeda"]
# `doctor --cloud` and not plain `doctor`: it exits non-zero when this container
# could not actually do the job, which makes `docker run <image>` a real check
# rather than a screenful somebody has to read. It is a one-shot boot check, not
# a HEALTHCHECK — a periodic probe would keep a machine awake that is supposed
# to be stopped, which is the bill scale-to-zero exists to avoid.
CMD ["cron", "serve"]

EXPOSE 8080
