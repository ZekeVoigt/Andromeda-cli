# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Until 1.0, a minor bump may change a command's shape. Anything that changes a
tool's name, arguments or risk tier is called out explicitly, because a model
that learned the old contract will keep using it.

## [0.1.0]

First release.

### The agent
- A conversation loop against a locked model, with a hosted relay as the
  default provider and BYOK alongside it.
- Two-stage context compaction — old tool results pruned first, summarisation
  only if that was not enough — starting at 75% of the window rather than at
  the wall.
- Thinking-level control (`off`/`low`/`medium`/`high`), sent only to a model
  that declares it accepts one.
- Vision through an auxiliary model reachable only by the tool that needs it,
  never selectable as the conversation model.

### Tools
- Files, search, patch, terminal, todo, memory, skills, web fetch and search,
  a browser (refs and structure, never pixels), background processes, MCP over
  stdio and streamable HTTP, and `clarify`.
- Delegation to narrowed lanes, three at a time, in the background by default,
  with graded success criteria and tool-call evidence in every report.

### Consent
- A risk-tiered approval gate: consent stated before creation, a child never
  more permissive than its parent, and a non-interactive session narrowed to
  read-only rather than left to refuse calls one at a time.
- Learned approvals bound to the tier they were learned at, and never widening
  themselves.

### Autonomy
- Scheduled jobs with cron expressions or `every 30m`, consent fixed at
  creation, monitor mode that skips the model entirely when a cheap source is
  unchanged, script jobs, a per-job notepad, an execution ledger, suggestions,
  blueprints and webhook delivery.
- `cron install` writes a launchd agent or a systemd user unit.

### Interfaces
- A REPL with live-rendered markdown, and a full-screen interface
  (`andromeda --tui`) sharing the same renderer.
- Sessions, `--resume`/`--continue`, checkpoints and `/rewind`.
- `backup`, `export` and `restore`, with the credential warning keyed on what
  actually went into the archive.

### Distribution
- `install.sh` and `install.ps1`, a transactional `andromeda update` that rolls
  back a failed dependency install, and `andromeda doctor`.
- Published from a standalone repository, so installing needs no access to the
  development tree. The installer and `update` accept both layouts — a flat
  checkout where the package is the tree, and a nested one where it is a
  directory of it — probing for `pyproject.toml` rather than assuming either.
- The skills that ship with an install now resolve from any working directory.
  They previously required the user to be standing inside the checkout, which
  meant a normal install had no skills at all.
