"""Refusing to go looking for somebody's secrets.

A real session: asked to build a job that emails a summary, the agent wanted an
email provider. Rather than saying so, it ran

    grep -iE "resend|sendgrid|ses|smtp|mailgun|postmark|brevo" ~/*.txt

found a live `RESEND_API_KEY` in an old recovery dump, used it, and copied it
into a third party's environment. Every individual step was permitted. The
sequence was not something anybody had agreed to.

Two things are wrong with it, and only the second is about permissions.

**A key found in one project was not given to you for another.** Its owner
scoped it to a thing they were building last year. Reusing it silently widens
that scope, spreads the secret to new places, and makes a later rotation break
software nobody remembers deploying.

**The order is wrong.** Credentials should come from a connected app, then the
workspace's own configuration, then a question. Sweeping a home directory is
not a fallback in that list — it is a different activity that happens to
produce a string that works.

So: a command that looks like a credential sweep is refused, and the refusal
says what to do instead. This is narrow by construction. Reading a `.env` in
the workspace is normal work and is not touched; `grep -r AWS_SECRET ~` is not.
"""

from __future__ import annotations

import re

# Names that only appear when somebody is looking for a secret. Deliberately
# the *shape* of a credential name rather than a list of vendors: a vendor list
# is one new product away from being incomplete, and the point is not to play
# whack-a-mole with brands.
SECRET_WORDS = re.compile(
    r"(?<![\w-])("
    r"api[_-]?key|secret[_-]?key|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|bearer[_-]?token|password|passwd|credentials?"
    r")(?![\w-])"
    # Screaming-snake environment names, matched whole. The alternatives above
    # carry a `(?<![\w-])` that an underscore defeats: in
    # `AWS_SECRET_ACCESS_KEY` the character before `ACCESS_KEY` is `_`, which
    # is a word character, so `access[_-]?key` never fires. This catches the
    # whole name instead of trying to find a boundary inside it.
    r"|(?<![\w-])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS?)(?![\w-])",
    re.IGNORECASE,
)

# Vendor names are still worth catching, but only together with a search that
# ranges outside the workspace — which is what the caller checks.
VENDOR_WORDS = re.compile(
    r"(?<![\w-])("
    r"resend|sendgrid|mailgun|postmark|brevo|twilio|stripe|openai|anthropic"
    r"|aws|gcp|azure|github|gitlab|slack|notion|linear|vercel|supabase"
    r")(?![\w-])",
    re.IGNORECASE,
)

SEARCH_TOOLS = re.compile(
    r"(?<![\w./-])(grep|rg|ripgrep|ag|ack|find|fgrep|egrep)(?![\w-])"
)

# Places that are somebody's whole life rather than the thing being worked on.
#
# Depth is what separates them. `~/notes.txt` and `/Users/someone` are a home
# directory; `/Users/someone/code/api/src` is a project that happens to live in
# one. So the home prefix is matched, and then how far *below* it the search is
# aimed decides — at most one component down counts as sweeping the home
# directory itself.
_HOME_PREFIX = re.compile(
    r"(?:^|[\s\"'=:])(~|\$HOME|\$\{HOME\}|/Users/[^/\s\"']+|/home/[^/\s\"']+)"
    r"(/[^\s\"']*)?"
)

#: How deep below a home directory still counts as sweeping it.
HOME_DEPTH = 1

# Stores whose whole content is credentials. Depth does not apply: these are
# secrets wherever they are.
SECRET_STORES = re.compile(
    r"/\.ssh(/|\b)"
    r"|/Library/(Keychains|Application Support)"
    r"|\.aws/credentials|\.netrc|\.npmrc|\.pypirc|\.docker/config\.json"
    r"|(?<![\w./-])id_(rsa|ed25519|ecdsa)(?![\w-])"
    r"|(?<![\w./-])Keychain",
    re.IGNORECASE,
)


def _searches_somewhere_broad(text: str) -> bool:
    """Whether the command ranges over a home directory or a secret store."""
    if SECRET_STORES.search(text):
        return True
    for found in _HOME_PREFIX.finditer(text):
        tail = (found.group(2) or "").strip("/")
        if not tail:
            return True
        # `~/*.txt` and `~/notes.txt` are the home directory. `~/code/api/src`
        # is a project.
        if len([part for part in tail.split("/") if part]) <= HOME_DEPTH:
            return True
    return False


REFUSAL = """Refused: this looks like a search for credentials outside the workspace.

A key that lives in another project was scoped to that project. Reusing it here
widens what it can reach, spreads it to new places, and makes rotating it later
break something nobody remembers deploying.

Get credentials in this order instead:
  1. a connected app — `connect_app` with action='list' to see what is available
  2. this workspace's own configuration, or a `secrets:` reference in the config
  3. ask the user, saying exactly what you need and what it is for

If the user has already told you to look in a specific file, read that file
directly rather than searching for what might be in it."""


def sweeps_for_credentials(command: str) -> bool:
    """Whether this command is hunting for secrets somewhere it should not be.

    Three conditions, all required, so ordinary work is untouched:

    * it is a search tool — `cat`ting a file you were pointed at is not a sweep;
    * it names something credential-shaped;
    * it ranges somewhere broad — a home directory, or a store whose whole
      content is credentials.

    `grep API_KEY .env` inside a checkout fails the third and runs, as does a
    search through a project that happens to live under `/Users/someone`.
    `security find-generic-password` fails the first, so it is caught on its
    own below.
    """
    text = command or ""
    if not text.strip():
        return False

    # The keychain readers are not searches and have no innocent reading in an
    # agent's hands. Caught on their own.
    if re.search(r"security\s+find-(generic|internet)-password", text, re.IGNORECASE):
        return True

    if not SEARCH_TOOLS.search(text):
        return False
    if not _searches_somewhere_broad(text):
        return False
    return bool(SECRET_WORDS.search(text) or VENDOR_WORDS.search(text))
