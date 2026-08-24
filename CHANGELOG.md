# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Until 1.0, a minor bump may change a command's shape. Anything that changes a
tool's name, arguments or risk tier is called out explicitly, because a model
that learned the old contract will keep using it.

## [0.1.4]

### Changed
- The install command is back to `https://ai-andromeda.com/install.sh`, which
  now serves. It pointed at the repository while that URL was returning 404.

## [0.2.5]

### Changed
- **The study is the whole composition now** — circle, square and figure, not
  just a fragment of the figure. The circle and square are *drawn* rather than
  extracted: the source is a photograph of aged vellum, Leonardo's circle is a
  faint line whose luminance overlaps the stained paper around it, and no
  threshold separates the two. The landing page does the same thing, drawing
  its circle and square as elements and using the photograph only for the
  figure.

## [0.2.4]

### Changed
- **The study is legible now.** It ships at two sizes and picks by terminal
  width: a detailed 96-column render where there is room, the compact one
  otherwise. At 58 columns the figure read as a suggestion of a body; at 96 the
  square's sides, the spread legs and the outstretched arms are all there. The
  crop also no longer clips the feet.

## [0.2.3]

### Fixed
- **The startup study now opens the full-screen interface too.** It was only in
  the REPL's banner, so anyone with `interface: tui` configured never saw it —
  the two surfaces share a renderer precisely so they cannot drift on how
  things look, and opening on different faces was the same drift by another
  route.

## [0.2.2]

### Fixed
- **The installer could only be run once.** `uv venv` refuses an existing
  directory and the installer did not pass `--clear`, so every re-run died at
  "Could not create the venv" — including the re-run its own failure message
  tells you to do.
- **`andromeda update` could never succeed.** The installer builds the venv with
  `uv venv`, which does not include pip, but `update` shelled out to
  `python -m pip` — so every update reset to the new revision, failed to
  install, rolled back, and correctly reported that the install still worked.
  At the old version, permanently. It now uses `uv` when it is present and
  falls back to pip for hand-built venvs.

## [0.2.0]

### Added
- **Onboarding.** `andromeda setup` — four screens, one decision each, all
  skippable, with a step counter and a capability summary that names the exact
  command to close each gap. It runs automatically at the end of the installer,
  reading from `/dev/tty` so it works inside `curl … | bash`.
- **`SOUL.md`** — standing instructions in your own words, read every session
  and never written to by the program. Ships fully commented out, so an
  untouched file costs nothing.
- **The startup study.** The landing page's Vitruvian figure, rendered in
  braille, with Andromeda's real sky coordinates and a scan line that sweeps it
  once on launch. Degrades to a wordmark on terminals that cannot draw it and
  disappears entirely when output is redirected.

### Changed
- The palette is the website's — near-monochrome zinc with a single restrained
  accent — instead of the default terminal cyan and magenta.

## [0.1.3]

### Fixed
- `--version` reports the released version. It was written in two places and
  drifted immediately, so a freshly installed CLI named a release two behind —
  and `doctor` prints it, so every bug report would have carried the wrong one.
  The packaging metadata is the single source now.

## [0.1.2]

### Fixed
- The install command in the README pointed at a hosted URL that is not serving
  yet. It now points at the repository, which resolves today. A README whose
  first command fails is the worst possible first impression.

## [0.1.1]

### Fixed
- The test suite no longer depends on terminal width. A wrapped rich line puts
  a newline inside a phrase, so a substring assertion failed on CI, where paths
  are longer, while passing on a wide local terminal.

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
