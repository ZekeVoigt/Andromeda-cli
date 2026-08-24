# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Until 1.0, a minor bump may change a command's shape. Anything that changes a
tool's name, arguments or risk tier is called out explicitly, because a model
that learned the old contract will keep using it.

## [0.3.1]

### Removed
- Nine bundled skills that were not being used: `calendar`, `canvas`, `email`,
  `github`, `shopify`, `slack`, `stripe`, `weather` and `browser/gmail`. The
  remaining set is `browser`, `computer-control` and `skill-creator`.

### Changed
- Two tests stopped pinning the bundled skill set. One asserted a count and one
  named `github` specifically, so curating the set broke tests that were not
  about the set at all. They now assert what they were for: that whatever ships
  parses in the repo's format, and that the bundled directory resolves.

## [0.3.0]

### Added
- **Full-text search across every past session**, at no model cost. A derived
  SQLite index at `~/.andromeda-cli/state.db` backs `andromeda sessions
  search`, the `/sessions` slash command, and a new `session_search` tool the
  agent can call — so "what did we decide about X" and "the thing I asked you
  last week" are questions it answers rather than guesses at. Three routes,
  chosen by the query: FTS5 by default, a trigram index for CJK (the default
  tokenizer splits CJK into single characters, so phrase matching against it
  does not work at all), and a substring scan where neither applies. A query
  that raises always falls back — reporting zero results for a grammar the
  sanitizer did not anticipate is indistinguishable from "there is nothing
  there".
- **The transcripts remain the source of truth.** The index is derived and can
  be deleted at any moment without losing a message, which is why
  `andromeda sessions recover --rebuild-index` is the blunt repair and it is
  always safe.
- `andromeda sessions recap [id]` — what happened in a session, computed from
  the transcript. No model call: a recap you wait for and pay for is a recap
  nobody runs, and it would invalidate the prompt cache the next real turn is
  about to use.
- `andromeda sessions export <id> --format html|markdown|jsonl|text`.
  Everything is escaped, including in Markdown — a transcript holds whatever
  anybody pasted into it, and an export opened in a browser is a local file
  with the privileges of any other. `--format jsonl` can export prompts alone,
  one per line, for piping into review tooling.
- `andromeda sessions doctor`, `reindex`, `recover` and `rm`. `recover`
  salvages a transcript a machine truncated mid-write: JSON is all-or-nothing
  to a parser, so one missing brace loses a conversation that is almost
  entirely intact on disk. It walks the message array keeping every complete
  object, and **deletes nothing** — the original moves to
  `sessions/quarantine/` and the salvage is written in its place. It is a dry
  run until `--apply`.
- Filters on every listing and search: `--since`, `--until`, `--workspace`,
  `--model`, `--provider`, `--role`. `--since` takes `7d`, `2h`, `yesterday`
  or `2026-08-01`; a value it cannot read is an error, never "no filter".
- `andromeda sessions active`, and a warning when you open a session another
  live terminal already holds. Two terminals on one transcript interleave
  their turns and the last save wins. A claim is released only when its owner
  is **proved** gone, by pid *and* process start time — pids get reused, and
  reaping on the pid alone hands a live session to a second terminal.
- **`/resume` switches sessions without restarting the terminal.** Only the
  transcript moves: the tools, the approval policy, the workspace and any
  running background processes stay exactly as they are. `/resume` alone lists
  the candidates, numbered as well as addressable by id. On both surfaces.
- **Profiles** — several independent installs, one program.
  `andromeda profile create|use|delete|list`, and `-p <name>` for a single
  command. The default profile is your home directory itself, so an existing
  install is already the default and there is nothing to migrate.
  `ANDROMEDA_HOME` still wins outright: it is how a container or a scheduled
  job is certain which state it is touching.
- **Compaction stops throwing work away.** When the context window fills and
  older turns are replaced by a summary, those turns are kept in the index and
  the summary says so — it names the session and the anchor to read them back
  with, so a detail the summary left out is a lookup rather than a guess. The
  pruned-tool-output placeholder says the same. The instruction the model
  *writes* the summary from deliberately does not mention it: telling it there
  is a safety net while it summarises produces a lazier summary.
  Compacted turns leave the transcript file too, so for those the index is the
  only remaining copy — rebuilding it never deletes them, `sessions show`
  prints them above the live transcript, and `sessions doctor` counts them
  separately.
- **The index is checked on startup, once a day.** A stale index is the one
  failure here nobody notices: search answers "nothing found", which reads
  exactly like the truth. A small backlog is caught up silently; a large one is
  reported in a line rather than blocking the first prompt. Deliberately not
  the whole of `sessions doctor` — parsing every transcript and running an
  integrity check are both O(everything).
- **`andromeda memory`** — `list`, `search`, `remember`, `forget`, `export`,
  `stats`. Standing memories are a premise the agent argues from, not a note,
  and until now the only way to remove a wrong one was to ask the agent to,
  which requires already knowing it is there. `forget` prints what it matched
  before doing anything, because matching is generous by design. `export` is
  the only way to read a sqlite-backed store as text.
- **A pluggable memory backend**, `memory_backend: json | sqlite`. Storage and
  candidate retrieval are the backend's; **scoring is not**. `minScore` means
  "this fraction of the query's meaningful terms appear in the memory" on both,
  because a backend that swapped in its own ranking would keep the parameter
  name while silently retuning every threshold set against it.

### Changed
- `andromeda doctor` reports the index, the memory backend and the profile in
  use. All three fail quietly otherwise — a search that finds nothing and a
  recall that returns nothing look exactly like "there was nothing there".
- `restore` rebuilds the search index from the restored transcripts. A restore
  that ends with "nothing found" reads as a restore that lost the sessions.
- `backup` and `export` carry memories on every backend. On `sqlite` they live
  in the index, which is deliberately not portable, so an export that quietly
  carried none is a thing you would discover on the other machine.

### Note for anyone reading the source
- The index's migrations are keyed **by name, never by number**. A numbered
  ledger breaks the day two branches both add a fourth migration, or somebody
  renumbers a shipped one — every existing install then either re-runs a
  migration or skips one.
- `session_search` anchors on a **row id, not a transcript offset**.
  Compaction restarts a session's offsets, so an offset handed out before one
  would silently address a different message afterwards.
- A new source-hygiene check catches a decorator separated from its function by
  an inserted method — a `@staticmethod` taking `self`, or a plain method
  taking none. It shipped that way once, and Python accepts it silently.

## [0.1.4]

### Changed
- The install command is back to `https://ai-andromeda.com/install.sh`, which
  now serves. It pointed at the repository while that URL was returning 404.

## [0.2.7]

### Changed
- **The figure is Leonardo's again, and it has a head.** It had neither: the
  crop that was supposed to hold "the figure" started at the shoulders, so the
  head was never in the picture, and the fix after that replaced the drawing
  with a constructed stick pose. Both were the wrong move. Leonardo's line work
  now supplies the body; only the small head region is reconstructed after
  reduction, with a centred silhouette and a few stable facial marks instead
  of the dense sideways-looking block the photograph produced in braille.
- The plate's square and circle are now measured rather than estimated: the
  four ruled lines by local contrast, the circle by a least-squares fit over
  arc points sampled outside the square. The drawn circle and square are
  reproduced from those ratios and the photographed figure is blitted into the
  drawn square at the scale it occupies on the plate. So the figure stands on
  the square's base with its fingertips on its sides, and the arcs of
  Leonardo's circle that cut the square's corners land on the drawn ring
  instead of beside it — where they used to read as blobs.
- An anatomical mask keeps the photographed ink around the torso, limbs,
  hands and feet, so the plate's background rulings no longer turn into bars
  across the empty quadrants. Solid masses are hollowed to outlines and lone
  vellum specks are dropped. The four foot positions are extended to the exact
  lower circle boundary, without stray registration dots or interior guide
  arcs competing with the figure. The startup motion remains a single reveal,
  and now runs in the full-screen TUI as well as the line-oriented interface.

## [0.2.6]

### Changed
- **The figure is drawn now, and it has a head.** In the source plate the head
  is about 35 pixels across, which at terminal size reduces to roughly a 7x9
  patch of braille dots — mush at any threshold, which is why three rounds of
  crop tuning produced a figure missing the one part everybody looks for. The
  pose is constructed instead, to proportions measured off the landing page's
  own render: head, both arm positions, both leg positions, torso with mass.

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
