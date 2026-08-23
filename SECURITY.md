# Security

## Reporting

Email **security@ai-andromeda.com**, or open a private advisory through
GitHub's *Report a vulnerability* on this repository. Please do not open a
public issue.

Include what you did, what happened, and what you expected. A proof of concept
helps but is not required to report something.

You will get an acknowledgement within three working days and an assessment
within ten. If a fix is warranted you will be told when it ships and credited
in the release notes unless you would rather not be.

## What this program is running on your machine

Worth stating plainly, because it changes what counts as a vulnerability.

Andromeda runs a language model in a loop with tools that read and write your
filesystem, run shell commands, drive a browser and reach the network. That is
the product, not a flaw in it. It is protected by a consent gate rather than by
a sandbox:

- Tools carry a **risk tier**, and anything above the session's ceiling stops
  and asks before it runs.
- A **non-interactive session is narrowed**, not left to ask — a piped run
  drops to read-only rather than refusing calls one at a time.
- **A child is never more permissive than its parent.** Delegated lanes and
  scheduled jobs cannot widen what they were given.
- **Learned approvals never widen themselves.** Repetition produces a
  suggestion; promotion is always an explicit keystroke.
- **The shell is not confined to the workspace.** The file tools are; the shell
  is not, and a test pins that so nobody mistakes it for a guarantee. If you
  need containment, run Andromeda in a container.

So "the agent ran a command that changed my files" is expected behaviour if it
was approved, and a bug worth reporting if it was not.

## What we consider a vulnerability

- Anything that runs a tool above the session's ceiling without consent, or
  that widens a ceiling, belt or disabled-tool list from inside a session.
- A scheduled job or delegated lane acquiring permissions its creator did not
  have.
- Prompt content — a web page, a file, a tool result — that causes a gated
  action to run unprompted. Injection that produces *text* is not this;
  injection that produces an *action* is.
- Credential exposure: a token in a log, a crash dump, an `export` archive, or
  a request to a host that should never have seen it.
- Path traversal out of the workspace by the file tools, or out of the scripts
  directory by a scheduled script.
- The private-network guard failing to hold on the browser or fetch tools.

## Out of scope

- The model producing wrong, offensive or made-up output.
- The shell not being confined — documented above and by design.
- Anything requiring an attacker who already has your user account or your
  unlocked machine.
- Denial of service by giving the agent an expensive task.
- Vulnerabilities in a pinned dependency with no path to exploit through this
  code. Report those upstream; tell us if you would like the pin moved.

## Supported versions

The latest release. Fixes land there and are published from upstream.
