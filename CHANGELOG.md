# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

Until 1.0, a minor bump may change a command's shape. Anything that changes a
tool's name, arguments or risk tier is called out explicitly, because a model
that learned the old contract will keep using it.

## [0.5.0] — 2026-08-25

### Added
- **Secrets are removed from tool output before anything can read them back.**
  A tool result reaches the terminal, the transcript, the search index, an
  export and the model; the redaction runs once, at the tool, so all five see
  the same thing. Vendor-prefixed keys, JWTs, private keys, database
  connection strings and `Authorization:` headers are masked in any output;
  named assignments (`OPENAI_API_KEY=…`, `"apiKey": …`, `password: …`) are
  masked additionally on the two surfaces that are credential dumps rather
  than prose — `env`/`printenv` output and a read of `.env`, `.netrc`,
  `.pgpass` or `.envrc`, where *every* value is masked whatever its key is
  called. A redacted file read says how many values were removed and that they
  must not be copied onward; the mask it uses is syntactically impossible as a
  key, so it cannot be written back into a config as one. Credentials this
  install is holding — the device token, your BYOK key, an MCP token — are
  masked by exact match, which is the only thing that catches a credential
  with no recognisable shape. Prose is deliberately left alone: `Secretary: J.
  Smith` and `tokenizer: cl100k_base` are not credentials. Turn the pattern
  passes off with `ANDROMEDA_REDACT_SECRETS=0`; your own credentials stay
  masked regardless.
- **`andromeda mcp login <server>`** — OAuth for MCP servers that will not
  talk to an anonymous client, which is most hosted ones. Discovery, dynamic
  client registration, PKCE and token refresh, against the specifications
  rather than through an SDK. Add `"auth": "oauth"` to a server's entry in
  `mcp.json` and run the command; tokens are stored 0600 and refreshed before
  they expire. A tool call never opens a browser — an unauthorized server says
  which command to run. `andromeda mcp logout <server>` forgets the tokens,
  and `andromeda mcp` now says which OAuth servers are signed in.
- **`andromeda secrets`** — credentials can live in a vault instead of in a
  file. A `secrets:` block in `config.yaml` maps an environment-variable name
  to a reference — `op://` (1Password), `bw://` (Bitwarden Secrets Manager),
  `keychain://` (macOS), `cmd://` (any helper you configure) or `env://` —
  resolved into the environment at startup, so the BYOK lane, an MCP server, a
  hook and anything you run see the value without knowing a vault was
  involved. Something your shell already sets always wins. Nothing is cached
  to disk and nothing is ever installed: a missing helper is named with the
  command that installs it. A locked vault is a warning naming the unlock
  command, never a stopped session. `andromeda secrets` shows what resolves,
  `secrets get <NAME>` checks one (masked — there is no flag to unmask it),
  `secrets schemes` lists what this build can read from.

### Fixed
- A value resolved from a vault is masked in every tool result, transcript and
  export for the rest of the session, automatically.

## [0.4.0] — 2026-08-25

### Added
- **The coding posture** — a session started inside a codebase now gets an
  operating brief, a snapshot of the repository (branch, dirty state, recent
  commits, the manifest, the package manager and the project's own verify
  commands), and the project's own `AGENTS.md` / `CLAUDE.md` / `.cursorrules`,
  merged from the repository root down to the working directory. A notes
  folder is unaffected: a bare `git init` only counts once the directory
  actually holds code. Context files are scanned for prompt injection before
  they are loaded, because one arrives with every clone and goes straight into
  a prompt the user never sees.
- **Context files discovered on the way.** When the model first reads
  something under a directory nothing has looked at, that directory's
  `AGENTS.md` is appended to the tool's own result — so a package's own
  conventions arrive at the moment the model starts working in it, without
  rewriting the cached system prompt mid-turn.
- `coding_context` (`auto` | `on` | `off`) and `coding_instructions` — the
  posture's switch, and standing coding rules that belong to this install
  rather than to a checkout.

- **Language-server diagnostics after an edit.** `patch` and `write_file` now
  report what the change broke, using whichever language server the project
  already has. Only the problems the edit *introduced* are reported: the
  baseline is subtracted after being shifted onto the new line numbers, so
  inserting a line at the top of a file no longer reports the whole file as
  new. Errors only, by default.
- **Nothing is ever installed for you.** A language server that is not on this
  machine is named, with the command that would install it, and the edit
  proceeds without diagnostics. `andromeda lsp status` says which servers
  apply to the project you are in and which are missing; `andromeda lsp
  servers` lists every one the harness knows.
- `lsp` and `lsp_severities` — the switch, and which severities are reported.

- **`/usage`**, in both interactive surfaces — what this session and this week
  have spent, in tokens. The question `/credits` could not answer: a balance is
  an account-level figure, it lags a turn, and on the BYOK lane it does not
  exist at all.
- **`andromeda status`** — one screen: the model, the lane, the approval mode,
  whether this machine is signed in, what has been spent in the last seven
  days, and what this directory means for the next session started in it. It
  makes no network call.
- **Token accounting.** Every response's usage is recorded from the provider's
  own reply and kept on the session transcript, so `andromeda status` can say
  what a week cost in tokens. There is no price table and there never will be
  one: a local rate that has drifted produces a cost figure somebody plans
  against.

### Fixed
- **A rate limit no longer ends the turn.** A 429 or a transient 5xx is retried
  with jittered backoff, honouring the provider's own `Retry-After` when it
  sends one, and the terminal says why it went quiet instead of appearing to
  hang. Nothing is retried once output has started — a terminal cannot
  unprint, and two half-answers stitched together is worse than one honest
  failure.
- **An answer that dies mid-stream is kept.** Whatever arrived before the
  failure stays in the transcript rather than being discarded, so a reply that
  died at ninety per cent is worth ninety per cent.
- **An empty response is asked again once, and only once.** The same emptiness
  twice from the same model will not become an answer on a third attempt, and
  each attempt re-sends the whole conversation at full input cost.
- **A model that spends its whole output budget repeating itself is stopped**
  rather than asked to continue, which would have bought more of the same text
  at full price.
- **The system prompt no longer describes tools the session cannot call.** A
  non-interactive run is narrowed to `safe_local` by default, which denies both
  edit tools — and it was still being told how to choose between `patch` and
  `write_file`. The brief and the prompt are both tailored to what is actually
  offered, including inside a delegated lane.
- **Language servers now shut down cleanly.** The farewell was written through
  a handle that had already been cleared, so every server was killed after the
  full timeout instead of exiting.
- **`/credits` no longer reads as frozen while you are spending.** A `$0.10`
  grant with three hundredths of a cent spent rendered as "$0.10 of $0.10",
  because the two figures were rounded to the cent independently — at that
  scale a whole turn is invisible. Decimal places are now added only when the
  cent would hide the difference, so an untouched window still says "$0.10 of
  $0.10" and a spent one does not.
- **`/credits` says that its figure is from your previous turn.** The relay
  stamps the balance headers from the reservation it takes *before* answering
  and settles the charge when the reply ends, so the number has always been one
  turn behind. It was never said out loud, which is most of why it looked
  stuck.

### Changed
- TUI now matches the landing page: monochrome, unboxed, with CLI changes
  beside Leonardo and bracketed responses.
- Conversation growth now moves Leonardo and the release notes upward through
  one shared scroll flow while the composer remains fixed.

### Added
- **Hooks** — shell scripts run at seventeen lifecycle boundaries, configured
  in the `hooks:` block of `config.yaml`. A `pre_tool_call` hook can block a
  call, rewrite its arguments, or send it to the approval prompt the policy
  would have skipped; the rest observe or transform. Both the canonical and
  the Claude-Code/Cursor spellings of every directive are accepted, and exit
  code 2 blocks with no JSON at all, so a script written for another harness
  works unchanged.
- `andromeda hooks list | test | revoke | doctor`. `test` and `doctor` fire
  through the same code path a live session uses, so a script that passes
  there behaves the same in a real turn.
- `--accept-hooks`, `ANDROMEDA_ACCEPT_HOOKS`, and `hooks_auto_accept` — the
  three ways to register hooks where there is nobody to answer a prompt.
  Without one of them a non-interactive run registers nothing.
- `ANDROMEDA_SAFE_MODE=1` now also skips hook registration, so a
  troubleshooting run executes none of your customizations.

- **A `builder` lane** — the first specialist that changes anything. Every belt
  until now was read-only, so delegation could not be used for the work people
  most want to delegate. It reads and writes files; no shell and no network,
  because a shell can `cd` out of any confinement it is given.
- **`worktree_isolation`** — one git worktree per lane, branched from `HEAD`,
  with the lane's tools bound to it. Lanes that write then run in parallel
  without editing underneath each other, and the parent is told the branch,
  the commit count and whether the tree is dirty. A copy holding nothing is
  removed; a copy whose state git would not confirm is kept and reported as
  unproven, because "unknown" is not "clean".
- `andromeda worktrees list | prune [--dry-run]` — the attended pass over what
  the lanes left behind. It keeps anything with tracked edits, unique commits,
  or untracked files, and removes a tree before its branch.

- **Skills are scanned before they are offered to the model** — 119 patterns
  over exfiltration, prompt injection, destructive commands, persistence,
  reverse shells, obfuscation, privilege escalation, supply chain, mining and
  leaked credentials, plus structural checks and invisible-character
  detection. Trust comes from where a skill lives, because that is the only
  provenance a harness with no registry has: shipped-with-the-install is
  never scanned, your own `~/.andromeda-cli/skills` may carry a `caution`
  verdict, and a skill found in a workspace must come back `safe`.
- `andromeda skills list | scan | trust | untrust`. `trust` is recorded
  against the skill's content hash, so editing a trusted skill withdraws the
  decision.

- **`andromeda batch <file>`** — one prompt over every row of a JSONL (or a
  plain list), each row in its own conversation, with answers appended to a
  results file as they land and `--resume` skipping what is already recorded.
  A failing row is recorded and the batch continues.
- **`andromeda eval --repeat N`** — a pass rate instead of a verdict, with
  scenarios that pass intermittently reported as flaky. `--jobs N` runs them
  at once, results keep scenario order regardless of finishing order.
- **`andromeda eval report`** — what broke, what was fixed, and what got less
  reliable since the previous run, with the model change called out first when
  there was one. Every run is saved under `~/.andromeda-cli/eval-runs/`.
- Three eval checks: `tools_in_order` (a subsequence, not an exact list),
  `steps_under`, and `file_matches`.
- **`andromeda pause` / `andromeda resume`** — a resumable hold on scheduled
  work. New jobs only: work already running is never killed, and interactive
  sessions are untouched. A sentinel file, so anything can set it; an
  unreadable one counts as paused, because failing open would lift an
  emergency stop exactly when the filesystem is misbehaving. Surfaced in
  `doctor` and at the top of a session.
- `andromeda approvals test <tool>` — the real gate's verdict for a tool plus
  the rule that produced it, executing nothing and persisting nothing.
  Script-friendly exit codes (0 allow, 2 ask, 3 deny).
- `andromeda approvals suggest [--apply N,M]` — tools approved often enough to
  stop being asked about. Proposals only; destructive and irreversible tools
  are never proposed, and a tool withheld for that reason is named.
- **`andromeda acp`** — this agent inside an editor, over the Agent Client
  Protocol. Streams the answer, each tool call and the todo plan as the turn
  runs, and asks the approval gate through the editor rather than answering on
  the user's behalf: a cancelled dialog is a refusal. Written against the wire
  protocol, so it adds no dependency.
- **A curator for the skill library.** Every `skill_load` is now recorded, and
  agent-written skills in your own skills directory move between active, stale
  (30 days) and archived (90) on their own. Archive is a move, never a delete —
  `andromeda curator restore <name>` brings one back — pinned skills are never
  touched, and a never-used skill gets a grace period rather than being read as
  neglected. The first sight of an install stamps the clock and waits an
  interval instead of sweeping a library it has no history for.
- `andromeda curator status | sweep | review | pin | unpin | restore | pause |
  resume`. `review` asks a model what it would change about what skills *say*
  and writes proposals; it applies nothing, because an agent may propose and
  only a person grants.
- `andromeda completion bash | zsh | fish` — generated by walking the live
  argument parser, so the verb list can never drift from the program.
- **MCP tools are no longer listed on every request.** `tool_search`,
  `tool_describe` and `tool_call` stand in for the whole connected catalogue,
  with a listing of what is deferred embedded in the search tool so the model
  still knows what exists — full descriptions, then names only, then a count
  per server, whichever fits the budget. Built-in tools never defer. A call
  through the bridge meets the same policy, prompt and hooks as a direct one,
  and a call missing required arguments gets the schema back rather than an
  opaque failure from inside the tool. `tool_search: off` restores the old
  behaviour.

### Changed
- **Skill discovery is layered rather than first-match.** The bundled skills,
  your own, and a workspace's now stack, with the nearest winning a name
  collision. Before this, one `skills/` directory in a repository hid your
  entire personal library — including anything the agent had written for
  itself, which made the curator's subject invisible.
- **A skill the scan withholds is no longer offered to the model, and its
  instructions never enter the prompt.** It is not silently missing either:
  `/skills` names it with the finding that withheld it. If you have a
  workspace skill that trips a `high` pattern, `andromeda skills scan <name>`
  shows the line and `skills trust <name>` keeps using it.
- With `worktree_isolation` off, two tree-writing lanes take the working
  directory in turn — the same exclusive-surface rule the browser has. The
  lane registry's browser lock is now one lock per named surface.
- The approval prompt shows *why* a call it would have allowed is being asked
  about, when a hook escalated it. Both surfaces.
- `transform_tool_result` rewrites what the model reads, not what the terminal
  shows: the surface keeps printing the tool's own output.

### Fixed
- `andromeda hooks revoke <command>` would have dispatched as though `<command>`
  were the verb — the subparser's positional shadowed the top-level `command`
  destination. Caught by its own test before it shipped.

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
