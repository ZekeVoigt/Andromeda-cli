# Andromeda CLI

A local-first agent harness for the terminal. The loop runs on your machine.

```bash
andromeda                            # REPL
andromeda "what is 2+2"              # one turn, then exit
git log -5 | andromeda "summarise"   # stdin is folded into the prompt
```

## Install

```bash
curl -fsSL https://ai-andromeda.com/install.sh | bash        # macOS, Linux, WSL
iex (irm https://ai-andromeda.com/install.ps1)               # Windows
```

Clones to `~/.andromeda-cli/checkout/`, builds a `uv` venv, and links
`~/.local/bin/andromeda`.
There is no wheel and no Homebrew formula on purpose — the CLI resolves bundled
assets from its checkout, so an installed-package build would be missing half of
itself.

```bash
andromeda update            # pull and reinstall
andromeda update --check    # what is available, change nothing
andromeda doctor            # what is and is not working
```

`update` is transactional: the revision is recorded before anything moves, and
if the dependency install fails the checkout is reset back. A failed update
leaves the install working rather than leaving new code against old packages —
which fails as an ImportError before the CLI can print why. It refuses outright
on a dirty checkout, because resetting over someone's edits is data loss, not an
update.

From a checkout:

```bash
cd cli
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/andromeda
```

## Two lanes

**`relay`** (default) — the hosted endpoint. It holds the provider key, enforces
the model allowlist, and reserves and settles credit per call. The CLI never
sees a provider key and never touches the ledger.

```bash
andromeda auth login            # signs in through your browser
andromeda auth login <code>     # or a code, for a machine with no browser
andromeda auth status
```

Signing in opens ai-andromeda.com, and the website redirects the pairing code
straight back to a loopback listener the CLI opened first — nothing is typed on
either side. Pairing mints a device token, stored `0600` in `~/.andromeda-cli/credentials.json`.
A terminal has no Clerk client and so cannot hold a session JWT; this is the
same credential the gateway daemon uses.

**`direct`** — BYOK against any OpenAI-compatible base. Your key, your bill, no
account needed.

```bash
export OPENROUTER_API_KEY=...
andromeda config set provider direct
```

## Tools

```bash
andromeda tools                     # what is on, and how each is gated
andromeda tools enable|disable <name>
```

| Tool | Tier | Default in `ask` mode |
|---|---|---|
| `read_file` `list_dir` `search_files` `todo` | `safe_local` | runs |
| `skill_load` `memory_search` `memory_store` `memory_forget` | `safe_local` | runs |
| `web_fetch` `web_search` `delegate` | `safe_local` | runs |
| `write_file` `patch` `terminal` | `destructive` | asks first |
| `browser_*` | `destructive` | asks first |

`skill_load`, the `memory_*` family and the `web_*` pair share their names,
schemas and tiers with
the desktop runtime's registry, and a test fails the build if they drift apart —
a tool that takes different arguments or is gated differently depending on which
surface you are on is a bug the user experiences as the agent being unreliable.

**Approval.** `ask` (default) stops before anything that changes the machine and
shows you what will happen — for `terminal` that is the command verbatim, never a
paraphrase. Answer `y` once, `a` for the rest of the session, or `n`. An
interrupted prompt counts as `n`; walking away from the keyboard is not consent.

`--approval auto` does not ask. `--approval deny` refuses every tool.

**Non-interactive runs are narrowed, not left to fail.** In a pipe there is
nobody to answer a prompt, so gated tools are not offered at all and the model
plans around what it actually has. `--approval auto` is the explicit way to hand
a script more.

## Where the agent can reach

The file tools are confined to the workspace root — the current directory, or
`--workspace <dir>`. Paths outside it are not reachable, not "reachable with
approval": symlinks are resolved before the check, so a link out of the root does
not work either.

**The shell is not confined.** `terminal` runs real shell commands, and
`cat ~/.ssh/id_rsa` is one of them. There is no blocklist, because a shell that
cannot be escaped is not a shell and any such list loses to `$(printf ...)`. What
contains it is the gate: `terminal` is `destructive`, so `ask` mode shows you the
command first and a non-interactive run never gets it. `--approval auto` is
granting the model your shell.

## MCP servers

One integration, and the CLI gains whatever anyone has published a server for.

```bash
andromeda mcp             # configured servers and their tools
andromeda mcp example     # a starter mcp.json
```

Config lives in `~/.andromeda-cli/mcp.json` and accepts both the `mcpServers`
key every other client writes and the `mcp_servers` snake_case variant, so a
config can be copied in from either spelling without editing. stdio and
streamable HTTP transports.

Tools arrive as `mcp__<server>__<tool>`, so their origin is legible and two
servers exposing `search` do not collide.

**Every MCP tool is `outbound`** — third-party code, configured by you, reaching
somewhere this harness knows nothing about. Tiering by what a server *claims* its
tool does would take the word of the thing being gated. So `ask` mode stops for
you, and a pipe never sees them.

A server that fails to start is recorded and reported, never fatal: the others
still work, and its stderr is in `andromeda mcp`.

### Tools the model finds rather than tools it is handed

Every tool in the array is paid for on every request, used or not. Thirty
built-in tools is worth it. Connect three MCP servers and it is four hundred
tools, most of which this session will never call — and that bill arrives on
every turn of every conversation.

So MCP tools are not listed. Three bridge tools stand in for all of them:
`tool_search` finds a capability by description, `tool_describe` loads one
tool's parameters, `tool_call` invokes it. **Built-in tools never defer** —
hiding those would make the agent slower at everything it does most.

The bridge carries a listing of what is deferred, because a bridge without one
produces a model that does not know what it does not know, and says a
capability is unavailable rather than searching for it. How dense that listing
is depends on what fits: a name and a line each, then names only, then a count
per server for a catalogue whose names alone would not fit. It degrades rather
than truncating — half a catalogue looks like a whole one.

A call through the bridge takes the ordinary path: same policy, same approval
prompt, same hooks. It changes what the model can see, never what it may do.
Calling a deferred tool without its required arguments hands back the schema
instead of a failure from inside the tool, which is the difference between one
round trip and a loop.

```yaml
tool_search: auto                  # auto | on | off
tool_search_listing_tokens: 4000   # 0 never embeds a listing
```

## Background processes

`terminal` blocks and kills its process tree on timeout, which is right for
`wc -l` and useless for `npm run dev`.

```
terminal(command="npm run dev", background=true)   →  proc_4dae56ca
process(action="poll", session_id="proc_4dae")     →  what is new since last poll
```

One `process` tool with an action enum — `list`, `poll`, `log`, `wait`, `kill`,
`write`, `submit`, `close` — rather than eight near-identical tools, which would
be eight chances to pick the wrong one. Ids accept any unambiguous prefix.

Output is drained by a reader thread from the moment it starts. Reading lazily
on `poll` looks simpler and deadlocks: a child that fills the 64KB pipe buffer
blocks on write forever while nobody reads. `/ps` lists them, and leaving the
REPL kills them — a session should not end with a dev server holding a port.

## Skills

Read from the existing `skills/` directory — `skills/<name>/SKILL.md`, the same
format the desktop app uses. Three layers, and they **stack**: what shipped
with the install, then your own `~/.andromeda-cli/skills`, then the first
`skills/` found walking up from the workspace. The nearest layer wins a name
collision and everything else is still there — a project that ships one skill
does not hide your library. `ANDROMEDA_BUNDLED_SKILLS_DIR` replaces the lot.

Only names and one-line descriptions go into the prompt; bodies are loaded on
demand with `skill_load`. A skill whose required binaries are missing is marked
unavailable, and loading it says so before the instructions — otherwise the
agent follows steps that cannot work and reports a failure you have to decode.

`/skills` lists them in the REPL.

### The library keeps itself honest

A skill library grows and never shrinks. The agent writes one for a job it does
once, the job never comes back, and a year later the manifest in every prompt
lists forty skills of which six are ever loaded.

Every `skill_load` is recorded — when, and how often. From that, three states:
**active**, **stale** (untouched for 30 days; still listed and still loadable),
**archived** (untouched for 90; moved aside, not offered, not deleted).

Two passes, and the split is the point.

**The sweep** is arithmetic — dates against thresholds. It runs by itself when
a session opens and a week has gone by, and it says so when anything moved.
Four rules hold it up: only skills **the agent wrote**, only in your own
`~/.andromeda-cli/skills`; a **pinned** skill is never touched; **nothing is
ever deleted** — archive is a move, and `restore` is a move back; and a skill
that has **never been used** gets a grace period, because that is absence of
evidence, not evidence of staleness.

**The review** reads the skills and proposes changes to what they *say* —
two that should be one, a description that never triggers a load, instructions
naming a tool that is gone. It costs a model call and it edits work you may
care about, so it applies nothing: it writes proposals and you decide. An agent
may propose; only a person grants.

```bash
andromeda curator status              # what is tracked, and its state
andromeda curator sweep --dry-run
andromeda curator review              # proposals, applied by nobody but you
andromeda curator pin <name>          # never sweep this one
andromeda curator restore <name>
andromeda curator pause | resume
```

The first sight of an install never sweeps — it stamps the clock and waits one
interval, because a library that predates this feature has no history and an
immediate pass would read every skill as untouched since the epoch.

### Skills are scanned before they are offered

A skill is instructions that go into the model's context and a directory of
files it may open. The realistic attack on this harness is not a malicious
registry — there is no registry. It is a `skills/` directory that arrived with
a repository somebody cloned. Nobody reads those; the agent reads them every
session.

So where a skill lives decides how much it has to prove:

| Where | Trust | Allowed |
|---|---|---|
| shipped with this install | `builtin` | everything, unscanned |
| `~/.andromeda-cli/skills` | `trusted` | anything but a `dangerous` verdict |
| a workspace | `community` | only a `safe` verdict |

The scan is regex over the text, structural checks (binaries, escaping
symlinks, size), and a hunt for invisible characters — the ones that make what
you read and what the model reads two different texts. A `critical` finding
makes a skill **dangerous**, a `high` makes it **caution**, and medium/low
never block anything on their own: a scanner that blocks on `subprocess.run`
is a scanner people switch off.

A withheld skill is never silently missing. `/skills` names it and says why.

```bash
andromeda skills list                # every skill, with its verdict
andromeda skills scan <name>         # the actual lines that caused it
andromeda skills trust <name>        # use it anyway
andromeda skills untrust <name>
```

`trust` records your decision against the skill's **content hash**, so editing
the skill puts it back behind the gate — what you accepted was the text you
read, not the name it goes by. A skill can ship a `.skillignore` to keep
development leftovers out of the scan; it can never exclude its own `SKILL.md`.

## Memory

`memory_store`, `memory_search` and `memory_forget`, backed by
`~/.andromeda-cli/memory/`. `standing` memories load into every prompt and are
capped; `episode` memories are recalled by search. Restating a known fact
consolidates rather than duplicating.

**One divergence from the desktop runtime, stated rather than hidden:** recall
here is *lexical* (term overlap), not semantic. `minScore` is the same range but
not the same meaning — a paraphrase the desktop side would recall may score zero
here. That is the cost of not shipping an embedding model with a terminal client.

Storage is pluggable, scoring is not:

```bash
andromeda config set memory_backend json    # one readable file (default)
andromeda config set memory_backend sqlite  # rows in the state index, FTS recall
```

`sqlite` only starts to matter past a few thousand memories: it asks the index
for candidates instead of reading every memory. It then scores those candidates
with **the same function** the `json` backend uses, so `minScore` means exactly
the same thing on both — a backend that swapped in its own ranking would keep
the parameter name while silently retuning every threshold set against it.

An unrecognised backend name falls back to `json` and says so, for the same
reason an unrecognised `cron_provider` falls back: a typo in a setting must not
take away the agent's memory.

You can read and edit the store directly, without spending a turn on it:

```bash
andromeda memory                       # everything; ★ = loaded every prompt
andromeda memory --scope standing
andromeda memory search "retry budget" # exactly what the agent would recall
andromeda memory remember "..." --standing --tags a,b
andromeda memory forget "..." --force  # shows what would go first
andromeda memory export [file.json]    # readable text on either backend
andromeda memory stats
```

This exists because standing memories are a **premise**, not a note: a wrong
one is something the agent argues from until somebody removes it, and until
there was a command for it the only way to remove one was to ask the agent —
which requires already knowing it is there. `forget` prints what it matched
before doing anything, because matching is generous by design and a count
alone does not tell you it caught the right ones.

## The web

`web_fetch` reduces a page to text with the standard library — no parsing
dependency. It refuses private, loopback and link-local addresses, **and
re-checks after redirects**, so a public URL that 302s to `169.254.169.254`
does not walk through the guard.

`web_search` needs a provider and is registered only when one is configured
(`BRAVE_SEARCH_API_KEY` or `TAVILY_API_KEY`). Without a key the tool does not
exist, rather than existing and always answering "not configured".

## The browser

```bash
andromeda browser install     # Playwright + Chromium, on demand
andromeda browser status
```

Lazy by design: until it is installed the `browser_*` tools are not registered,
so the model is never offered a browser it cannot open.

**Refs, never pixels.** Every read of a page is a structured outline of its
interactive elements, each with a short ref; every action names one:

```
Example Domain — https://example.com/

# Example Domain

[e1] link "Learn more" (https://iana.org/domains/example)
```

There is no screenshot tool and there will not be one. Reasoning about a page
from an image is slower, costs more, and is wrong in ways that are invisible — a
model that "sees" a button at the wrong coordinates clicks whatever is actually
there.

A field's label and its current value are reported separately, because a model
that cannot see what it typed types it again. Passwords are never shown.

Every `browser_*` tool is `destructive`, including the reads. Not because
reading a page changes this machine — it does not — but because a browser
carries signed-in sessions, and "click e7" is how an agent sends an email or
files an order. The tier describes the surface, not the keystroke.

Same private-network guard as `web_fetch`. To work against a local dev server,
`andromeda config set allow_private_network true` — a session setting, never a
tool argument, because a guard the model can switch off is not a guard.

## Delegation

`delegate` hands one self-contained piece of work to a narrowed helper and waits
for its report.

Lanes run **in the background by default**, at most three at once
(`MAX_CONCURRENT_LANES`). `delegate` returns a lane id immediately so the
next one starts alongside it; `subagents_wait` collects the reports. Three
40-second lanes cost 40 seconds, not two minutes.

`subagents_list`, `subagents_status` and `subagents_wait` take the hosted
registry's names *and* schemas exactly — unlike `delegate`, those three
contracts are ones this harness can honour in full.

Staleness is measured against **progress**, not start time: 450 seconds idle
outside a tool, 1,200 inside one, because a tool can legitimately be slow and a
model turn cannot. A lane fifteen minutes into honest work is not stalled.

The browser is held exclusively for the life of a browser lane, and the worker
slot is always taken *before* the surface — the other order deadlocks the moment
all three slots hold lanes waiting for the browser.

| Lane | Holds | Steps |
|---|---|---|
| `scout` | reads and the web; changes nothing | 12 |
| `builder` | reads **and writes files**. No shell, no network | 16 |
| `browser` | the browser, plus reads. One at a time | 20 |
| `writer` | local reads only — no network at all | 10 |
| `verifier` | reads, and cannot store what it concludes | 12 |

### The Builder, and one working copy each

`builder` is the lane that changes something. It reads and writes files and
does nothing else — no shell and no network, deliberately: a shell can `cd`
anywhere, and a lane holding one has confinement as a suggestion rather than a
boundary. What it may touch is a closed list, so a tool added later is denied
until somebody decides otherwise.

Two lanes writing the same directory at the same time is the failure that has
no symptom — the second one reads a file half-way through the first one's
change and reports success against something that is already gone. So:

```yaml
worktree_isolation: true
```

Each lane then gets its own git worktree at `<repo>/.worktrees/lane-<id>` on
branch `andromeda/lane-<id>`, branched from your current `HEAD`, and its tools
are bound to that directory — the confinement check does the enforcing, not a
sentence in its brief. Builders then run genuinely in parallel. **With the
setting off, two builders take the working tree in turn**, the same way two
browser lanes take the browser.

Outside a git repository the setting is ignored and lanes share the directory
exactly as before: a half-applied isolation is worse than none.

The lane commits to its own branch and its report names the branch, the commit
count and whether the tree is dirty — enough to merge it or go and read it. A
copy holding **nothing** (no commits, clean tree) is removed automatically;
anything holding work is kept.

**Pruning requires proof.** If a git probe fails, the state is unknown, and
unknown is not "clean": the copy is kept and the report says the numbers are
unproven. The parent only ever sees that report, and a default of "0 commits,
clean" reads as "the lane did nothing" for a tree that may hold an afternoon.

```bash
andromeda worktrees list          # what the lanes left, and what can go
andromeda worktrees prune         # remove the ones holding nothing
andromeda worktrees prune --dry-run
```

`prune` keeps anything with uncommitted edits to tracked files, with commits
that exist nowhere else, or with untracked files — nobody else has those — and
it keeps anything git would not answer a question about. It removes the tree
before the branch, always: the other order orphans commits that were reachable
a moment earlier.

The browser is a single-occupancy surface, so only the browser lane may touch
it. Two lanes driving one browser is worse than two in one mailbox, because
neither can see that it is happening.

The belts are ported from the desktop runtime's specialists, including the
property that makes them mean anything: **a belt is a hard denial, read before
anything else.** A lane runs without prompting, so a tool the belt rejects must
come back `denied` and not `needs_approval` — otherwise a Writer that "cannot
send" is really a Writer that sends after a pause.

A lane's policy is derived with `Policy.narrow()`, so it can only ever hold a
subset of what your session holds — an `allowedTools` naming something you lack
intersects away to nothing rather than granting it. No lane can delegate
further; depth stops at one, guarded three times over.

Each report carries evidence read off the lane's transcript, not its prose:

```
[scout · 3 steps · called read_file×2, list_dir]
```

That line exists because a lane with no shell will write `<shell>…</shell>` into
its answer, and a parent reading that as work is a parent reporting something
that never happened.

`/lanes` lists them in the REPL.

## Long conversations

The transcript is compacted before it hits the window, in two stages, in this
order because they cost very differently:

1. **Micro-compact.** The content of old tool results is replaced with a
   placeholder, keeping the two most recent intact. Free, instant, and usually
   enough: a transcript's weight is nearly always old file reads.
2. **Summarise.** If pruning was not enough, the older part of the conversation
   is summarised and replaced. Costs a model call, so it is only reached when
   it has to be.

The constants: a summary budget of 20% of the window, floor 2,000 tokens,
ceiling 10,000, and compaction starting at 75% — well before the wall, because
the summarisation call needs room to run.

The invariant that keeps it safe: an assistant message carrying `tool_calls`
and the `tool` messages answering them are **one unit**. Splitting them produces
a request the API rejects outright. Every operation moves whole units.

A gauge appears in the prompt once you are past a third of the window.

## Sessions

Every exchange is saved to `~/.andromeda-cli/sessions/` as it completes, not at
exit — the session that most needs recovering is the one that ended in a crash.

```bash
andromeda sessions                  # recent, newest first
andromeda sessions show <id>        # transcript; id prefixes work like git
andromeda sessions show <id> --live-only   # skip what it compacted away
andromeda sessions search <text>    # full-text, across every session
andromeda sessions recap [id]       # what happened, without asking the model
andromeda sessions export <id> --format html|markdown|jsonl|text
andromeda sessions rm <id> --force
andromeda --resume <id>             # pick one back up
andromeda --continue                # pick up the most recent
```

Every listing and search takes the same filters:

```bash
andromeda sessions --since 7d --workspace ~/code
andromeda sessions search "retry budget" --role user --since yesterday
```

`--since` accepts `7d`, `2h`, `yesterday` or `2026-08-01`. A value it cannot
read is an error, never "no filter" — silently searching a wider range than you
asked for looks like a successful search.

Resuming replays the transcript verbatim, including its original system message
— rewriting it would change the rules the earlier turns were produced under.
Inside a session, `/resume` switches which transcript this terminal is writing
to without restarting it: only the transcript moves, so the tools, the approval
policy, the workspace and any running background processes stay as they are.
`/sessions <text>` searches from the prompt and `/recap` says what has happened
so far.

### Searching across sessions

Search reads an index at `~/.andromeda-cli/state.db`, so "what did we decide
about the retry budget in March" costs one SQLite query and no model tokens.

**The transcripts stay the source of truth.** The index is derived from them
and can be deleted at any moment without losing a message — which is why
repairing it is a rebuild rather than a salvage operation:

```bash
andromeda sessions doctor           # readable? indexed? which search route?
andromeda sessions reindex          # catch up after transcripts moved
andromeda sessions recover          # salvage a truncated transcript (dry run)
andromeda sessions recover --apply
andromeda sessions recover --rebuild-index
andromeda sessions active           # sessions open in other terminals
```

A stale index is the one failure here a person cannot notice — search answers
"nothing found", which reads exactly like the truth. So that single check runs
by itself, once a day and on the first launch after an upgrade, and catches up
a small backlog silently. It is deliberately not the whole of `sessions doctor`:
parsing every transcript and running an integrity check are both O(everything),
and a startup path that slows down the longer you use the program is one people
work around.

`recover` exists for the case flat files really hit: a machine that lost power
mid-write leaves JSON that one missing brace makes unreadable to a parser, even
though every earlier message is intact. It walks the message array and keeps
every complete object. Nothing is deleted — the original moves to
`sessions/quarantine/` and the salvage is written in its place.

The agent has the same search as a tool, `session_search`, so "the thing I
asked you last week" is a question it can answer instead of guess at.

### Compaction is not deletion

When a conversation fills the context window it is compacted — old tool output
is blanked, and if that is not enough, older turns are replaced by a summary.
**Those turns stay in the index.** The placeholder and the summary both say so,
and the summary names the session and tells the model how to read them back, so
a detail the summary left out is a lookup rather than a guess:

```
[CONTEXT SUMMARY — earlier turns, compacted]
…
The 14 turn(s) this replaced are not lost — they are still in this session's
searchable history. `session_search(query="…")` finds them, and
`session_search(session_id="a1b2c3", anchor=N)` reads any of them in context.
```

Compacted turns leave the transcript file too, so for those the index is the
only copy left — which is why rebuilding it never deletes them, and why
`sessions show` prints them above the live transcript instead of hiding them.
`sessions doctor` counts them separately for the same reason.

The instruction the model writes the summary *from* deliberately does not
mention any of this: telling it there is a safety net while it is summarising
produces a lazier summary. The note is for whoever reads it later.

Two terminals resuming one session interleave their turns and the last save
wins. `sessions active` shows what is open, and opening a session another live
terminal holds says so — a warning, not a refusal, because a registry is not
entitled to overrule someone who asked for a session by id. A claim is released
only when its owner is *proved* gone, by process id **and** process start time;
pids get reused, and reaping on the pid alone is how the second terminal takes
over a session that is still being typed into.

## Profiles

Several independent installs, one program. A profile is a whole home of its
own — config, credentials, sessions, memories, skills, jobs and index — and
nothing crosses between them.

```bash
andromeda profile                       # every profile, current one marked
andromeda profile create work [--clone] # --clone copies settings, never the token
andromeda profile use work              # make it the default
andromeda -p work sessions              # or use one for a single command
andromeda profile delete work --force
```

**The default profile is your home directory itself**, not a directory called
`default` inside it, so an existing install is already the default profile and
there is nothing to migrate. `ANDROMEDA_HOME` still wins outright when it is
set — it is how a container or a scheduled job is certain which state it is
touching, and a sticky profile quietly redirecting it would make that guarantee
false.

## Output

Answers are rendered as markdown in the terminal — headings, **bold**, real
tables, syntax-highlighted code — streaming into a live region so structure
forms as it arrives rather than reflowing at the end.

Numbers worth comparing get a chart. The model emits:

````
```chart
registry.py: 642
browser.py: 459
web.py: 276
```
````

and the terminal draws horizontal bars in eighth-block characters, so values
within a few percent of each other still look different — whole blocks alone
make everything in the same 1/32nd of the range render identically, and a chart
where different numbers look the same is worse than a list.

**A tty is not a pipe.** Redirected output is plain text with no escape codes,
so `andromeda "..." > notes.md` gives you markdown rather than a screenshot of
markdown.

## The full-screen interface

```bash
andromeda --tui                    # this session
andromeda config set interface tui # from now on
andromeda --no-tui                 # back to the REPL for one run
```

A transcript that scrolls on its own, an activity lane that shows what is
running right now without pushing the answer off the screen, a status line with
the model, the approval mode and the context gauge, and prompts that take over
the screen instead of being printed into a stream that is still moving.

| Key | |
|---|---|
| `enter` | send — or queue, if a turn is already running |
| `ctrl+c` | interrupt the turn, then clear the draft — it never quits |
| `ctrl+d` | leave |
| `ctrl+l` | new conversation |
| `shift+enter` | new line (`alt+enter` / `ctrl+j` also work) |
| `ctrl+g` | write the prompt in `$EDITOR` |
| paste | every line lands in the field |
| `pgup` / `pgdn` | scroll the transcript |
| `↑` / `↓` | walk back through what you have asked |

The slash commands are the REPL's, unchanged — a test fails the build if the
two lists drift apart.

**Ctrl-C never quits**, matching the REPL — it interrupts a turn, then clears
the draft, and Ctrl-D is the way out. A terminal is not a private channel:
shell integrations clear the current line before typing into it, and a stray
`^C` from one of those should not end a session and everything in it.

**Why a session ended** is appended to `~/.andromeda-cli/tui.log`. A full-screen
app takes the terminal with it when it goes, so anything it printed on the way
out is wiped by the screen restore; a line on disk survives that.

**The composer is a real multi-line editor.** Paste as much as you like; every
line lands in the field. `enter` sends, `shift+enter` starts a new line
(`alt+enter` and `ctrl+j` also work, for terminals that cannot tell shift+enter
apart). The field grows with the text to ten lines and then scrolls, so a long
prompt never pushes the conversation off the screen.

`ctrl+g` opens `$EDITOR` for anything longer, and what you save fills the field.

It draws the same things the REPL does, because it runs the same renderer: the
markdown, the chart bars, the palette and the context meter are one module used
from two surfaces rather than two implementations that agree for a while.

**It is not the default**, and turning it on is deliberate. It takes over the
terminal and clears it on exit, which is the right trade when you are working
in it and the wrong one when you wanted three lines of output. `--tui` needs a
terminal on *both* stdin and stdout and refuses otherwise — a full-screen app
writes cursor moves, and a pipe must stay plain text. Passing `--tui` with a
prompt is refused rather than ignored, for the same reason `--resume` is.

**Prompts stop the agent.** When a tool needs approval, the turn is genuinely
blocked — the thread is parked, the composer is disabled, and the activity lane
says who is being waited on. Escape, `ctrl+c` and quitting the app all answer
*no*: walking away from the keyboard is not consent.

## Input you did not type

Editors and shell integrations type into new terminals — VS Code's Python
extension writes `source .../.venv/bin/activate` into any terminal where it
finds a venv. If that lands while Andromeda is starting, it would otherwise be
read as your first prompt, sent to the model, and come back as a command to
approve.

Two layers, because the injection can arrive either before or after the prompt
is drawn:

- Anything already in the terminal's buffer when a session starts is discarded.
- For the first few seconds after that, a line longer than a few characters that
  arrives **faster than it could be typed** — measured from its first character
  to Enter, not from when the prompt appeared — is ignored.

Both say what they ignored, and the line is still in your history. Type-ahead
and a paste in the first moments are lost with it; running a command you never
typed is the worse outcome.

**The permanent cure is at the source.** In VS Code, set
`"python.terminal.activateEnvironment": false` — the extension is what types
`source .../activate` into your terminals.

## Rewinding

A checkpoint is taken before every prompt, so rewinding lands where you were when
you asked — not after the answer you want to discard.

```
/history      the checkpoints you can go back to
/rewind       undo the last exchange
/rewind 3     go back to a numbered one
```

Rewinding discards the checkpoints taken after it. Keeping them would let a
second rewind jump *forward* into a transcript that no longer describes what
happened.

Checkpoints are saved with the session, so `--resume` brings the ability to
rewind back with it — the run you most want to undo is often the one you came
back to the next morning.

## Being asked, and not being asked

The agent can ask you a question mid-run with `clarify` rather than guessing —
a wrong assumption five steps in costs the whole run. In a pipe it refuses and
says so, because a default there is exactly the guess the tool replaces.

Approvals accumulate. At the prompt, `!` means "stop asking about this tool"; after
five plain approvals it offers.

```bash
andromeda approvals                 # what you have stopped being asked about
andromeda approvals forget <tool>
andromeda approvals clear
```

Two commands answer the questions the gate raises:

```bash
andromeda approvals test terminal        # what would happen, without running it
andromeda approvals test terminal --mode auto
andromeda approvals suggest              # what you have approved often enough
andromeda approvals suggest --apply 1,2
```

`test` runs the **real** gate and reports the verdict and the rule that
produced it — the ceiling, a belt, an override, a learned entry, the mode.
Nothing is executed, nobody is prompted and nothing is written down, which is
what makes it safe to point at `terminal`. It exits 0 for allowed, 2 for asks,
3 for denied, so a script can ask too.

`suggest` proposes; it never promotes. **A destructive or irreversible tool is
never proposed however often it was approved** — approving `git status` twenty
times says nothing about the next command the model puts through the same tool
— and a tool that is withheld for that reason is named rather than quietly
missing.

**A learned entry is bound to the tier it was granted at.** Trust `terminal`
while it is `destructive` and it stays trusted at `destructive` — if a tool's
tier ever rises, the entry stops applying and the gate is back. A permission
granted for one thing must not silently cover a more dangerous version of it.

Learned trust never widens itself: counts only drive a suggestion, promotion is
always your explicit answer. It cannot reopen a ceiling, a belt, or a disabled
tool, an explicit config override beats it, and it does not descend into a
delegated lane.

## Hooks

A shell script at a lifecycle boundary. Hooks are how you make this harness
follow your rules without forking it — block a command, rewrite an argument,
send a call to the approval prompt that would otherwise have gone straight
through, or just record what happened.

They live in the `hooks:` block of `config.yaml`, one list per event:

```yaml
hooks:
  pre_tool_call:
    - command: ~/.andromeda-cli/hooks/guard.sh
      matcher: terminal          # regex over the tool name; tool events only
      timeout: 10                # seconds, 1-300, default 60
      fail_closed: true          # a broken gate blocks instead of allowing
  on_session_end:
    - command: /usr/bin/env python3 ~/hooks/log-the-session.py
```

The script is handed one JSON object on stdin:

```json
{"hook_event_name": "pre_tool_call", "tool_name": "terminal",
 "tool_input": {"command": "git push --force"}, "session_id": "01J…",
 "cwd": "/Users/me/project", "extra": {"risk_tier": "destructive", "step": 3}}
```

and may print one JSON object on stdout to change what happens next:

| stdout | effect |
| --- | --- |
| `{"action": "block", "message": "…"}` | the call does not run; the message is what the model reads |
| `{"action": "modify", "args": {…}}` | the named arguments are replaced, then the call proceeds |
| `{"action": "approve", "message": "…"}` | the call goes to the approval prompt even if the policy allowed it |
| `{"context": "…"}` | `pre_llm_call` only — text added to this turn's user message |
| `{"output": "…"}` | `transform_*` only — replaces the text passing through |
| nothing | an observer; the run continues untouched |

`{"decision": "block", "reason": "…"}` and `{"decision": "modify",
"tool_input": {…}}` are accepted too, so a script written for another harness
works here. Exiting **2** blocks a `pre_tool_call` whether or not anything was
printed, which is all a one-line guard needs.

### The events

| event | when |
| --- | --- |
| `pre_tool_call` | a tool is about to run — the only event that can block |
| `post_tool_call` | it finished, succeeded, failed or was blocked |
| `transform_tool_result` | its output, on the way to the model |
| `pre_llm_call` | a request is about to go to the model |
| `post_llm_call` | a turn came back |
| `transform_llm_output` | the final answer, on the way to you |
| `on_session_start` / `on_session_end` / `on_session_reset` | a session opened, ended, or was cleared with `/new` |
| `on_compaction` | the transcript was shortened, and what it cost |
| `pre_approval_request` / `post_approval_response` | the gate opened, and what you answered |
| `subagent_start` / `subagent_stop` | a delegated lane, with the tools it actually called |
| `pre_command` | a slash command was typed |
| `on_job_start` / `on_job_end` | a scheduled job ran |

### Consent

A hook runs a command on your machine with your credentials, from a file a
`git pull` can change under you. So each `(event, command)` pair is approved
once, at a prompt, and:

- the approval records the script's **mtime**, and `andromeda hooks doctor`
  tells you when the file has changed since — the approval was for the script
  you read, not for the path it sits at;
- a run with no terminal registers **nothing** unless you opt in with
  `--accept-hooks`, `ANDROMEDA_ACCEPT_HOOKS=1`, or `hooks_auto_accept: true`.
  A scheduled job is not the moment a new script first executes;
- `ANDROMEDA_SAFE_MODE=1` skips hooks entirely, along with everything else you
  configured — it is the "is it me or is it my config" switch.

```bash
andromeda hooks list                 # what is configured, and whether it may run
andromeda hooks doctor               # check every hook without waiting for a session
andromeda hooks test pre_tool_call --for-tool terminal
andromeda hooks revoke ~/.andromeda-cli/hooks/guard.sh
```

`test` and `doctor` execute the script, so neither will touch a hook you have
not approved yet — the reason to run `doctor` on a config you just pulled is to
see what is *about to* register, and running those scripts to tell you about
them would already have done the thing you were checking for.

### Failing

Hooks fail **open**: a missing script, a timeout or unreadable output is logged
and contributes nothing, because a broken hook must not be able to stop you
working. `fail_closed: true` inverts that for `pre_tool_call`, which is what a
secret scanner or a policy check wants — a gate that crashed has not granted
permission. A hook that times out has its whole process tree taken down; one
that finishes keeps whatever it started, so `some-daemon &` still works.

## Plugins

A hook runs a script at a lifecycle boundary. A **plugin** is the other half:
Python that Andromeda imports and hands a registration object, so outside code
can add tools, commands and skills — or take over the memory backend, the
scheduler, the model provider or the secret resolver.

```bash
andromeda plugins list
andromeda plugins install owner/repo
andromeda plugins enable <name>
andromeda plugins capabilities
```

### Two kinds

```
  A PYTHON PLUGIN                    A PORTABLE PACKAGE
  ───────────────                    ──────────────────
  plugin.yaml                        plugin.json
  __init__.py  → register(ctx)       skills/<name>/SKILL.md
                                     mcp.json

  imported into this process         never imported — nothing in it runs
  can declare capabilities           declares none, and is refused if it tries
```

The second is the interchange format from `agent-plugins.org`, so a package
written for another harness loads here unchanged. It carries **skills and MCP
servers and no code at all**, which makes it a strictly safer thing to install:
its skills are text the model may read, and its servers go through the ordinary
MCP path, tool gate and all.

### What a plugin is

A directory with two files.

```
my-plugin/
├── plugin.yaml      name, version, and what it is asking for
└── __init__.py      def register(ctx): ...
```

```yaml
name: acme
version: 1.0.0
description: Talk to Acme.
requires_env: [ACME_API_KEY]
```

```python
def register(ctx):
    ctx.register_tool(
        "acme_search",
        "Search Acme.",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        run_search,
        risk_tier="outbound",
        category="read",
    )
    ctx.register_hook("on_session_start", lambda **kw: warm_cache())
    ctx.register_command("acme", lambda raw: status_line())
```

The full surface, grouped by what it does:

```
ADD                              REPLACE (needs a capability)
─────────────────────────        ────────────────────────────────
register_tool                    register_memory_backend
register_hook       (17 events)  register_cron_provider
register_command    (/slash)     register_model_provider
register_cli_command(andromeda)  register_secret_source
register_skill                   register_browser_provider
register_delivery                register_specialist
register_web_search_provider     register_approval_transport
register_lsp_server              register_auxiliary_task
register_blueprint               register_system_prompt_section
register_eval                    register_middleware
register_redaction_patterns      register_tool(override=True)
                                 register_command(override=True)

REACH BACK                       TALK TO OTHER PLUGINS
──────────────────               ─────────────────────
ctx.state        10MB of JSON    ctx.emit("event", payload)
ctx.get_config / set_config      ctx.subscribe("other:event", fn)
ctx.dispatch_tool                ctx.has_plugin("other")
ctx.call_mcp                     ctx.on_unload(fn)
ctx.llm                          ctx.profile_name
```

`risk_tier` and `category` are the same words every built-in tool uses, so a
plugin tool goes through the same approval gate as `terminal` does. Omit them
and you get `outbound`/`write` — a tool that asks first, which is the right
default for code somebody else wrote.

### Where they come from

```
bundled      shipped with Andromeda
user         ~/.andromeda-cli/plugins/
project      ./.andromeda/plugins/      ← ignored unless you opt in
pip          packages exposing the andromeda_cli.plugins entry point
```

Later beats earlier on a name collision, so dropping your own copy of a bundled
plugin into `~/.andromeda-cli/plugins/` replaces it with no config change.

**Project plugins are off by default.** Set
`ANDROMEDA_ENABLE_PROJECT_PLUGINS=1` to turn them on. Without that, cloning a
repository cannot put Python into your agent's process just because you `cd`
into it.

### Capabilities

Most of what a plugin does is *additive* and needs no permission: a new tool is
still gated by the approval policy, a new command is a new name, a new language
server is a binary you may not even have. Twelve things are different, because
they mean taking over something the harness already owns:

| Capability | What it lets the plugin do |
|---|---|
| `tools.override` | Replace a built-in tool — including `terminal` |
| `commands.override` | Replace a built-in slash command |
| `model.provider` | Answer as the model provider |
| `model.auxiliary` | Make side calls on your credential, outside the conversation |
| `secrets.source` | Resolve `secrets:` references |
| `cron.provider` | Decide when scheduled jobs are due |
| `memory.backend` | Own where memories live |
| `browser.provider` | Answer as the browser, cookies and all |
| `prompt.inject` | Add text to the system prompt, every turn |
| `lanes.specialist` | Define a delegation lane — a belt is a permission boundary |
| `approvals.transport` | Answer approval prompts when you are not here |
| `runtime.middleware` | Wrap every tool call and every model call |

Those are declared in the manifest and refused until you grant them:

```yaml
capabilities: [tools.override]
```

`andromeda plugins enable` names each one, in a sentence about what it does to
you, and asks once. The grant records a hash of exactly what you agreed to — so
an update that asks for something new asks you again, and one that asks for
less silently drops what it no longer needs.

**None of this is a sandbox, and it does not pretend to be.** A plugin is
Python running as you, in this process. It can `import os`, monkey-patch the
loop, and ignore every gate above. What capabilities do is decide what the
harness *hands* it, and give you an honest record of what you agreed to. The
scan on install catches careless and obvious. It does not catch determined.
Read what you install.

### Installing

```bash
andromeda plugins search tide          # the community index
andromeda plugins install tides        # a name from it
andromeda plugins install owner/repo   # or a repository
andromeda plugins install ./my-plugin  # or a directory
andromeda plugins install owner/repo --ref v1.2.0
```

**Indexed is not audited.** An entry's *metadata* was reviewed — that the name
is not a typosquat, that the repository is the one the description claims. Not
its code, which changes after the review anyway. Every index entry pins a
40-character commit, because a tag can be moved and a branch head moves by
definition.

The order matters and is fixed:

```
clone ──▶ security scan ──▶ capability consent ──▶ "enable it now?"
              │                                          │
        dangerous → refused,                       default is no
        and --force does not help
```

Nothing is imported until the last step. A plugin that has been enabled has
already run its `register()`, so asking afterwards would be asking about
something that already happened.

Enabling a plugin also switches on the tools it adds — `enabled_tools` is an
allowlist, and a plugin tool that is not in it is a tool the model is never
offered. `andromeda tools disable <name>` turns one back off afterwards.

### Writing one

```bash
andromeda plugins new tides --description "Watches the tide."
andromeda plugins doctor tides
andromeda plugins install tides --enable
```

`new` writes a manifest, a `register(ctx)` with one tool and one command, and a
README — a plugin that already loads, so the next step is editing rather than
assembling.

```bash
andromeda plugins doctor .
```

Loads your plugin through the real runtime **with the network cut**, and
reports what failed: a manifest typo, a missing `register`, a `register` that
raises, a capability this version does not have, a scan finding. It also prints
what you registered, which is how you find out that the thing you thought you
added is not there.

Two things `ctx` gives you beyond registration:

```python
ctx.state.set("cursor", 41)      # 10MB of JSON, private to this plugin
ctx.emit("synced", {"n": 12})    # published as "<your id>:synced"
ctx.subscribe("other:event", fn) # anyone may listen; only you may emit as you
```

### Wrapping the loop

Hooks tell you what happened and can veto. **Middleware changes what happens.**

```python
def register(ctx):
    ctx.register_middleware("tool_request", add_a_default_argument)
    ctx.register_middleware("tool_execution", retry_once_on_timeout)
```

Four kinds. The two `_request` ones are handed a payload and return a
replacement; the two `_execution` ones are handed the call itself and decide
whether to run it — twice for a retry, not at all for a cache hit.

```
  tool_request     the arguments, before the tool runs
  tool_execution   the tool call, as a callable
  llm_request      the payload, before it reaches the provider
  llm_execution    the provider call, as a callable
```

Execution middleware nests: the first registered is outermost, so it sees the
others' retries as one call. All four sit behind `runtime.middleware`, because
holding one and not the others is not a meaningful distinction.

### A set of plugins as one file

```bash
andromeda plugins pack export --name writing-desk --out desk.yaml
andromeda plugins pack show desk.yaml
andromeda plugins pack install desk.yaml
```

```yaml
name: writing-desk
plugins:
  - name: wordcount
    ref: 4f1c2b9a8e7d6c5b4a3928170615243342516070
config:
  wordcount:
    target: 800
```

Three refusals are the whole format. **Every entry must pin a 40-character
commit** — a pack that named a tag would install different code tomorrow under
the same description. **A pack can never grant a capability**; each plugin
still goes through its own consent. **`config:` cannot carry a credential** —
a pack is a file people share, and it should not be the convenient place to put
one.

### A package with no code in it

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "shipyard",
  "version": "1.0.0",
  "description": "Deployment know-how and a filesystem server."
}
```

```
shipyard/
├── plugin.json
├── skills/deploy/SKILL.md    → loadable as `shipyard:deploy`
└── mcp.json                  → connected as `shipyard:<server>`
```

Servers are namespaced by the package, so two packages carrying a `github`
server do not become one and neither can shadow a server you configured
yourself. `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` expand in `command`, `args`,
`env` and `cwd` — they are the only two placeholders, both resolve to
directories this harness owns, and a `cwd` landing outside the package is
dropped.

One broken skill is a note, not a failure: the parts are independent, and
`andromeda plugins show <name>` tells you which one did not load rather than
leaving you to wonder why it never appeared.

### Turning it all off

```bash
andromeda --no-plugins            # this run
ANDROMEDA_NO_PLUGINS=1            # every run
```

`andromeda plugins ...` never loads plugins, so a plugin that breaks on import
can always be disabled — the command that fixes it is not the command it breaks.

## Stopping

```bash
andromeda pause --reason "deploying by hand"
andromeda resume
```

Holds the scheduler. **New work only** — a job half-way through is not made
safer by being killed mid-write — and **never your own terminal**: a REPL, a
one-shot and an editor session are a person asking for something while watching
the answer, and a stop button that takes those away is a stop button you cannot
use to find out what is wrong.

It is a file (`~/.andromeda-cli/PAUSED`), so anything can set it: another
terminal, a script, a `touch` over SSH from a phone. An empty file still pauses;
the JSON inside is a courtesy. **An unreadable sentinel counts as paused** —
failing open would lift somebody's emergency stop at exactly the moment the
filesystem is misbehaving, which is the moment they engaged it.

A paused install says so in `andromeda doctor` and at the top of a session,
because the alternative is a scheduler that looks healthy and fires nothing.

## Moving to another machine

```bash
andromeda backup out.tar.gz     # everything, INCLUDING the device token
andromeda export out.tar.gz     # everything except credentials
andromeda restore in.tar.gz     # --force to overwrite existing state
```

Two verbs rather than one flag, because they differ in exactly one way and that
way matters: a `backup` is for moving your own install and should be treated
like a password; an `export` is safe to share or commit. Collapsing them is how a
file that quietly holds a live credential ends up in a git repo.

Restore drops any archive member that would land outside your home, and any
symlink — extracting an archive someone handed you is precisely when that
matters. It then rebuilds the search index from the restored transcripts,
because a restore that ends with "nothing found" reads as a restore that lost
the sessions.

The index itself is never in the archive: it is derived from `sessions/`, so
carrying it would add the largest file in your home to every archive to save
one reindex. Memories always are, whichever backend holds them — on `sqlite`
they live inside that index, and an export that quietly carried none is a thing
you would discover on the other machine.

## Scheduled jobs

The agent, without you. `andromeda cron` schedules work; `andromeda cron
install` makes something actually run it.

```bash
andromeda cron add "every 1h" "check the build and tell me if it broke"
andromeda cron add "0 9 * * 1-5" "summarise yesterday's commits" --deliver notify
andromeda cron add "in 2h" "check whether the deploy finished"     # once, then retires
andromeda cron list | show <id> | logs <id> | run <id> | rm <id>
andromeda cron install        # runs at login, restarts if it dies
andromeda cron service        # is it actually running?
andromeda cron daemon         # or in this terminal
```

`install` writes a launchd agent or a systemd user unit and hands the process
over — the daemon does not fork, because both supervisors want to own what they
supervise.

**It snapshots your `PATH`.** A user agent starts with roughly
`/usr/bin:/bin`, so without this a job calling `gh`, `rg` or anything from
Homebrew works when you run it by hand and fails when the scheduler runs it.
The snapshot is taken from the shell you install from, so install from a normal
login shell and re-run `install` after you change your `PATH`. `cron service`
prints how many entries were captured and says when they differ from your
current shell.

Exactly five variables travel — `PATH`, `LANG`, `ANDROMEDA_HOME`,
`ANDROMEDA_PROVIDER`, `OPENROUTER_API_KEY` — and if the last one is set the
command says so out loud, because a service file is a new copy of a credential.
The file is written `0600`.

**Consent is established at creation and stated in full.** A job carries the
approval mode it was created with, and that mode beats whatever the machine is
set to — one created read-only stays read-only even if you later set
`approval_mode: auto` for your own sessions. A corrupt or unrecognised mode
reads as `ask`, the narrow one: a damaged field must never widen what a job may
do.

**Every run is recorded, success or failure**, with its full output in
`~/.andromeda-cli/cron/output/<job>/`. A scheduler that only logs failures
leaves you unable to tell "ran and found nothing" from "never ran".

### Three ways to spend nothing

An agent turn is the most expensive part of the loop, so most of this section
is about not taking one.

**Watch something.** A cheap source runs first each tick and its output is
hashed. Unchanged means the agent does not run at all.

```bash
andromeda cron add "every 10m" "Something changed on the status page — say what." \
  --watch-url https://example.com/status
andromeda cron add "every 5m" "The queue depth changed — is it a problem?" \
  --watch queue-depth.sh
```

Comparison is **exact bytes** — no timestamp stripping, no whitespace
normalisation. Normalising means guessing which differences are meaningful, and
a guess that is wrong in the quiet direction is a monitor that never fires. Emit
stable output from your source. A source that *fails* is an error, never a
change, and the stored hash is left alone so a source that recovers to its
previous output still suppresses.

**Skip the agent entirely.** A classic watchdog needs no model at all.

```bash
andromeda cron add "every 10m" --no-agent --script disk-check.sh
```

The script is the job and its stdout is the report. **Empty stdout is silence** —
a watchdog that reports every time it finds nothing is a watchdog people mute.

**Let something else gather the facts.**

```bash
andromeda cron add "every 1h" "Anything here worth waking me for?" --script collect.sh
andromeda cron add "every 1h" "Turn the collector's findings into one paragraph." --after job_ab12
```

`--script` puts a script's output into the prompt; `--after` puts another job's
latest output into it, so one job can collect and another can reason.

Scripts live in `~/.andromeda-cli/scripts/` and are named, never pathed. A job
spec is data — it can be written by the agent — and a path in data that can
point anywhere is arbitrary code execution on a timer. `.sh`, `.bash` and `.py`;
an unknown extension is refused rather than fed to a guessed interpreter.

### What a job remembers

Every run starts fresh, which is what makes a job reproducible. The one thing
that carries over is a small notepad — for the cursor a polling job needs.

```bash
andromeda cron notepad <id>                    # what it is holding
andromeda cron notepad <id> set cursor 12345
andromeda cron notepad <id> clear
```

The job writes it through a `notepad` tool bound to that job. It is tiered
`safe_local` on purpose: a job in the default `ask` mode is narrowed to
read-only precisely because nobody is watching it, and a job that can be trusted
to remember its own cursor but not to run commands is the common case. It is
capped at 16KB a note and 64KB a job, because this text is prepended to every
prompt that job ever sends.

### Automations on offer

Andromeda proposes automations. It never creates one.

```bash
andromeda cron suggest              # what is on offer, and where it came from
andromeda cron suggest accept 1     # create it
andromeda cron suggest dismiss 1    # never offer it again
```

Four sources: a curated `catalog`, a `blueprint` shipped by a skill you
installed, `usage` — something you have asked for by hand three times, in your
own words, with a recurrence in it — and `integration`, an automation that
became possible because a capability appeared.

A dismissal **latches**: nothing is ever re-offered after you say no. The
backlog is capped at five, because a list of twenty things to decide about is a
list nobody opens. And a stored proposal is inert data — it is validated only
when you accept it, by the same `Schedule.add` every other path uses, so a
suggestion cannot smuggle in an unattended job by being written to disk.

Installing a skill that ships a `blueprint:` block registers a *suggestion*,
never a job. A skill you installed should not be able to schedule work on your
machine without being asked.

### Automations as a form

Nobody should have to type cron.

```bash
andromeda cron blueprint                  # what is available
andromeda cron blueprint show watch-url   # its fields
andromeda cron blueprint use watch-url url=https://example.com/status interval_min=30
```

A blueprint carries a fixed recurrence and parameterises only the parts you
have an opinion about — a time, a weekday set, an interval. An unknown field
name is **refused**, not ignored: a typo'd `tiem=07:15` that silently creates a
job at the default time works, is wrong, and says nothing.

### The agent can schedule its own follow-ups

Ask for something recurring and it will use the `cron` tool.

**An agent may propose autonomy. Only a person grants the unattended kind.**
This is the approval gate's "a child is never more permissive than its parent",
applied to time instead of to depth: a job the agent creates is read-only, the
tool has no argument that could ask for anything else, and widening is a
separate command a person types after reading what the job will do.

```bash
andromeda cron approve <id> --approval auto    # shows the prompt first
```

The agent cannot *run* a job either — that would be an agent turn nested inside
an agent turn with nothing supervising the pair. And a job whose prompt or
script contains a command that stops or restarts the scheduler is refused at
creation: under a supervisor that is a respawn loop, not a restart.

### Saying nothing

A job with nothing worth waking you for replies `[SILENT]` and delivers
nothing. The run is still recorded in full — this suppresses the message, never
the record. A job that reports "nothing to report" every hour trains you to
ignore it, and then it is worse than not existing.

### What was in flight when the machine went down

```bash
andromeda cron executions              # every attempt
andromeda cron executions --unresolved # the ones nobody recorded the end of
```

The run history says what a job produced. This says what was *attempted* —
including attempts a killed scheduler never finished. An attempt is written
before anything with a side effect and closed after, so "never ran" and "ran,
did the thing, and died before recording it" stop being the same empty history.

Abandoned attempts become `unknown`, never retried. `unknown` is the honest
state: the side effects may or may not have happened, which is the only thing
anyone can actually know. Recovery only marks an attempt abandoned when its
owner is *proved* gone — pid **and** process start time, because pids are
reused and calling a live attempt dead is how you get two copies.

### A job's own settings

```bash
andromeda cron add "0 6 * * *" "..." --thinking high --tools read_file,terminal \
  --skill triage --deliver webhook --deliver-to https://hooks.example/andromeda \
  --attach <session-id>
```

A job is not the session that created it. `--tools` can only ever **subtract** —
a job cannot name a tool you switched off and get it back. `--attach` appends
each run to a session, so a scheduled follow-up shows up in `--resume` beside
the conversation that asked for it.

### When things go wrong

- **Five failures in a row and a job stops trying.** A job whose credentials
  expired fails identically forever; the failure is already recorded the first
  time. `andromeda cron resume <id>` when it is fixed.
- **A missed run fires once, not once per interval slept through.** A laptop
  shut over six hourly ticks produces one run, and the history says how late it
  was.
- **One scheduler at a time**, enforced with a lock. Two daemons fire every job
  twice, and the second copy of a job that writes files is not a duplicate
  report, it is a second edit.
- **A tick is a heartbeat.** `andromeda cron list` and `andromeda doctor` both
  say when the scheduler last ran, because "3 jobs scheduled" reads as working
  and is exactly as true when nothing has ticked for a week.

## Evaluations

Unit tests answer "does this function do what I wrote". Evals answer "does the
agent still do the right thing" — which depends on a model that changes
underneath you, a prompt you edit, and a tool description you reword. None of
those break a unit test.

```bash
andromeda eval          # run them against the live model
andromeda eval list
andromeda eval --json   # machine-readable, for CI
```

Scenarios live in [`evals/`](../evals) as YAML: a workspace, a prompt, and
checks written against **observable outcomes** — files that exist, tools that
were called, text that appears. Never against the model's exact words, because
asserting on phrasing gives you a suite that fails on a synonym and passes on a
lie.

They cost money, which is the point: a mocked eval measures the mock.

### Repeat, compare, and run them at once

```bash
andromeda eval --repeat 5          # a pass rate, not a verdict
andromeda eval --jobs 4
andromeda eval report              # what moved since the last run
andromeda eval runs
```

An agent is stochastic, and one run of a stochastic system is an anecdote.
`--repeat` reports **n/m passed** and calls a scenario that passed some of the
time **flaky** rather than rounding it to either answer — an intermittent
behaviour is a real finding that a single run reports as fine or broken
depending on the day. When a repeated scenario fails at all, the report shows
the failing run, not the one that happened to work.

Every run is saved, so `eval report` can say what changed: what **broke**, what
was **fixed**, and what got **less reliable** without failing outright — 5/5 to
3/5 is the earliest thing worth knowing, and a pass count on its own cannot say
it. If the model changed between the two runs, the report says so first, since
that is the likeliest explanation for everything under it.

Checks also cover order and cost: `tools_in_order` asserts a subsequence ("it
read the file before it wrote it") rather than an exact call list, `steps_under`
bounds how many tool calls it took, and `file_matches` is a regex over a file.

## Inside an editor

```
andromeda acp
```

Speaks the [Agent Client Protocol](https://agentclientprotocol.com) on stdin
and stdout, so an editor that has adopted it — Zed and the others — can drive
this harness as its agent. Point the editor's ACP agent configuration at that
command. The editor owns the window; this owns the turn.

While a turn runs the editor sees the answer as it arrives, each tool call with
its title and how it ended, and the todo list as a plan. The approval gate is
asked **through** the editor: same policy, same tiers, same learned approvals,
in a dialog instead of a terminal. Anything that is not one of the four answers
— a cancelled dialog, a closed window — is a refusal, because the alternative
is a tool that runs when nobody answered.

Written against the wire rather than an SDK, for the same reason the MCP client
is: the protocol is versioned and a few hundred lines, and a dependency is one
whose next release breaks every session.

**Nothing else may write to stdout while it runs** — the protocol is the
stream. Every console this program owns is pointed at stderr for the duration,
which is where an editor's log looks anyway.

## Completion

```bash
eval "$(andromeda completion bash)"    # ~/.bashrc
eval "$(andromeda completion zsh)"     # ~/.zshrc
andromeda completion fish | source     # fish config
```

Generated from the program's own argument parser, so it cannot go stale — a
hand-kept list is wrong the first time somebody adds a command, and the symptom
is a tab that does nothing, which reads as "completion is not installed".
Profile names are completed from disk after `-p`.

## The same job over many inputs

```bash
andromeda batch tickets.jsonl --prompt "Classify this ticket: {body}"
andromeda batch tickets.jsonl --resume
andromeda batch --show tickets.jsonl.results.jsonl --failures
```

A shell loop does this too, and then the laptop sleeps at item 137 and you have
no idea which ones finished. So the unit here is the **ledger**: every row's
answer is appended to a JSONL the moment it exists, and `--resume` skips what is
already in it.

Each row gets its own conversation, so nothing one row says can reach the next
— that costs the prompt cache and buys the only property that makes two rows'
answers comparable. A row that fails is recorded as a failure and the batch
carries on; `--resume` then retries only those. `--dry-run` shows what would
run and spends nothing.

A row's `id` is what a resume matches on. Rows without one are matched by line
number, so reordering the file between runs breaks a resume — give them ids.

## Configuration

`~/.andromeda-cli/config.yaml`. Defaults < file < `ANDROMEDA_*` env < flags.

Not `~/.andromeda` — that is the desktop app's data directory (its sqlite
stores, vault key and browser profiles), and the installer's checkout lands
there too.

```bash
andromeda config get              # all settings
andromeda config set model a/b
andromeda config path
```

Secrets live in a separate `credentials.json` and are never printed by any
command — which is why `config get` can dump everything without redaction.

`ANDROMEDA_HOME` relocates both files.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the run failed (not signed in, network, upstream) |
| 2 | usage error |
| 3 | out of credit |
| 130 | interrupted |

## Layout

```
andromeda_cli/     commands, REPL, config, profiles, session assembly, output,
                   rendering, and state/: the derived search index over the
                   transcripts, its queries, recap, export and repairs
andromeda_agent/   the turn loop, the approval gate, provider lanes, errors,
                   and the autonomy layer: schedule, runner, monitor, notepad,
                   scripts, delivery
andromeda_tools/   the registry, executors, skills, memory, web, browser, MCP
andromeda_tui/     the full-screen surface: events, driver, widgets, prompts
plugins/           the bundled plugins, resolved by walking up from the package
tests/
install/
```

`andromeda_tui/` runs the agent in-process on a worker thread rather than
speaking a protocol to it. Splitting a TUI out over a JSON-RPC gateway pays off
when the same tree also renders a desktop app and a web dashboard; one client
does not pay for that. What it keeps is the seam — `events.py` is a
serialisable event vocabulary and blocking questions are answered by request id
— so a gateway is a later addition rather than a rewrite.

## Tests

```bash
.venv/bin/python -m pytest
```

## Status

M4 complete, plus compaction, concurrent lanes, a rendered terminal surface,
MCP, background processes, rewind, `clarify`, learned approvals, vision,
thinking-level control, an evals harness, the full-screen interface, and the
autonomy layer — monitored jobs, script jobs, chaining, notepads, self-
scheduling and a supervised scheduler — and the plugin socket. See
[`docs/andromeda-cli-plan.md`](../docs/andromeda-cli-plan.md) for what is next.
