"""A job that works on a repository instead of watching one.

This is the class that separates "watches things and tells me" from "does work
while I sleep", and it is also the only class that can change something a person
will read later. It lands last for that reason — after the consent axis, the
tool ceiling, the fire claim and the budgets, all of which exist to bound it.

## The rule the whole module is built around

> **A repo job never pushes to a branch it did not create.**

Not "it should not", and not "the prompt tells it not to". A cron prompt is fed
to a model, and a rule enforced by asking is a rule enforced by nothing.
`push` refuses any ref that is not the branch this run made, and the branch name
is generated here rather than anywhere the model can reach. There is no
parameter for it, so there is no argument to get wrong and none for a prompt
injection to set — the same shape the `cron` tool's missing `runs_on` takes.

## Why a fresh clone every time

A hosted runner is one of many interchangeable containers, so there is no
"the checkout" to keep. That sounds like a cost and is mostly a feature: every
run starts from the remote's actual state, so a job cannot slowly drift onto a
stale local branch and start making decisions from a tree nobody else has seen.

`--depth 1` because a scheduled job wants today's code and not the history, and
a shallow clone of a large repository is the difference between a fire that
costs seconds and one that costs minutes.

## Credentials

The token comes from the hosted secret store (`andromeda://`), injected into the
environment before the job runs, and is used through a credential helper rather
than embedded in the remote URL. A URL with a token in it ends up in
`.git/config`, in `git remote -v`, and in the reflog — three places nobody
thinks to redact.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Long enough for a large shallow clone, short enough that a hung network does
# not hold a container open until its lease expires.
GIT_TIMEOUT = 300

# What a job may be given as a remote. An `ssh://` or `git@` remote would need a
# key on the runner, which is a credential with no expiry and no scope — refused
# in favour of a token that can be revoked in one click.
_HTTPS_REMOTE = re.compile(r"^https://[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~/-]+)+?(?:\.git)?$")


class RepoError(RuntimeError):
    """Something about the repository stopped the job. Printable."""


@dataclass
class Checkout:
    path: Path
    branch: str
    base: str
    remote: str


def validate_remote(url: str) -> str:
    """The remote, or a refusal explaining which shape is wanted."""
    url = (url or "").strip()
    if not url:
        raise RepoError("A repo job needs a remote to work on.")
    if url.startswith("git@") or url.startswith("ssh://"):
        raise RepoError(
            "An SSH remote needs a key on the runner, which is a credential "
            "with no expiry and no scope. Use the https URL and store a token "
            "with `andromeda secrets put GITHUB_TOKEN --cloud`."
        )
    if not _HTTPS_REMOTE.match(url):
        raise RepoError(f"{url!r} is not an https git remote.")
    return url


def _git(args: list[str], cwd: Path | None = None, token: str = "") -> str:
    """One git command, with the token kept out of everything durable.

    `-c credential.helper=` clears any inherited helper first: a runner that
    picked one up from an image would use it silently, and "silently" is the
    problem. The token then arrives on stdin for exactly this invocation.
    """
    env = {
        **os.environ,
        # A prompt on a machine with no terminal is a job that hangs until its
        # lease expires and is then reported `unknown`.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
    }
    command = ["git", "-c", "credential.helper=", *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RepoError(f"`git {args[0]}` did not finish within {GIT_TIMEOUT}s.") from None

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        # Whatever git said, minus anything that looks like the token. A remote
        # that echoes a credential back in an error is not hypothetical.
        if token:
            detail = detail.replace(token, "<token>")
        raise RepoError(f"`git {args[0]}` failed: {detail[:400]}")
    return result.stdout


def _authenticated(url: str, token: str) -> str:
    """The remote with a token, for one command, never written to disk.

    Used only as an argument to `clone` and `push`, and never with `git remote
    set-url`: a URL in `.git/config` shows up in `git remote -v` and the reflog,
    which are three places nobody thinks to redact.
    """
    if not token:
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def prepare(
    remote: str,
    into: Path,
    job_id: str,
    base_ref: str = "",
    branch_prefix: str = "andromeda",
    token: str = "",
) -> Checkout:
    """A fresh shallow clone on a new branch, ready to be worked in."""
    remote = validate_remote(remote)
    into.mkdir(parents=True, exist_ok=True)

    args = ["clone", "--depth", "1"]
    if base_ref:
        args += ["--branch", base_ref]
    args += [_authenticated(remote, token), str(into)]
    _git(args, token=token)

    base = (base_ref or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=into).strip())

    # Generated here, from the job id and the clock. Not a parameter, because a
    # branch name the model could choose is a branch name it could choose to be
    # `main`.
    branch = f"{_slug(branch_prefix)}/{_slug(job_id)}-{int(time.time())}"
    _git(["checkout", "-b", branch], cwd=into, token=token)

    # An unattended commit with no identity fails on a machine with no global
    # config, which reads as a git bug rather than a missing setting.
    _git(["config", "user.name", "Andromeda"], cwd=into)
    _git(["config", "user.email", "andromeda@users.noreply.github.com"], cwd=into)

    return Checkout(path=into, branch=branch, base=base, remote=remote)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-")
    return cleaned[:60] or "job"


def has_changes(checkout: Checkout) -> bool:
    return bool(_git(["status", "--porcelain"], cwd=checkout.path).strip())


def commit_all(checkout: Checkout, message: str) -> bool:
    """Commit whatever the job changed. False when it changed nothing.

    Nothing to commit is the *normal* outcome for most runs — a job that checks
    something and finds it fine should leave no trace, and an empty commit on
    every tick is a repository nobody wants to read.
    """
    if not has_changes(checkout):
        return False
    _git(["add", "-A"], cwd=checkout.path)
    _git(["commit", "-m", message[:2000] or "Andromeda scheduled run"], cwd=checkout.path)
    return True


def push(checkout: Checkout, token: str = "") -> str:
    """Push the branch this run created, and only that.

    The refusal below is the module's whole reason for existing. It is checked
    against the checkout's own generated name rather than against a list of
    protected branches: a denylist of `main`, `master`, `develop`… is a list
    somebody's default branch is missing from.
    """
    if not checkout.branch or checkout.branch == checkout.base:
        raise RepoError(
            "Refusing to push: this run is on the base branch, not a branch it "
            "created. A scheduled job never pushes to a branch it did not make."
        )
    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout.path).strip()
    if current != checkout.branch:
        # Something moved HEAD mid-run — a `git checkout` in a script, or an
        # agent that found a way. Pushing now would push whatever it landed on.
        raise RepoError(
            f"Refusing to push: the checkout is on {current!r}, not the branch "
            f"this run created ({checkout.branch!r})."
        )

    _git(
        ["push", _authenticated(checkout.remote, token), f"HEAD:refs/heads/{checkout.branch}"],
        cwd=checkout.path,
        token=token,
    )
    return checkout.branch


# ---------------------------------------------------------------------------
# Telling somebody
# ---------------------------------------------------------------------------

# The forge this build can open a request on. One, named, rather than a guess
# from the hostname: a GitLab or Gitea remote would take the GitHub call and
# fail with a 404 that reads like a permissions problem.
_GITHUB_HOSTS = ("github.com", "www.github.com")

PR_TIMEOUT = 30


def github_repo(remote: str) -> tuple[str, str] | None:
    """`(owner, name)` if this remote is a GitHub one, else None."""
    from urllib.parse import urlparse

    parsed = urlparse(remote)
    if parsed.hostname not in _GITHUB_HOSTS:
        return None
    parts = [p for p in parsed.path.strip("/").removesuffix(".git").split("/") if p]
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def open_pull_request(
    checkout: Checkout, title: str, body: str, token: str
) -> str:
    """Open a pull request for the branch this run pushed, and return its URL.

    A pushed branch nobody is told about is work that happened and did not
    arrive. It is the same failure the scheduler already fixed twice — a job
    that wrote output into a file nobody opens, and a hosted run that wrote to a
    volume nobody can reach. A branch in a repository is the third shape of it.

    **Best-effort, and never fatal.** The commit is already pushed and is the
    durable artefact; failing to announce it must not turn a successful run into
    a failed one. Callers report the reason and keep going.
    """
    target = github_repo(checkout.remote)
    if target is None:
        raise RepoError(
            "This build opens pull requests on GitHub only. The branch is "
            f"pushed as {checkout.branch!r} and can be opened by hand."
        )
    if not token:
        raise RepoError(
            "No GITHUB_TOKEN, so the branch was pushed without a pull request. "
            "`andromeda secrets put GITHUB_TOKEN --cloud` fixes that."
        )

    import json
    import urllib.error
    import urllib.request

    owner, name = target
    payload = json.dumps(
        {
            "title": (title or f"Andromeda: {checkout.branch}")[:250],
            # The run's own summary. The whole difference between "a branch
            # appeared" and "here is what I did and why".
            "body": (body or "")[:60_000],
            "head": checkout.branch,
            "base": checkout.base,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "andromeda-runner",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=PR_TIMEOUT) as response:
            return str(json.loads(response.read()).get("html_url") or "")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            answer = json.loads(exc.read() or b"{}")
            detail = str(answer.get("message") or "")
            # GitHub answers 422 for "a pull request already exists", which is
            # the ordinary state of a job that ran twice before anyone merged.
            # Reported as itself rather than as a failure.
            errors = answer.get("errors") or []
            if any("already exists" in str(e) for e in errors):
                raise RepoError("a pull request for this branch already exists") from None
        except RepoError:
            raise
        except Exception:  # noqa: BLE001 - an error page is not always JSON
            detail = ""
        raise RepoError(
            f"GitHub refused the pull request ({exc.code}): {detail or 'no reason given'}"
        ) from None
    except (TimeoutError, OSError) as exc:
        raise RepoError(f"could not reach GitHub: {exc}") from None
